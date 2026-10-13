"""Independent verification of the worker's final report.

A second model context that sees the task and the report but none of the
worker's reasoning, and has read-only tools. It re-derives the truth from the
systems themselves. This is what stops "I submitted the form" from being
reported as "the invoice is recorded".
"""
from __future__ import annotations

from .executor import Executor
from .llm import LLM
from .loop import run_loop
from .policy import Policy
from .prompts import VERIFIER_SYSTEM, verifier_message
from .tools import SUBMIT_VERDICT, verifier_registry
from .tools.base import ToolContext, validate


class Verifier:
    def __init__(self, llm: LLM, ctx: ToolContext, today: str):
        self.llm = llm
        self.ctx = ctx
        self.today = today
        self.registry = verifier_registry()
        policy = Policy(ctx.settings.allowed_origins, rules=[], read_only=True)
        self.executor = Executor(self.registry, ctx, policy, role="verifier")

    def verify(self, task: str, report: dict) -> dict:
        """Returns {"verified": bool | None, "checks": [...], "feedback": str}.
        verified is None when verification itself could not be completed."""
        messages = [{"role": "user", "content": verifier_message(task, self.today, report, self.ctx.human_log)}]

        def on_verdict(payload: dict) -> tuple[bool, str]:
            errors = validate(SUBMIT_VERDICT.input_schema, payload)
            return (not errors, "Verdict recorded." if not errors else "Invalid verdict: " + "; ".join(errors))

        outcome = run_loop(
            llm=self.llm, system=VERIFIER_SYSTEM, messages=messages, registry=self.registry,
            executor=self.executor, terminal="submit_verdict", on_terminal=on_verdict,
            max_steps=self.ctx.settings.verifier_max_steps, trace=self.ctx.trace, role="verifier",
        )
        if outcome.payload is None:
            return {"verified": None, "checks": [],
                    "feedback": f"Verification could not be completed ({outcome.reason}): {outcome.detail}"}
        return {"verified": outcome.payload["verified"], "checks": outcome.payload["checks"],
                "feedback": outcome.payload.get("feedback", "")}
