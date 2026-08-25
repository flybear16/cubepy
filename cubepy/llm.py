"""Thin OpenAI-compatible chat client for the M2 ask layer.

No SDK dependency: one httpx POST to ``{base_url}/chat/completions`` covers
DeepSeek, DashScope (qwen), OpenAI and local vLLM — provider choice is pure
config (``CUBEPY_LLM_*``). ``FakeLLM`` gives tests and key-less demos a
deterministic double.

LLM output is UNTRUSTED input: callers must validate (member whitelist +
``Query.parse``) before anything reaches the executor.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from inspect import isawaitable
from typing import Protocol

import httpx

from cubepy.config import Settings, settings

__all__ = ["ChatModel", "FakeLLM", "LLMError", "OpenAICompatibleLLM", "extract_json"]


class LLMError(Exception):
    """LLM call failed after retry (timeout, HTTP error, malformed body)."""


class ChatModel(Protocol):
    """Minimal chat seam — anything speaking ``messages -> content`` fits."""

    async def chat(self, messages: list[dict[str, str]]) -> str: ...


class OpenAICompatibleLLM:
    """POSTs to ``{base_url}/chat/completions``; one retry on transient failure.

    ``transport`` is an injection seam for tests (``httpx.MockTransport``).
    """

    def __init__(
        self,
        conf: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._conf = conf or settings
        self._transport = transport

    async def chat(self, messages: list[dict[str, str]]) -> str:
        url = self._conf.llm_base_url.rstrip("/") + "/chat/completions"
        payload = {"model": self._conf.llm_model, "messages": messages, "temperature": 0}
        headers = {"Authorization": f"Bearer {self._conf.llm_api_key or ''}"}
        last_exc: Exception | None = None
        for _attempt in (1, 2):  # 1 initial + 1 retry
            try:
                async with httpx.AsyncClient(
                    timeout=self._conf.llm_timeout_seconds, transport=self._transport
                ) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                body = resp.json()
                return str(body["choices"][0]["message"]["content"])
            except (
                httpx.TimeoutException,
                httpx.TransportError,
                httpx.HTTPStatusError,
                KeyError,
                TypeError,
                ValueError,
            ) as exc:
                last_exc = exc
        raise LLMError(f"LLM call failed after retry: {last_exc}") from last_exc


class FakeLLM:
    """Deterministic LLM double for tests and key-less demos.

    Pops scripted ``responses`` in order; a ``responder(messages) -> str``
    callable wins when set. Records every call in ``calls`` for assertions.
    Raises :class:`LLMError` when it runs out of script — a test that hits
    that has an over-chatty conversation under test.
    """

    def __init__(
        self,
        responses: list[str] | None = None,
        responder: Callable[[list[dict[str, str]]], str] | None = None,
    ) -> None:
        self._responses = list(responses or [])
        self._responder = responder
        self.calls: list[list[dict[str, str]]] = []

    async def chat(self, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages)
        if self._responder is not None:
            result = self._responder(messages)
            if isawaitable(result):  # async responders are awaited transparently
                result = await result
            return str(result)
        if not self._responses:
            raise LLMError("FakeLLM exhausted scripted responses")
        return self._responses.pop(0)


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def extract_json(text: str) -> dict:
    """Parse a JSON object out of an LLM reply (tolerates ```json fences).

    Raises ``ValueError`` on anything that is not a JSON object — callers map
    that to the repair-retry path, never to a 500.
    """
    if not isinstance(text, str):
        raise ValueError("LLM reply is not a string")
    candidate = _FENCE_RE.search(text)
    raw = candidate.group(1) if candidate else text
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("LLM reply is not a JSON object")
    return parsed
