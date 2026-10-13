"""The OpenAI-compatible client, tested against a stubbed HTTP endpoint: the
loop's transcript must translate to valid chat messages and back."""
from __future__ import annotations

import json

import httpx
import openai

from worker.config import Settings
from worker.llm import OpenAICompatLLM, to_chat_messages
from worker.tools import worker_registry


def stub_llm(replies: list[dict], seen: list[dict]) -> OpenAICompatLLM:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        message = replies[len(seen) - 1]
        finish = "tool_calls" if message.get("tool_calls") else "stop"
        return httpx.Response(200, json={
            "id": "x", "object": "chat.completion", "created": 0, "model": "stub",
            "choices": [{"index": 0, "message": {"role": "assistant", **message}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

    llm = OpenAICompatLLM("stub", "key")
    llm._client = openai.OpenAI(api_key="key", base_url="http://stub.invalid/v1",
                                http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    return llm


def test_tool_call_round_trip():
    seen: list[dict] = []
    call = {"id": "abc", "type": "function", "extra_content": {"google": {"thought_signature": "sig"}},
            "function": {"name": "read_file", "arguments": '{"path": "handbook.md"}'}}
    llm = stub_llm([{"content": None, "tool_calls": [call]}, {"content": "all done"}], seen)
    tools = worker_registry().specs()
    messages = [{"role": "user", "content": "task"}]

    first = llm.complete("system prompt", messages, tools)
    assert first.stop_reason == "tool_use"
    assert (first.tool_calls[0].name, first.tool_calls[0].input) == ("read_file", {"path": "handbook.md"})

    # What the loop does next: append the turn, then the tool result plus a harness note.
    messages.append({"role": "assistant", "content": first.raw_content})
    messages.append({"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "abc", "content": "file text", "is_error": False},
        {"type": "text", "text": "[harness] 6 steps left"}]})
    second = llm.complete("system prompt", messages, tools)
    assert (second.text, second.tool_calls, second.stop_reason) == ("all done", [], "end_turn")

    sent = seen[1]["messages"]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "tool", "user"]
    assert sent[2]["tool_calls"][0]["extra_content"] == call["extra_content"]   # provider extras are echoed back
    assert sent[3] == {"role": "tool", "tool_call_id": "abc", "content": "file text"}
    assert "additionalProperties" not in json.dumps(seen[0]["tools"])
    assert llm.usage.calls == 2 and llm.usage.input_tokens == 20


def test_malformed_arguments_do_not_crash():
    call = {"id": "abc", "type": "function", "function": {"name": "read_file", "arguments": "{not json"}}
    response = stub_llm([{"content": None, "tool_calls": [call]}], []).complete("s", [{"role": "user", "content": "t"}], [])
    assert "_unparseable_arguments" in response.tool_calls[0].input


def test_plain_string_turns_translate():
    out = to_chat_messages("sys", [{"role": "user", "content": "hi"},
                                   {"role": "assistant", "content": [{"type": "text", "text": "thinking aloud"}]},
                                   {"role": "user", "content": "use your tools"}])
    assert out[2] == {"role": "assistant", "content": "thinking aloud"}
    assert [m["role"] for m in out] == ["system", "user", "assistant", "user"]


def test_provider_is_picked_from_the_key_that_is_set(monkeypatch):
    for var in ("WORKER_PROVIDER", "WORKER_MODEL", "LLM_BASE_URL", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
                "GROQ_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(var, "")
    assert Settings.from_env().provider == "anthropic"
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    s = Settings.from_env()
    assert (s.provider, s.api_key) == ("gemini", "g-key") and "generativelanguage" in s.base_url
    monkeypatch.setenv("WORKER_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "q-key")
    assert Settings.from_env().api_key == "q-key"


def test_overloaded_provider_is_retried_not_fatal(monkeypatch):
    import worker.llm as llm_module
    monkeypatch.setattr(llm_module, "TRANSIENT_WAITS", (0, 0))
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={"error": {"code": 503, "message": "high demand"}})
        return httpx.Response(200, json={
            "id": "x", "object": "chat.completion", "created": 0, "model": "stub",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]})

    llm = OpenAICompatLLM("stub-model", "key")
    llm._client = openai.OpenAI(api_key="key", base_url="http://stub.invalid/v1", max_retries=0,
                                http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert llm.complete("s", [{"role": "user", "content": "t"}], []).text == "ok"
