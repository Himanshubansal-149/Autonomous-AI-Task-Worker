"""The guardrail between what the model decides and what actually happens.

The model is asked to respect company policy, but nothing here relies on it
doing so. Every tool call is described in plain facts (which URL, which form,
which fields changed) and judged by these rules before it runs:

    allow    run it
    approve  pause and ask a human first
    deny     never run it

Rules are data. Tuning them for another company means editing this list, not
the agent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class Rule:
    name: str
    decision: str   # "approve" | "deny"
    reason: str
    where: dict     # fact name -> regex; every entry must match


@dataclass(frozen=True)
class Decision:
    kind: str       # "allow" | "approve" | "deny"
    reason: str = ""
    rule: str = ""


DEFAULT_RULES = [
    Rule("admin-surface", "deny", "administrative endpoints are off limits to the worker",
         {"target_url": r"/admin(/|\?|$)"}),
    Rule("money-movement", "approve", "this action moves money",
         {"form_method": r"^post$", "submit_target": r"\b(pay|payments?|transfers?|wire|refunds?)\b"}),
    Rule("bank-details", "approve", "this action changes bank details",
         {"form_method": r"^post$", "changed_fields": r"(bank|iban|routing|account_?(no|number))"}),
    Rule("destructive", "approve", "this action deletes or cancels a record",
         {"form_method": r"^post$", "submit_target": r"\b(delete|remove|void|cancel)\b"}),
    Rule("api-write", "approve", "this is a write through an API",
         {"http_method": r"^(POST|PUT|PATCH|DELETE)$"}),
]


class Policy:
    def __init__(self, allowed_origins: list[str], rules: list[Rule] | None = None, read_only: bool = False):
        self.allowed_origins = {o.rstrip("/").lower() for o in allowed_origins}
        self.rules = DEFAULT_RULES if rules is None else rules
        self.read_only = read_only

    def _origin_allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        return f"{parsed.scheme}://{parsed.netloc}".lower() in self.allowed_origins

    def decide(self, facts: dict) -> Decision:
        # Where would this action send the browser or the request?
        target = facts.get("url") or facts.get("href") or facts.get("form_action") or ""
        if target and not self._origin_allowed(target):
            return Decision("deny", f"{target} is outside the systems this worker is allowed to use", "origin-allowlist")
        if self.read_only and facts.get("http_method", "GET") != "GET":
            return Decision("deny", "the verifier is read-only", "read-only")

        derived = dict(facts)
        derived["target_url"] = target
        derived["submit_target"] = f"{facts.get('label', '')} {urlparse(facts.get('form_action', '')).path}"
        for rule in self.rules:
            if all(re.search(pattern, str(derived.get(fact, "")), re.I) for fact, pattern in rule.where.items()):
                return Decision(rule.decision, rule.reason, rule.name)
        return Decision("allow")


def describe_for_human(tool: str, args: dict, facts: dict) -> str:
    """One readable sentence for the approver: what is about to happen."""
    if facts.get("form_action"):
        text = f'Submit the form "{facts.get("label") or "(unlabelled)"}" to {urlparse(facts["form_action"]).path}'
        if facts.get("form_values"):
            text += f" with {facts['form_values']}"
        if facts.get("changed_fields"):
            text += f" (fields changed: {facts['changed_fields']})"
        return text + f". Page: {facts.get('page_url', '')}"
    if facts.get("http_method"):
        return f"{facts['http_method']} {facts.get('url')} with body {args.get('json_body')}"
    return f"{tool} {args}"
