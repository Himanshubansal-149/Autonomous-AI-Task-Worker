"""The worker: one task in, one verified result out."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

from .config import Settings
from .executor import Executor
from .human import HumanChannel, NoHuman
from .llm import LLM
from .loop import run_loop
from .memory import Memory
from .policy import Policy
from .prompts import WORKER_SYSTEM, task_message
from .tools import FINISH, worker_registry
from .tools.base import ToolContext, validate
from .tools.browser import BrowserSession
from .trace import Listener, Trace
from .verifier import Verifier


@dataclass
class RunResult:
    run_id: str
    task: str
    status: str                 # completed | partially_completed | blocked | failed
    verified: bool | None       # True: independently confirmed. False: verifier disagreed. None: not checked.
    summary: str
    success_criteria: list[dict] = field(default_factory=list)
    needs_from_user: str = ""
    verification: dict = field(default_factory=dict)
    human_log: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    plan: list[dict] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    steps: int = 0
    verify_rounds: int = 0
    duration_s: float = 0
    usage: dict = field(default_factory=dict)
    run_dir: str = ""

    @property
    def headline(self) -> str:
        check = {True: "verified", False: "NOT verified", None: "unverified"}[self.verified]
        return f"{self.status.replace('_', ' ')} ({check})"


class Agent:
    def __init__(self, llm: LLM, settings: Settings, human: HumanChannel | None = None,
                 listeners: list[Listener] | None = None, verifier_llm: LLM | None = None):
        self.llm = llm
        self.verifier_llm = verifier_llm or llm
        self.settings = settings
        self.human = human or NoHuman()
        self.listeners = listeners or []

    def run(self, task: str, run_id: str | None = None, trace: Trace | None = None) -> RunResult:
        settings = self.settings
        run_id = run_id or f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
        run_dir = settings.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        trace = trace or Trace(run_dir, self.listeners)
        today = date.today().isoformat()
        started = time.time()

        memory = Memory(settings.memory_file)
        browser = BrowserSession(settings.headless, settings.workspace / "downloads", run_dir / "screens",
                                 settings.slow_mo_ms)
        ctx = ToolContext(settings=settings, run_dir=run_dir, trace=trace, human=self.human, memory=memory,
                          browser=browser, task=task)
        registry = worker_registry()
        executor = Executor(registry, ctx, Policy(settings.allowed_origins), role="worker")
        verifier = Verifier(self.verifier_llm, ctx, today)
        verification: dict = {}
        rounds = 0

        def on_finish(report: dict) -> tuple[bool, str]:
            """Gate the worker's report on independent verification."""
            nonlocal verification, rounds
            errors = validate(FINISH.input_schema, report)
            if errors:
                return False, "Invalid report: " + "; ".join(errors)
            rounds += 1
            verification = verifier.verify(task, report)
            if verification["verified"] is None:
                # The check itself broke (e.g. the model provider was unavailable); that says nothing
                # about the work, so try once more before recording the report as unchecked.
                trace.emit("notice", role="verifier", message=f"verification did not complete, retrying: "
                                                              f"{verification['feedback']}")
                verification = verifier.verify(task, report)
            trace.emit("verification", round=rounds, verified=verification["verified"],
                       checks=verification["checks"], feedback=verification["feedback"])
            if verification["verified"] is not False:
                return True, "Report accepted."
            if rounds > settings.max_verify_rounds:
                return True, "Report recorded as not verified."
            return False, (
                "An independent check of the live systems did not confirm your report.\n"
                f"Verifier feedback: {verification['feedback'] or '(none given)'}\n"
                "Failed checks:\n" + "\n".join(
                    f"- {c['criterion']}: {c['evidence']}" for c in verification["checks"] if not c["passed"]) +
                "\nClose the gap if you can. If the verifier is right and it cannot be fixed, call finish again "
                "with a status and summary that match reality.")

        trace.emit("run_start", run_id=run_id, task=task, model=self.llm.model)
        messages = [{"role": "user", "content": task_message(task, today, memory.lessons())}]
        try:
            outcome = run_loop(
                llm=self.llm, system=WORKER_SYSTEM, messages=messages, registry=registry, executor=executor,
                terminal="finish", on_terminal=on_finish, max_steps=settings.max_steps, trace=trace, role="worker",
            )
        finally:
            browser.close()

        if outcome.payload is not None:
            report = outcome.payload
            result = RunResult(
                run_id=run_id, task=task, status=report["status"], verified=verification.get("verified"),
                summary=report["summary"], success_criteria=report["success_criteria"],
                needs_from_user=report.get("needs_from_user", ""), verification=verification,
            )
        else:
            # The loop ended without an accepted report. Say so plainly rather than guess at an outcome.
            result = RunResult(
                run_id=run_id, task=task, status="failed", verified=None,
                summary=(f"The worker stopped before finishing: {outcome.detail} Work may be partly done; "
                         "check the trace and the systems before re-running."),
                verification=verification,
            )
        result.human_log, result.notes, result.plan = ctx.human_log, memory.notes, ctx.plan
        result.screenshots = sorted(p.relative_to(run_dir).as_posix() for p in (run_dir / "screens").glob("*.png"))
        result.steps, result.verify_rounds = outcome.steps, rounds
        result.duration_s = round(time.time() - started, 1)
        result.usage = self.llm.usage.as_dict(self.llm.model)
        result.run_dir = str(run_dir)

        (run_dir / "result.json").write_text(json.dumps(asdict(result), indent=2, default=str), encoding="utf-8")
        (run_dir / "report.md").write_text(render_report(result), encoding="utf-8")
        trace.emit("run_end", status=result.status, verified=result.verified, summary=result.summary)
        return result


def render_report(r: RunResult) -> str:
    lines = [f"# Task report: {r.headline}", "", f"**Task:** {r.task}", "", r.summary, ""]
    if r.needs_from_user:
        lines += [f"**Needs from you:** {r.needs_from_user}", ""]
    if r.success_criteria:
        lines += ["## What the worker claims", ""]
        lines += [f"- {c['criterion']}  \n  Evidence: {c['evidence']}" for c in r.success_criteria] + [""]
    if r.verification:
        lines += ["## Independent verification", ""]
        lines += [f"- {'PASS' if c['passed'] else 'FAIL'}: {c['criterion']}  \n  Observed: {c['evidence']}"
                  for c in r.verification.get("checks", [])]
        if r.verification.get("feedback"):
            lines += ["", f"Verifier feedback: {r.verification['feedback']}"]
        lines += [""]
    if r.human_log:
        lines += ["## People involved", ""] + [f"- {h['kind']}: {h['prompt']} => {h['response']}" for h in r.human_log] + [""]
    if r.notes:
        lines += ["## Facts noted during the run", ""] + [f"- {n}" for n in r.notes] + [""]
    cost = r.usage.get("estimated_cost_usd")
    lines += ["## Run details", "",
              f"- Steps: {r.steps}, verification rounds: {r.verify_rounds}, duration: {r.duration_s}s",
              f"- Model calls: {r.usage.get('calls')}" + (f", estimated cost: ${cost}" if cost is not None else ""),
              f"- Screenshots: {len(r.screenshots)} in `screens/`; full event log in `trace.jsonl`"]
    return "\n".join(lines) + "\n"
