"""OpenAI-compatible chat completions client for the lean runtime."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from declarative_agent_sdk.core.agent_logging import get_logger

logger = get_logger(__name__)


class LLMError(Exception):
    pass


# One model call, not one turn: a turn may make several. 120s was too short for
# a local server asked for anything long — a 27B model writing a document hits
# it every time, and the loop surfaces that as "the agent could not complete
# this request" with the real cause only in the log.
DEFAULT_REQUEST_TIMEOUT = 300.0

# llama.cpp parses tool calls out of the model's own text. When the model is
# asked for a whole document as one escaped JSON string and loses track of the
# escaping part-way, the server cannot read the call back and answers 500.
# Nothing is misconfigured when this happens: the generation ran to length and
# the model simply fumbled a quote.
_MALFORMED_TOOL_CALL = (
    "failed to parse tool call arguments",
    "parse error at line",
)


def _is_malformed_tool_call(detail: str) -> bool:
    lowered = (detail or "").lower()
    return all(marker in lowered for marker in _MALFORMED_TOOL_CALL)


def _malformed_tool_call_message(detail: str) -> str:
    """Say what broke and what to do, rather than forwarding server JSON."""
    column = re.search(r"column (\d+)", detail or "")
    where = (
        f"after about {int(column.group(1)):,} characters"
        if column else "part-way through"
    )
    return (
        f"the model's tool call was malformed: it broke the JSON string {where}, "
        f"so the server could not read the call back. A quantized model asked "
        f"for a whole document in one tool call commonly loses track of the "
        f"escaping — have the agent write smaller sections (a few KB per "
        f"write_file call) rather than one large one. Set "
        f"retry_malformed_tool_calls to try again on this error, bearing in "
        f"mind a retry costs another full generation."
    )


class LeanLLMClient:
    """Minimal chat-completions client (no streaming required for tool loop)."""

    def __init__(
        self,
        *,
        model: str,
        api_key: Optional[str] = None,
        base_url: str = "https://api.openai.com/v1",
        temperature: Optional[float] = 0.7,
        max_tokens: Optional[int] = 4096,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
        retry_malformed_tool_calls: int = 0,
    ) -> None:
        self.model = model
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        # Off by default: a retry is another full-length generation, which on a
        # local model is minutes. Opt in where losing the turn costs more.
        self.retry_malformed_tool_calls = retry_malformed_tool_calls
        self._token_key = "max_tokens"
        self._send_temperature = temperature is not None

    def _payload(
        self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]]
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"model": self.model, "messages": messages}
        if self.max_tokens is not None:
            payload[self._token_key] = self.max_tokens
        if self._send_temperature and self.temperature is not None:
            payload["temperature"] = self.temperature
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return payload

    def _adapt(self, detail: str) -> bool:
        if "max_completion_tokens" in detail and self._token_key == "max_tokens":
            self._token_key = "max_completion_tokens"
            logger.info(f"[llm] switching to max_completion_tokens for {self.model}")
            return True
        if "'temperature'" in detail and self._send_temperature:
            self._send_temperature = False
            logger.info(f"[llm] {self.model} rejects custom temperature; using default")
            return True
        return False

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """One completion round. Returns the assistant message dict."""
        if not self.api_key:
            raise LLMError(
                "no API key configured — set OPENAI_API_KEY or pass api_key="
            )

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/chat/completions"

        last_detail = ""
        tool_call_retries = int(self.retry_malformed_tool_calls or 0)
        for _ in range(3 + tool_call_retries):
            body = json.dumps(self._payload(messages, tools)).encode("utf-8")
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8")
                    data = json.loads(raw)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:800]
                last_detail = detail
                if exc.code == 400 and self._adapt(detail):
                    continue
                if _is_malformed_tool_call(detail):
                    if tool_call_retries > 0:
                        tool_call_retries -= 1
                        logger.warning(
                            "[llm] malformed tool call from the model; retrying "
                            f"({tool_call_retries} left after this)"
                        )
                        continue
                    raise LLMError(_malformed_tool_call_message(detail)) from exc
                raise LLMError(f"HTTP {exc.code} from API: {detail}") from exc
            except TimeoutError as exc:
                # Deliberately not retried: a timeout has already cost the full
                # budget, and three attempts would spend it three times before
                # the caller hears anything.
                #
                # Raised as LLMError so the loop ends the turn with a readable
                # error event. A bare TimeoutError escaped the generator and
                # killed the whole turn, losing both the reason and whatever
                # the agent had already produced.
                raise LLMError(
                    f"the model call timed out after {self.timeout:g}s. A local "
                    f"server that unloads the model between requests pays that "
                    f"reload on every call — raise request_timeout (or "
                    f"endpoint.timeout) in agent.yaml, or keep the model "
                    f"resident."
                ) from exc
            except urllib.error.URLError as exc:
                reason = getattr(exc, "reason", exc)
                if isinstance(reason, TimeoutError):
                    raise LLMError(
                        f"could not reach {self.base_url} within "
                        f"{self.timeout:g}s (connection timed out)"
                    ) from exc
                raise LLMError(f"network error talking to {self.base_url}: {exc}") from exc
            except json.JSONDecodeError as exc:
                raise LLMError(f"could not parse API response: {exc}") from exc
            break
        else:
            raise LLMError(f"API kept rejecting the request: {last_detail}")

        if "error" in data:
            err = data["error"]
            raise LLMError(str(err.get("message", err) if isinstance(err, dict) else err))

        choices = data.get("choices") or []
        if not choices:
            raise LLMError("API returned no choices")

        usage = data.get("usage") or {}
        if usage:
            logger.debug(
                "[llm] tokens prompt=%s completion=%s",
                usage.get("prompt_tokens"),
                usage.get("completion_tokens"),
            )
        return choices[0].get("message") or {}
