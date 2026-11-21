"""
What a provider offers, for the create-agent form.

Two facts the form can fill in instead of leaving to memory: where a provider's
API lives, and which models that endpoint serves. The second is read live from
``GET {endpoint}/models`` — the OpenAI-compatible listing, which OpenAI and
vLLM both answer — so the list is never a stale copy baked into the board.

Only OpenAI-compatible endpoints are listed. The lean runtime talks
chat-completions to every provider, but adk and deepagent route google and
anthropic through their own clients, where a guessed endpoint would be wrong.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List

import httpx

DEFAULT_TIMEOUT = 8.0

# Where a provider's API lives when the form leaves the endpoint blank. Kept to
# the one the SDK itself defaults to (agents/lean/agent.py), so filling it in
# changes nothing about where the agent connects — it only makes it visible.
PROVIDER_ENDPOINTS: Dict[str, str] = {
    "openai": "https://api.openai.com/v1",
}

# Providers whose endpoint answers the OpenAI-compatible /models listing.
LISTABLE = frozenset({"openai", "vllm"})

# OpenAI's listing mixes in models no chat agent can use. Matched on the id,
# which is all the listing gives.
_NOT_CHAT = re.compile(
    r"embedding|whisper|tts|dall-e|davinci|babbage|moderation|transcribe|"
    r"realtime|audio|image|search|computer-use|codex-mini|sora",
    re.IGNORECASE,
)

# The key the board may hold itself, per provider, when the form has none.
_KEY_ENV = {"openai": "OPENAI_API_KEY", "vllm": "OPENAI_API_KEY"}


def defaults() -> Dict[str, Any]:
    """Static part, shipped with /api/state so the form needs no round trip."""
    return {"endpoints": dict(PROVIDER_ENDPOINTS), "listable": sorted(LISTABLE)}


async def list_models(
    provider: str,
    endpoint: str = "",
    api_key: str = "",
    timeout: float = DEFAULT_TIMEOUT,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Dict[str, Any]:
    """
    Ask an endpoint which models it serves.

    Failures are results, not exceptions: the form shows the reason next to
    the field and the model can still be typed by hand.
    """
    provider = (provider or "").strip().lower()
    if provider not in LISTABLE:
        return {"ok": False, "models": [],
                "error": f"model listing is not available for {provider or 'this provider'}"}
    root = (endpoint or PROVIDER_ENDPOINTS.get(provider, "")).strip().rstrip("/")
    if not root:
        return {"ok": False, "models": [], "error": "enter the endpoint to list its models"}
    key = (api_key or os.environ.get(_KEY_ENV.get(provider, ""), "")).strip()
    if provider == "openai" and not key:
        return {"ok": False, "models": [], "endpoint": root,
                "error": "enter an API key to list models (or set OPENAI_API_KEY "
                         "where the board runs)"}

    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.get(f"{root}/models", headers=headers, timeout=timeout)
    except Exception as exc:      # noqa: BLE001 — unreachable is a result
        return {"ok": False, "models": [], "endpoint": root,
                "error": f"could not reach {root}: {type(exc).__name__}"}
    if response.status_code in (401, 403):
        return {"ok": False, "models": [], "endpoint": root,
                "error": "the endpoint rejected the API key"}
    if response.status_code != 200:
        return {"ok": False, "models": [], "endpoint": root,
                "error": f"{root}/models answered {response.status_code}"}
    try:
        data = response.json().get("data") or []
    except Exception:             # noqa: BLE001
        return {"ok": False, "models": [], "endpoint": root,
                "error": "the endpoint's model list was not JSON"}

    ids = [str(item.get("id")) for item in data if isinstance(item, dict) and item.get("id")]
    return {"ok": True, "models": chat_models(provider, ids), "endpoint": root, "error": ""}


def chat_models(provider: str, ids: List[str]) -> List[str]:
    """Sorted, de-duplicated, and for OpenAI without the non-chat models."""
    unique = sorted({str(i) for i in ids if i})
    if (provider or "").lower() == "openai":
        unique = [i for i in unique if not _NOT_CHAT.search(i)]
    return unique
