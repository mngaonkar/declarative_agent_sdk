"""OpenAI-compatible chat completions client for the lean runtime."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from declarative_agent_sdk.core.agent_logging import get_logger

logger = get_logger(__name__)


class LLMError(Exception):
    pass


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
        timeout: float = 120.0,
    ) -> None:
        self.model = model
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
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
        for _ in range(3):
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
                raise LLMError(f"HTTP {exc.code} from API: {detail}") from exc
            except urllib.error.URLError as exc:
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
