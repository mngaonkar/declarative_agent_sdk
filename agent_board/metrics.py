"""
What the model server behind an agent is actually doing.

When an agent takes four minutes to answer, the cause is almost never in the
agent: it is a cold model being reloaded, a single slot serialising two agents,
or a prompt that has grown to 90k tokens and now costs real time to decode. None
of that is visible from the board, so every slow turn starts with the same
round of ssh-and-curl. This module does that round over HTTP instead.

Three sources, all optional, all cheap:

  ``/metrics``  Prometheus counters — lifetime throughput, queue depth. Note
                that counters only advance when a request *completes*, so they
                say nothing about the request happening right now.
  ``/slots``    the live slot: how many prompt tokens this request carries, how
                many came from the prefix cache, how far generation has got.
                This is the one that answers "what context is it passing?".
  ``/props``    which model is loaded, and the context window it was given.

Built for llama.cpp, which is what the agents here point at, with vLLM's metric
names mapped onto the same shape. Anything else degrades to "no recognised
metrics" rather than failing — an endpoint that exposes nothing is a fact about
the endpoint, not an error in the board.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from declarative_agent_sdk.core.agent_logging import get_logger

logger = get_logger(__name__)

DEFAULT_TIMEOUT = 4.0

# A sample older than this is not a window, it is a different era: the model may
# have been swapped or the server restarted, and a rate derived across that gap
# would be invented.
SAMPLE_MAX_AGE = 600.0

# `name{labels} value`, which is all a server emits for counters and gauges.
# Histogram buckets carry labels we do not use and fall out naturally.
_PROM_LINE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{[^}]*\})?\s+(?P<value>[^\s]+)$"
)

# Normalised name → the keys that mean it, in preference order. llama.cpp first
# because that is what is deployed here.
_FAMILIES: Dict[str, Tuple[str, ...]] = {
    "prompt_tokens": ("llamacpp:prompt_tokens_total", "vllm:prompt_tokens_total"),
    "prompt_tokens_cached": ("llamacpp:prompt_tokens_cached_total",),
    "prompt_seconds": ("llamacpp:prompt_seconds_total",),
    "predicted_tokens": (
        "llamacpp:tokens_predicted_total", "vllm:generation_tokens_total",
    ),
    "predicted_seconds": ("llamacpp:tokens_predicted_seconds_total",),
    "decode_calls": ("llamacpp:n_decode_total",),
    "tokens_max": ("llamacpp:n_tokens_max",),
    "prefill_rate": ("llamacpp:prompt_tokens_seconds",),
    "decode_rate": ("llamacpp:predicted_tokens_seconds",),
    "processing": ("llamacpp:requests_processing", "vllm:num_requests_running"),
    "deferred": ("llamacpp:requests_deferred", "vllm:num_requests_waiting"),
    "kv_cache_ratio": ("llamacpp:kv_cache_usage_ratio", "vllm:gpu_cache_usage_perc"),
    "busy_slots_per_decode": ("llamacpp:n_busy_slots_per_decode",),
}


def base_url(endpoint: str) -> str:
    """
    The server root, from whatever an ``agent.yaml`` endpoint says.

    Configs point at the OpenAI-compatible path (``http://host:30081/v1``) but
    the introspection routes sit at the root, so the API suffix comes off.
    """
    url = (endpoint or "").strip().rstrip("/")
    if not url:
        return ""
    for suffix in ("/v1/chat/completions", "/v1/completions", "/v1", "/api"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
            break
    return url.rstrip("/")


def parse_prometheus(text: str) -> Dict[str, float]:
    """Flatten an exposition response to {metric name: value}."""
    values: Dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _PROM_LINE.match(line)
        if not match:
            continue
        try:
            value = float(match.group("value"))
        except ValueError:          # a quoted label value, or junk
            continue
        # NaN and +Inf parse happily in Python but mean "no reading"; carrying
        # them forward would put 'NaN tok/s' on a card.
        if value != value or value in (float("inf"), float("-inf")):
            continue
        values[match.group("name")] = value
    return values


def normalise(raw: Dict[str, float]) -> Dict[str, float]:
    """Map a server's metric names onto the names this board reports."""
    out: Dict[str, float] = {}
    for field, candidates in _FAMILIES.items():
        for key in candidates:
            if key in raw:
                out[field] = raw[key]
                break
    return out


def _slot_context(slots: Any) -> Dict[str, Any]:
    """
    The prompt size of the request in the slot — the context being passed.

    llama.cpp keeps a slot's counters after the request ends, so this reads as
    the live request while ``is_processing`` and as the last one afterwards.
    Both are worth showing; which it is, is reported rather than guessed.
    """
    if not isinstance(slots, list) or not slots:
        return {}

    # The busy slot if there is one, else the slot that ran most recently.
    busy = [s for s in slots if isinstance(s, dict) and s.get("is_processing")]
    pool = busy or [s for s in slots if isinstance(s, dict)]
    slot = max(pool, key=lambda s: int(s.get("id_task") or 0))

    live = bool(slot.get("is_processing"))
    tokens = int(slot.get("n_prompt_tokens") or 0)
    window = int(slot.get("n_ctx") or 0)
    context: Dict[str, Any] = {
        "live": live,
        "tokens": tokens,
        "n_ctx": window,
        "slots_busy": len(busy),
        "slots_total": len([s for s in slots if isinstance(s, dict)]),
    }
    if window:
        context["fill"] = round(tokens / window, 4)

    # An idle slot keeps its prompt size but zeroes the cache split and the
    # decode progress, so those are reported only while a request is in the
    # slot. Carrying the zeros forward would read as "nothing was cached",
    # which is a much worse claim than "not known for a finished request".
    if live:
        context["cached"] = int(slot.get("n_prompt_tokens_cache") or 0)
        context["processed"] = int(slot.get("n_prompt_tokens_processed") or 0)
        nxt = slot.get("next_token")
        entry = nxt[0] if isinstance(nxt, list) and nxt else nxt
        if isinstance(entry, dict):
            context["decoded"] = int(entry.get("n_decoded") or 0)
            # -1 is llama.cpp for "no limit", worth seeing as such.
            context["remain"] = int(entry.get("n_remain") or 0)
    params = slot.get("params")
    if isinstance(params, dict):
        cap = params.get("n_predict", params.get("max_tokens"))
        if cap is not None:
            context["max_tokens"] = int(cap)
    return context


def _rates(now: Dict[str, float], before: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Throughput over the window between two polls.

    Tokens per second *of model time*, not of wall-clock, so an idle gap
    between samples does not read as a slowdown. The counters move only when a
    request finishes, so a window that contains no completion yields nothing —
    reported as absent rather than as zero, because 0 tok/s is a claim about
    the model and this is a claim about the measurement.
    """
    if not before:
        return {}
    age = time.time() - float(before.get("at") or 0)
    # Under half a second is not a window; two polls that close together differ
    # by rounding, not by work done.
    if age < 0.5 or age > SAMPLE_MAX_AGE:
        return {}
    past: Dict[str, float] = before.get("metrics") or {}

    out: Dict[str, Any] = {"window_seconds": round(age, 1)}
    for label, tokens_key, seconds_key in (
        ("decode", "predicted_tokens", "predicted_seconds"),
        ("prefill", "prompt_tokens", "prompt_seconds"),
    ):
        tokens = now.get(tokens_key, 0.0) - past.get(tokens_key, 0.0)
        seconds = now.get(seconds_key, 0.0) - past.get(seconds_key, 0.0)
        # A counter that went backwards means the server restarted.
        if tokens > 0 and seconds > 0:
            out[f"{label}_rate"] = round(tokens / seconds, 1)
            out[f"{label}_tokens"] = int(tokens)
    return out


class Sampler:
    """
    Remembers the previous reading per endpoint, so rates can be windowed.

    In memory only: a restart loses the window, not the numbers, and the first
    reading after one simply reports lifetime figures.
    """

    def __init__(self) -> None:
        self._last: Dict[str, Dict[str, Any]] = {}
        # The last window that actually measured something, per endpoint.
        self._rates: Dict[str, Dict[str, Any]] = {}

    def held(self, url: str, rates: Dict[str, Any]) -> Dict[str, Any]:
        """
        This window's rates, or the last measured ones, marked as such.

        A window with no finished request measures nothing, and llama.cpp's own
        throughput gauges cover only the requests since the previous /metrics
        read, so between turns both read as nothing or 0. Showing the last real
        figure with its age is truer than either.
        """
        if any(key in rates for key in ("decode_rate", "prefill_rate")):
            self._rates[url] = dict(rates, measured_at=time.time())
            return rates
        last = self._rates.get(url)
        if not last:
            return rates
        held = {k: v for k, v in last.items() if k != "measured_at"}
        held["held"] = True
        held["age_seconds"] = round(time.time() - float(last["measured_at"]), 1)
        return held

    def take(self, url: str, metrics: Dict[str, float]) -> Optional[Dict[str, Any]]:
        before = self._last.get(url)
        if metrics:
            self._last[url] = {"at": time.time(), "metrics": dict(metrics)}
        return before

    def forget(self, url: str) -> None:
        self._last.pop(url, None)
        self._rates.pop(url, None)


async def probe(
    client: httpx.AsyncClient,
    endpoint: str,
    sampler: Optional[Sampler] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """
    Read one model server. Never raises — an unreachable endpoint is a result.

    ``/metrics`` has to be enabled (llama.cpp ``--metrics``) and ``/slots`` not
    disabled (``--no-slots``); each missing route costs the section it feeds
    and nothing else.
    """
    root = base_url(endpoint)
    if not root:
        return {"endpoint": endpoint, "reachable": False, "error": "no endpoint configured"}

    report: Dict[str, Any] = {"endpoint": endpoint, "url": root, "reachable": False}

    async def read(path: str) -> Optional[httpx.Response]:
        try:
            response = await client.get(f"{root}{path}", timeout=timeout)
        except Exception as exc:      # noqa: BLE001 — surfaced to the operator
            report.setdefault("error", f"{type(exc).__name__}: {exc}")
            return None
        return response

    props = await read("/props")
    if props is None:
        return report
    report["reachable"] = True
    if props.status_code == 200:
        try:
            data = props.json()
            report["model"] = str(data.get("model_alias") or data.get("model_path") or "")
            report["slots_total"] = int(data.get("total_slots") or 0)
            window = (data.get("default_generation_settings") or {}).get("n_ctx")
            if window:
                report["n_ctx"] = int(window)
        except Exception:             # noqa: BLE001 — not a llama.cpp server
            pass

    metrics_response = await read("/metrics")
    if metrics_response is not None and metrics_response.status_code == 200:
        values = normalise(parse_prometheus(metrics_response.text))
        if values:
            report["metrics"] = values
            cached = values.get("prompt_tokens_cached", 0.0)
            fresh = values.get("prompt_tokens", 0.0)
            if cached + fresh > 0:
                report["cache_hit_ratio"] = round(cached / (cached + fresh), 4)
            if sampler is not None:
                report["recent"] = sampler.held(
                    root, _rates(values, sampler.take(root, values)))
        else:
            report["metrics_note"] = "no recognised metrics at /metrics"
    elif metrics_response is not None:
        report["metrics_note"] = (
            f"/metrics returned {metrics_response.status_code} — "
            "start llama.cpp with --metrics to enable it"
        )

    slots_response = await read("/slots")
    if slots_response is not None and slots_response.status_code == 200:
        try:
            context = _slot_context(slots_response.json())
        except Exception:             # noqa: BLE001 — unexpected body
            context = {}
        if context:
            report["context"] = context
    elif slots_response is not None:
        report["slots_note"] = (
            f"/slots returned {slots_response.status_code} — context size is "
            "unavailable when the server runs with --no-slots"
        )

    return report


async def agent_context(
    client: httpx.AsyncClient, url: str, timeout: float = DEFAULT_TIMEOUT
) -> Dict[str, Any]:
    """
    Ask one agent how much history it is carrying.

    This is the figure a model server cannot give: the prompt the agent builds
    is its system preamble plus its retained history, and only the agent
    process knows the second part between requests. The agent answers with
    sizes, never content.

    A missing route means an agent installed before ``/context`` existed, which
    is reported as such rather than as a failure.
    """
    root = (url or "").strip().rstrip("/")
    if not root:
        return {"supported": False, "reachable": False,
                "error": "no address for this agent"}
    try:
        response = await client.get(f"{root}/context", timeout=timeout)
    except Exception as exc:      # noqa: BLE001 — a stopped agent is a result
        # Doubles as the board's liveness check: install-time `ready` only says
        # the config was sound, not that anything is listening now.
        return {"supported": False, "reachable": False,
                "error": f"{type(exc).__name__}: {exc}"}
    if response.status_code == 404:
        return {"supported": False, "reachable": True, "error":
                "no /context route — reinstall this agent to report its history"}
    try:
        data = response.json()
    except Exception:             # noqa: BLE001
        return {"supported": False, "reachable": True,
                "error": f"unreadable reply ({response.status_code})"}
    if not isinstance(data, dict):
        return {"supported": False, "reachable": True, "error": "unexpected reply"}
    # The largest session is the one worth showing on a row; the agent sorts
    # them, so this is just the head.
    sessions = data.get("sessions") or []
    data["top"] = sessions[0] if sessions else None
    data["session_count"] = len(sessions)
    data["reachable"] = True
    return data


async def agent_mcp(
    client: httpx.AsyncClient, url: str, timeout: float = DEFAULT_TIMEOUT
) -> Dict[str, Any]:
    """
    One agent's MCP servers, from its ``/mcp`` route (MCP-LIFE-5).

    An agent installed before MCP support has no route; that is reported as
    such, with no servers, rather than as a failure.
    """
    root = (url or "").strip().rstrip("/")
    if not root:
        return {"supported": False, "servers": [], "error": "no address for this agent"}
    try:
        response = await client.get(f"{root}/mcp", timeout=timeout)
    except Exception as exc:      # noqa: BLE001 — a stopped agent is a result
        return {"supported": False, "servers": [], "error": f"{type(exc).__name__}: {exc}"}
    if response.status_code == 404:
        return {"supported": False, "servers": [],
                "error": "no /mcp route — reinstall this agent for MCP support"}
    try:
        data = response.json()
    except Exception:             # noqa: BLE001
        return {"supported": False, "servers": [],
                "error": f"unreadable reply ({response.status_code})"}
    servers = data.get("servers") if isinstance(data, dict) else None
    return {"supported": True, "servers": servers if isinstance(servers, list) else []}


async def mcps_for(
    agents: List[Dict[str, Any]], timeout: float = DEFAULT_TIMEOUT
) -> Dict[int, Dict[str, Any]]:
    """Read every agent's MCP servers at once, keyed by agent id."""
    import asyncio

    if not agents:
        return {}
    async with httpx.AsyncClient() as client:
        reports = await asyncio.gather(*(
            agent_mcp(client, str(agent.get("url") or ""), timeout) for agent in agents
        ))
    return {int(agent["id"]): report for agent, report in zip(agents, reports)}


async def contexts_for(
    agents: List[Dict[str, Any]], timeout: float = DEFAULT_TIMEOUT
) -> Dict[int, Dict[str, Any]]:
    """Read every agent's context at once, keyed by agent id."""
    import asyncio

    if not agents:
        return {}
    async with httpx.AsyncClient() as client:
        reports = await asyncio.gather(*(
            agent_context(client, str(agent.get("url") or ""), timeout)
            for agent in agents
        ))
    return {int(agent["id"]): report for agent, report in zip(agents, reports)}


async def probe_all(
    endpoints: List[str],
    sampler: Optional[Sampler] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> List[Dict[str, Any]]:
    """Read every distinct endpoint at once; one slow server delays only itself."""
    import asyncio

    unique = sorted({e for e in endpoints if e})
    if not unique:
        return []
    async with httpx.AsyncClient() as client:
        return list(await asyncio.gather(
            *(probe(client, endpoint, sampler, timeout) for endpoint in unique)
        ))
