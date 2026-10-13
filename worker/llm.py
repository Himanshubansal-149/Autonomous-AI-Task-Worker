"""Model access. The agent loop depends only on the small LLM interface here,
so tests can drive the whole system with a scripted stand-in."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

# USD per million tokens (input, output). Used only for the cost estimate in reports.
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50),
          "claude-fable-5-1": (10.0, 50.0)}


class LLMError(Exception):
    """The model could not be reached or refused the request in a way the run cannot recover from."""


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class LLMResponse:
    raw_content: Any            # appended to the transcript unchanged (thinking blocks must round-trip)
    text: str
    thinking: str
    tool_calls: list[ToolCall]
    stop_reason: str


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    calls: int = 0

    def cost_usd(self, model: str) -> float | None:
        if model not in PRICES:
            return None
        price_in, price_out = PRICES[model]
        tokens_in = self.input_tokens + 0.1 * self.cache_read_tokens + 1.25 * self.cache_write_tokens
        return round((tokens_in * price_in + self.output_tokens * price_out) / 1_000_000, 4)

    def as_dict(self, model: str) -> dict:
        return {**self.__dict__, "estimated_cost_usd": self.cost_usd(model)}


class LLM(Protocol):
    model: str
    usage: Usage

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse: ...


class AnthropicLLM:
    def __init__(self, model: str, effort: str = "high", refusal_fallback: bool = True, max_tokens: int = 16000):
        import anthropic

        self.model = model
        self.effort = effort
        self.refusal_fallback = refusal_fallback
        self.max_tokens = max_tokens
        self.usage = Usage()
        # The SDK retries 408/409/429/5xx and connection errors with backoff.
        self._client = anthropic.Anthropic(max_retries=4)

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        import anthropic

        request = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=messages,
            tools=tools,
            # Summarised thinking is what lets the trace show why each action was chosen.
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": self.effort},
            # The transcript is append-only, so each turn reuses the cached prefix of the last.
            cache_control={"type": "ephemeral"},
        )
        try:
            if self.refusal_fallback:
                # If a safety classifier declines a request, let the API re-run it on a fallback model.
                response = self._client.beta.messages.create(
                    **request, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
            else:
                response = self._client.messages.create(**request)
        except anthropic.AuthenticationError:
            raise LLMError("The Anthropic API key is missing or invalid. Set ANTHROPIC_API_KEY in .env.") from None
        except anthropic.PermissionDeniedError as exc:
            raise LLMError(f"The API key is not permitted to use {self.model}: {exc.message}") from None
        except anthropic.NotFoundError:
            raise LLMError(f"Model {self.model!r} was not found. Check WORKER_MODEL.") from None
        except anthropic.BadRequestError as exc:
            raise LLMError(f"The API rejected the request: {exc.message}") from None
        except anthropic.RateLimitError:
            raise LLMError("Rate limited by the API even after retries. Wait a minute and run again.") from None
        except anthropic.APIStatusError as exc:
            raise LLMError(f"The API returned an error ({exc.status_code}) after retries: {exc.message}") from None
        except anthropic.APIConnectionError:
            raise LLMError("Could not reach the Anthropic API. Check the network connection.") from None

        u = response.usage
        self.usage.calls += 1
        self.usage.input_tokens += u.input_tokens or 0
        self.usage.output_tokens += u.output_tokens or 0
        self.usage.cache_read_tokens += getattr(u, "cache_read_input_tokens", 0) or 0
        self.usage.cache_write_tokens += getattr(u, "cache_creation_input_tokens", 0) or 0

        text, thinking, calls = [], [], []
        for block in response.content:
            if block.type == "text":
                text.append(block.text)
            elif block.type == "thinking" and block.thinking:
                thinking.append(block.thinking)
            elif block.type == "tool_use":
                calls.append(ToolCall(block.id, block.name, dict(block.input)))
        return LLMResponse(response.content, "\n".join(text), "\n".join(thinking), calls, response.stop_reason or "")


# Providers reachable through an OpenAI-compatible chat API: (base URL, key variable, default model).
COMPAT_PROVIDERS = {
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", "GEMINI_API_KEY", "gemini-3.5-flash"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "llama-3.3-70b-versatile"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "google/gemini-3.5-flash"),
    "openai": (None, "OPENAI_API_KEY", "gpt-4.1"),
}
# Models to move to, in order, when the current one's quota is used up or it stops answering.
FALLBACK_MODELS = {
    "gemini": ["gemini-3.7-flash", "gemini-3.8-flash", "gemini-3-flash-preview", "gemini-3.5-flash-lite",
               "gemini-3.1-flash-lite"],
}
MAX_RATE_LIMIT_WAIT_S = 75      # a longer wait than this means a daily quota, not a per-minute one
MAX_SHORT_WAITS = 6
TRANSIENT_WAITS = (5, 15, 30)     # seconds between retries when the provider is overloaded
_BENCHED: dict[str, float] = {}   # model -> time it may be used again; shared by worker and verifier


def _strip_additional_properties(schema):
    """Some providers reject `additionalProperties`. Inputs are validated by the executor anyway."""
    if isinstance(schema, dict):
        return {k: _strip_additional_properties(v) for k, v in schema.items() if k != "additionalProperties"}
    if isinstance(schema, list):
        return [_strip_additional_properties(v) for v in schema]
    return schema


def to_chat_messages(system: str, messages: list[dict]) -> list[dict]:
    """Translate the loop's transcript (text / tool_use / tool_result blocks) into chat-completions messages."""
    out: list[dict] = [{"role": "system", "content": system}]
    for message in messages:
        content = message["content"]
        if isinstance(content, str):
            out.append({"role": message["role"], "content": content})
        elif message["role"] == "assistant":
            text = "\n".join(b["text"] for b in content if b["type"] == "text")
            calls = [b["raw"] for b in content if b["type"] == "tool_use"]
            entry: dict = {"role": "assistant", "content": text or None}
            if calls:
                entry["tool_calls"] = calls
            out.append(entry)
        else:
            # Tool results must directly follow the assistant turn; harness notes go after them.
            for block in content:
                if block["type"] == "tool_result":
                    out.append({"role": "tool", "tool_call_id": block["tool_use_id"], "content": block["content"]})
            notes = "\n".join(b["text"] for b in content if b["type"] == "text")
            if notes:
                out.append({"role": "user", "content": notes})
    return out


