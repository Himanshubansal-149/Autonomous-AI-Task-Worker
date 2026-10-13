"""The observe-decide-act loop, shared by the worker and the verifier.

One iteration: the model reads the transcript and chooses tool calls; each
call is executed and its observation appended; repeat until the model calls
the terminal tool and that call is accepted. The transcript is append-only.
"""
from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from typing import Callable

from .executor import Executor
from .llm import LLM, LLMError
from .tools.base import ToolRegistry
from .trace import Trace

WARN_STEPS_LEFT = 6
REPEAT_LIMIT = 3


@dataclass
class LoopOutcome:
    payload: dict | None    # the accepted terminal call's input, if the loop ended that way
    steps: int
    reason: str             # terminal | budget | no_action | refusal | llm_error
    detail: str = ""


def _result(call_id: str, content: str, is_error: bool = False) -> dict:
    return {"type": "tool_result", "tool_use_id": call_id, "content": content, "is_error": is_error}


def run_loop(*, llm: LLM, system: str, messages: list[dict], registry: ToolRegistry, executor: Executor,
             terminal: str, on_terminal: Callable[[dict], tuple[bool, str]], max_steps: int, trace: Trace,
             role: str) -> LoopOutcome:
    tools = registry.specs()
    recent: deque = deque(maxlen=8)
    idle_turns = 0

    for step in range(1, max_steps + 1):
        try:
            response = llm.complete(system, messages, tools)
        except LLMError as exc:
            return LoopOutcome(None, step, "llm_error", str(exc))
        trace.emit("llm", role=role, step=step, model=llm.model, thinking=response.thinking, text=response.text,
                   stop_reason=response.stop_reason)
        messages.append({"role": "assistant", "content": response.raw_content})

        if response.stop_reason == "refusal":
            return LoopOutcome(None, step, "refusal", "The model declined to continue.")
        if response.stop_reason == "max_tokens":
            # The turn was cut off, so any tool input in it may be incomplete. Do not run it.
            note = "Your last message was cut off before it was complete, so nothing was run. Send the action again."
            content = [_result(c.id, note, True) for c in response.tool_calls] or note
            messages.append({"role": "user", "content": content})
            continue
        if not response.tool_calls:
            idle_turns += 1
            if idle_turns > 2:
                return LoopOutcome(None, step, "no_action", "The model stopped using tools without finishing.")
            messages.append({"role": "user", "content": (
                f"Nobody reads messages written here. Keep working with your tools, and call {terminal} to "
                "deliver your result when you are done.")})
            continue

        results: list[dict] = []
        payload = None
        for call in response.tool_calls:
            if payload is not None:
                results.append(_result(call.id, "Not run: the task had already been closed.", True))
            elif call.name == terminal:
                accepted, feedback = on_terminal(call.input)
                results.append(_result(call.id, feedback, not accepted))
                if accepted:
                    payload = call.input
            else:
                outcome = executor.execute(call.name, call.input, step)
                content = outcome.content
                # Same call, same observation, several times over: the model is stuck.
                signature = (call.name, json.dumps(call.input, sort_keys=True, default=str), content)
                recent.append(signature)
                if recent.count(signature) >= REPEAT_LIMIT:
                    trace.emit("notice", role=role, message=f"repeated {call.name} detected, nudging the model")
                    content += ("\n\n[harness] You have made this exact call several times and got the same result "
                                "each time. Repeating it will not help. Try a different approach, or if you are "
                                "stuck, say so in your final report.")
                results.append(_result(call.id, content, outcome.is_error))

        if payload is not None:
            messages.append({"role": "user", "content": results})
            return LoopOutcome(payload, step, "terminal")
        if max_steps - step == WARN_STEPS_LEFT:
            results.append({"type": "text", "text": (
                f"[harness] You have {WARN_STEPS_LEFT} steps left. Wrap up: confirm what has and has not been "
                f"done, then call {terminal} with an accurate status.")})
        messages.append({"role": "user", "content": results})

    return LoopOutcome(None, max_steps, "budget", f"The step budget of {max_steps} was used up.")
