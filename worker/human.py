"""How the worker reaches a person: to ask a question, or to get approval
for an action the policy will not let it take alone."""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Callable, Protocol


@dataclass
class Approval:
    approved: bool
    note: str = ""


class HumanChannel(Protocol):
    def ask(self, question: str, options: list[str] | None = None) -> str: ...
    def approve(self, action: str, reason: str) -> Approval: ...


class ConsoleHuman:
    """Interactive terminal."""

    def ask(self, question: str, options: list[str] | None = None) -> str:
        print(f"\n>>> The worker needs your input:\n    {question}")
        for i, opt in enumerate(options or [], 1):
            print(f"      {i}. {opt}")
        answer = input("    Your answer: ").strip()
        if options and answer.isdigit() and 1 <= int(answer) <= len(options):
            answer = options[int(answer) - 1]
        return answer or "(no answer given)"

    def approve(self, action: str, reason: str) -> Approval:
        print(f"\n>>> Approval needed ({reason}):\n    {action}")
        answer = input("    Approve? [y/N, optional note after a space]: ").strip()
        verdict, _, note = answer.partition(" ")
        return Approval(verdict.lower() in ("y", "yes"), note)


class NoHuman:
    """Unattended runs: nobody is there, so nothing sensitive gets approved."""

    def ask(self, question: str, options: list[str] | None = None) -> str:
        return ("No human is available to answer in this run. Do not guess: finish with status "
                "'blocked' and put this question in needs_from_user.")

    def approve(self, action: str, reason: str) -> Approval:
        return Approval(False, "No human approver is available in this run.")


class ScriptedHuman:
    """Deterministic stand-in for a person, used by tests and evals.

    answers: (regex, answer) pairs tried in order against each question.
    approvals: decides each approval request from its description.
    """

    def __init__(self, answers: list[tuple[str, str]] | None = None,
                 approvals: Callable[[str], bool] | bool = False, decline_note: str = "Not approved."):
        self.answers = answers or []
        self.approvals = approvals
        self.decline_note = decline_note

    def ask(self, question: str, options: list[str] | None = None) -> str:
        for pattern, answer in self.answers:
            if re.search(pattern, question, re.I | re.S):
                return answer
        return "I can't add anything beyond the original request. Use your judgement within company policy."

    def approve(self, action: str, reason: str) -> Approval:
        ok = self.approvals(action) if callable(self.approvals) else bool(self.approvals)
        return Approval(ok, "" if ok else self.decline_note)


class QueueHuman:
    """Web console: park the request, wait for someone to respond in the UI."""

    def __init__(self, timeout_s: float = 900):
        self.pending: dict | None = None
        self._event = threading.Event()
        self._response: dict = {}
        self._timeout = timeout_s
        self._counter = 0

    def _wait(self, request: dict) -> dict | None:
        self._counter += 1
        self._event.clear()
        self.pending = {"id": self._counter, **request}
        answered = self._event.wait(self._timeout)
        self.pending = None
        return self._response if answered else None

    def respond(self, request_id: int, response: dict) -> bool:
        if not self.pending or self.pending["id"] != request_id:
            return False
        self._response = response
        self._event.set()
        return True

    def ask(self, question: str, options: list[str] | None = None) -> str:
        response = self._wait({"kind": "question", "prompt": question, "options": options or []})
        if response is None:
            return NoHuman().ask(question)
        return str(response.get("answer") or "(no answer given)")

    def approve(self, action: str, reason: str) -> Approval:
        response = self._wait({"kind": "approval", "prompt": action, "reason": reason})
        if response is None:
            return Approval(False, "Nobody responded to the approval request in time.")
        return Approval(bool(response.get("approved")), str(response.get("note") or ""))
