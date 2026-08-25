"""Unit tests for the M2 LLM plumbing (llm.py) — no network."""

from __future__ import annotations

import httpx
import pytest

from cubepy.config import Settings
from cubepy.llm import FakeLLM, LLMError, OpenAICompatibleLLM, extract_json

_CONF = Settings(
    llm_base_url="http://test.internal/v1",
    llm_api_key="test-key",
    llm_model="test-model",
    llm_timeout_seconds=1,
)


def _ok(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


# --- FakeLLM -----------------------------------------------------------------


async def test_fake_llm_scripted_responses_popped_in_order() -> None:
    fake = FakeLLM(responses=['{"a": 1}', '{"a": 2}'])
    msgs = [{"role": "user", "content": "q"}]
    assert await fake.chat(msgs) == '{"a": 1}'
    assert await fake.chat(msgs) == '{"a": 2}'
    assert fake.calls == [msgs, msgs]


async def test_fake_llm_responder_wins_over_script() -> None:
    fake = FakeLLM(responses=["unused"], responder=lambda m: m[-1]["content"])
    assert await fake.chat([{"role": "user", "content": "echo"}]) == "echo"


async def test_fake_llm_accepts_async_responder() -> None:
    # run_server's demo responder is async — the seam must await it transparently
    # (caught live by the demo e2e, regression-locked here).
    async def responder(messages: list[dict[str, str]]) -> str:
        return messages[-1]["content"]

    fake = FakeLLM(responder=responder)
    assert await fake.chat([{"role": "user", "content": "echo"}]) == "echo"


async def test_fake_llm_raises_when_exhausted() -> None:
    fake = FakeLLM()
    with pytest.raises(LLMError, match="exhausted"):
        await fake.chat([{"role": "user", "content": "q"}])


# --- extract_json ------------------------------------------------------------


def test_extract_json_plain_object() -> None:
    assert extract_json('{"measures": ["Orders.revenue"]}') == {"measures": ["Orders.revenue"]}


def test_extract_json_fenced_block() -> None:
    fenced = '```json\n{"notAnswerable": true, "reason": "nope"}\n```'
    assert extract_json(fenced)["notAnswerable"] is True


def test_extract_json_bare_fence() -> None:
    assert extract_json('```\n{"a": 1}\n```') == {"a": 1}


@pytest.mark.parametrize("bad", ["[1, 2]", '"text"', "not json", ""])
def test_extract_json_rejects_non_object(bad: str) -> None:
    with pytest.raises(ValueError):
        extract_json(bad)


# --- OpenAICompatibleLLM -----------------------------------------------------


async def test_openai_compat_success_returns_content() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok("hello")

    llm = OpenAICompatibleLLM(_CONF, transport=httpx.MockTransport(handler))
    out = await llm.chat([{"role": "user", "content": "hi"}])
    assert out == "hello"
    assert seen[0].url == "http://test.internal/v1/chat/completions"
    assert seen[0].headers["Authorization"] == "Bearer test-key"


async def test_openai_compat_retries_once_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, text="boom")
        return _ok("second try")

    llm = OpenAICompatibleLLM(_CONF, transport=httpx.MockTransport(handler))
    assert await llm.chat([{"role": "user", "content": "hi"}]) == "second try"
    assert calls["n"] == 2


async def test_openai_compat_gives_up_after_two_failures() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectTimeout("unreachable")

    llm = OpenAICompatibleLLM(_CONF, transport=httpx.MockTransport(handler))
    with pytest.raises(LLMError, match="after retry"):
        await llm.chat([{"role": "user", "content": "hi"}])
    assert calls["n"] == 2


async def test_openai_compat_malformed_body_raises_llm_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"nope": True})

    llm = OpenAICompatibleLLM(_CONF, transport=httpx.MockTransport(handler))
    with pytest.raises(LLMError):
        await llm.chat([{"role": "user", "content": "hi"}])
