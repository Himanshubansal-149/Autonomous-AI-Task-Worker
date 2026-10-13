"""Runs one tool call on the model's behalf: validate, check policy, get
approval if required, inject secrets, execute, redact, record."""
from __future__ import annotations

import json
import re

from .policy import Policy, describe_for_human
from .tools.base import ToolContext, ToolRegistry, ToolResult, validate

SECRET = re.compile(r"\{\{\s*secret:([A-Za-z0-9_]+)\s*\}\}")


class Executor:
    def __init__(self, registry: ToolRegistry, ctx: ToolContext, policy: Policy, role: str = "worker"):
        self.registry = registry
        self.ctx = ctx
        self.policy = policy
        self.role = role

    def execute(self, name: str, args: dict, step: int) -> ToolResult:
        self.ctx.trace.emit("tool_call", role=self.role, step=step, name=name, args=args)
        result = self._run(name, args)
        result.content = self._redact(result.content)
        screenshot = self.ctx.browser.last_screenshot if name.startswith("browser_") else None
        self.ctx.trace.emit("tool_result", role=self.role, step=step, name=name, is_error=result.is_error,
                            content=result.content[:4000], screenshot=screenshot)
        return result

    def _run(self, name: str, args: dict) -> ToolResult:
        tool = self.registry.get(name)
        if tool is None:
            return ToolResult(f"Unknown tool {name!r}. Available tools: {self.registry.names()}", is_error=True)
        errors = validate(tool.input_schema, args)
        if errors:
            return ToolResult("Invalid tool input: " + "; ".join(errors), is_error=True)

        try:
            facts = tool.describe(self.ctx, args) if tool.describe else {}
        except Exception as exc:
            return ToolResult(f"Could not inspect the action before running it: {exc}", is_error=True)
        decision = self.policy.decide(facts)
        if decision.kind != "allow":
            self.ctx.trace.emit("policy", role=self.role, tool=name, decision=decision.kind,
                                reason=decision.reason, rule=decision.rule)
        if decision.kind == "deny":
            return ToolResult(f"Blocked by policy: {decision.reason}. This will not be allowed; do not retry it.",
                              is_error=True)
        if decision.kind == "approve":
            description = describe_for_human(name, args, facts)
            approval = self.ctx.human.approve(description, decision.reason)
            outcome = "approved" if approval.approved else f"declined. {approval.note}".strip()
            self.ctx.human_log.append({"kind": "approval", "prompt": description, "response": outcome})
            self.ctx.trace.emit("human", kind="approval", prompt=description, response=outcome)
            if not approval.approved:
                return ToolResult(
                    f"A human approver declined this action ({decision.reason}). {approval.note} "
                    "The action was NOT performed. Do not look for another way to achieve the same effect.",
                    is_error=True)

        try:
            resolved = self._inject_secrets(args, tool.secret_fields)
        except KeyError as exc:
            return ToolResult(str(exc.args[0]), is_error=True)
        try:
            return tool.handler(self.ctx, resolved)
        except Exception as exc:  # a tool bug must not kill the run: the model can route around it
            return ToolResult(f"The tool failed unexpectedly: {type(exc).__name__}: {exc}", is_error=True)

    def _inject_secrets(self, args: dict, allowed_fields: tuple[str, ...]) -> dict:
        secrets = self.ctx.settings.secrets
        resolved = dict(args)
        for key, value in args.items():
            if not isinstance(value, str) or not SECRET.search(value):
                if SECRET.search(json.dumps(value, default=str)):
                    raise KeyError(f"Secrets cannot be used inside the {key!r} argument.")
                continue
            if key not in allowed_fields:
                raise KeyError(f"Secrets cannot be used in the {key!r} argument of this tool.")
            missing = [n for n in SECRET.findall(value) if n not in secrets]
            if missing:
                raise KeyError(f"Unknown secret(s) {missing}. Available secret names: {sorted(secrets)}")
            resolved[key] = SECRET.sub(lambda m: secrets[m.group(1)], value)
        return resolved

    def _redact(self, text: str) -> str:
        for name, value in self.ctx.settings.secrets.items():
            if value and value in text:
                text = text.replace(value, f"[secret:{name}]")
        return text
