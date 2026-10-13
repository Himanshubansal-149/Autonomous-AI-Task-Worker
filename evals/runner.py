"""Runs the evaluation suite: for each task, reset the sandbox, let the worker
loose, then check the sandbox's real state."""
from __future__ import annotations

import json
import tempfile
from datetime import datetime
from pathlib import Path

from sandbox import client
from worker.agent import Agent
from worker.config import ROOT, Settings
from worker.llm import make_llm
from worker.trace import console_listener

from .tasks import TASKS, EvalTask

RESULTS_DIR = ROOT / "evals" / "results"


def run_task(task: EvalTask, settings: Settings, verbose: bool) -> dict:
    client.reset(**task.chaos)
    before = client.state()
    llm = make_llm(settings)
    agent = Agent(llm, settings, task.human(), [console_listener] if verbose else [])
    result = agent.run(task.prompt)
    checks = task.check(before, client.state(), result, settings.workspace)
    return {
        "task": task.name, "prompt": task.prompt, "tests": task.tests, "chaos": task.chaos,
        "passed": all(ok for _, ok in checks),
        "checks": [{"check": name, "passed": ok} for name, ok in checks],
        "status": result.status, "verified": result.verified, "summary": result.summary,
        "steps": result.steps, "verify_rounds": result.verify_rounds, "duration_s": result.duration_s,
        "cost_usd": result.usage.get("estimated_cost_usd"), "run_dir": result.run_dir,
    }


def to_markdown(report: dict) -> str:
    rows = report["results"]
    passed = sum(r["passed"] for r in rows)
    lines = [f"# Evaluation results: {passed}/{len(rows)} tasks passed", "",
             f"Model `{report['model']}`, effort `{report['effort']}`, run {report['finished']}.", "",
             "| Task | Result | Checks | Worker status | Steps | Time | Cost |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        ok = sum(c["passed"] for c in r["checks"])
        verified = {True: "verified", False: "not verified", None: "unverified"}[r["verified"]]
        cost = f"${r['cost_usd']:.2f}" if r["cost_usd"] is not None else "n/a"
        lines.append(f"| `{r['task']}` | {'PASS' if r['passed'] else 'FAIL'} | {ok}/{len(r['checks'])} | "
                     f"{r['status']}, {verified} | {r['steps']} | {r['duration_s']:.0f}s | {cost} |")
    for r in rows:
        lines += ["", f"## {r['task']}: {'PASS' if r['passed'] else 'FAIL'}", "", f"> {r['prompt']}", "",
                  f"Probes: {r['tests']}." + (f" Injected faults: `{json.dumps(r['chaos'])}`." if r["chaos"] else ""), ""]
        lines += [f"- [{'x' if c['passed'] else ' '}] {c['check']}" for c in r["checks"]]
        lines += ["", f"Worker's report: {r['summary']}"]
    return "\n".join(lines) + "\n"


def main(only: str | None = None, verbose: bool = False) -> int:
    settings = Settings.from_env()
    # Each eval starts cold: lessons saved by one task must not leak into the next.
    settings.memory_file = Path(tempfile.mkdtemp(prefix="eval-memory-")) / "lessons.json"
    client.ensure_running()

    wanted = set(only.split(",")) if only else None
    tasks = [t for t in TASKS if not wanted or t.name in wanted]
    if wanted and len(tasks) != len(wanted):
        print(f"Unknown task name. Available: {[t.name for t in TASKS]}")
        return 2

    results = []
    for i, task in enumerate(tasks, 1):
        print(f"\n[{i}/{len(tasks)}] {task.name}: {task.prompt}")
        try:
            outcome = run_task(task, settings, verbose)
        except Exception as exc:  # one broken run should not hide the others
            outcome = {"task": task.name, "prompt": task.prompt, "tests": task.tests, "chaos": task.chaos,
                       "passed": False, "checks": [{"check": f"run crashed: {type(exc).__name__}: {exc}", "passed": False}],
                       "status": "crashed", "verified": None, "summary": "", "steps": 0, "verify_rounds": 0,
                       "duration_s": 0, "cost_usd": None, "run_dir": ""}
        results.append(outcome)
        print(f"  => {'PASS' if outcome['passed'] else 'FAIL'}  ({outcome['status']}, {outcome['steps']} steps, "
              f"{outcome['duration_s']}s)")
        for check in outcome["checks"]:
            if not check["passed"]:
                print(f"     failed check: {check['check']}")

    report = {"model": settings.model, "effort": settings.effort,
              "finished": datetime.now().isoformat(timespec="seconds"), "results": results}
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "latest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (RESULTS_DIR / "latest.md").write_text(to_markdown(report), encoding="utf-8")
    passed = sum(r["passed"] for r in results)
    print(f"\n{passed}/{len(results)} tasks passed. Details: {RESULTS_DIR / 'latest.md'}")
    return 0 if passed == len(results) else 1