class OpenAICompatLLM:
    """Any provider with an OpenAI-compatible chat-completions API (Gemini, Groq, OpenRouter, OpenAI)."""

    def __init__(self, model: str, api_key: str, base_url: str | None = None,
                 fallback_models: list[str] | None = None, max_tokens: int = 8000):
        import openai

        self.models = [model, *[m for m in (fallback_models or []) if m != model]]
        self.max_tokens = max_tokens
        self.usage = Usage()
        self._client = openai.OpenAI(api_key=api_key, base_url=base_url, max_retries=1, timeout=90)

    @property
    def model(self) -> str:
        """The model currently in use: the first configured one that is not benched."""
        now = time.time()
        return next((m for m in self.models if _BENCHED.get(m, 0) <= now), self.models[0])

    def _bench(self, seconds: float) -> bool:
        """Set the current model aside for a while. Returns False when every model is benched."""
        _BENCHED[self.model] = time.time() + seconds
        now = time.time()
        return any(_BENCHED.get(m, 0) <= now for m in self.models)

    def _request(self, request: dict):
        import re

        import openai

        short_waits = transient = 0
        while True:
            model = self.model
            try:
                return self._client.chat.completions.create(model=model, **request)
            except openai.AuthenticationError:
                raise LLMError("The API key was rejected by the provider. Check the key in .env.") from None
            except openai.NotFoundError:
                if not self._bench(24 * 3600):
                    raise LLMError(f"Model {model!r} was not found at this provider. Set WORKER_MODEL in .env.") from None
            except openai.BadRequestError as exc:
                raise LLMError(f"The provider rejected the request: {exc}") from None
            except openai.RateLimitError as exc:
                # A short retry delay is a per-minute limit: wait it out. A long one is a daily
                # quota: waiting is pointless, so bench this model until then and use the next one.
                match = re.search(r"retry in (?:(\d+)h)?(?:(\d+)m)?([\d.]+)s", str(exc))
                delay = (int(match[1] or 0) * 3600 + int(match[2] or 0) * 60 + float(match[3])) if match else 20.0
                short_waits += 1
                if delay <= MAX_RATE_LIMIT_WAIT_S and short_waits <= MAX_SHORT_WAITS:
                    time.sleep(delay + 1)
                elif not self._bench(max(delay, 600)):
                    raise LLMError(
                        "The model provider's quota is used up for every configured model "
                        f"({', '.join(self.models)}). Wait for the quota to reset, or use a key with a higher limit. "
                        f"Last error: {str(exc)[:300]}") from None
                else:
                    short_waits = 0
            except (openai.APIStatusError, openai.APIConnectionError) as exc:
                status = getattr(exc, "status_code", None)
                if status is not None and status < 500:
                    raise LLMError(f"The provider returned an error ({status}): {str(exc)[:300]}") from None
                # Overloaded or unreachable is usually temporary: back off and retry, then try
                # another model for a few minutes, and only give up when nothing answers.
                transient += 1
                if transient <= len(TRANSIENT_WAITS):
                    time.sleep(TRANSIENT_WAITS[transient - 1])
                elif self._bench(300):
                    transient = 0
                else:
                    what = f"error {status}" if status else "no connection"
                    raise LLMError(f"The model provider is not answering ({what}) after several retries. "
                                   "Try again in a few minutes.") from None

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        import json

        functions = [{"type": "function", "function": {
            "name": t["name"], "description": t["description"],
            "parameters": _strip_additional_properties(t["input_schema"])}} for t in tools]
        response = self._request(dict(messages=to_chat_messages(system, messages), tools=functions,
                                      max_tokens=self.max_tokens))
        self.usage.calls += 1
        if response.usage:
            self.usage.input_tokens += response.usage.prompt_tokens or 0
            self.usage.output_tokens += response.usage.completion_tokens or 0
        if not response.choices:
            return LLMResponse([{"type": "text", "text": "(no output)"}], "", "", [], "refusal")

        choice = response.choices[0]
        message = choice.message
        text = message.content or ""
        raw: list[dict] = [{"type": "text", "text": text}] if text else []
        calls: list[ToolCall] = []
        for i, call in enumerate(message.tool_calls or []):
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = None
            if not isinstance(args, dict):
                args = {"_unparseable_arguments": call.function.arguments}   # fails validation, model retries
            call_id = call.id or f"call_{self.usage.calls}_{i}"
            # Keep the provider's own tool-call object so anything it needs echoed back survives the round trip.
            echo = call.model_dump(exclude_none=True)
            echo["id"] = call_id
            raw.append({"type": "tool_use", "id": call_id, "name": call.function.name, "input": args, "raw": echo})
            calls.append(ToolCall(call_id, call.function.name, args))
        stop = {"tool_calls": "tool_use", "length": "max_tokens", "content_filter": "refusal"}.get(
            choice.finish_reason or "", "tool_use" if calls else "end_turn")
        thinking = getattr(message, "reasoning_content", None) or getattr(message, "reasoning", None) or ""
        return LLMResponse(raw or [{"type": "text", "text": "(no output)"}], text, str(thinking), calls, stop)


def make_llm(settings) -> "LLM":
    """Build the model client the settings ask for."""
    if settings.provider == "anthropic":
        return AnthropicLLM(settings.model, settings.effort, settings.refusal_fallback)
    return OpenAICompatLLM(settings.model, settings.api_key, settings.base_url, settings.fallback_models)


Script = Callable[[list[dict]], "str | list[tuple[str, dict]]"]


@dataclass
class ScriptedLLM:
    """A deterministic 'model' for tests. `script` sees the transcript and returns
    either tool calls [(name, input), ...] or plain text."""
    script: Script
    model: str = "scripted"
    usage: Usage = field(default_factory=Usage)

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        self.usage.calls += 1
        out = self.script(messages)
        if isinstance(out, str):
            return LLMResponse([{"type": "text", "text": out}], out, "", [], "end_turn")
        calls = [ToolCall(f"call_{self.usage.calls}_{i}", name, args) for i, (name, args) in enumerate(out)]
        raw = [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.input,
                "raw": {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.input)}}}
               for c in calls]
        return LLMResponse(raw, "", "", calls, "tool_use")
