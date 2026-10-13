"""Web console: hand the worker a task, watch it work, answer its questions,
approve or decline sensitive actions, read the verified result."""
from __future__ import annotations

import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from sandbox import client as sandbox
from worker.agent import Agent, RunResult
from worker.config import Settings
from worker.human import QueueHuman
from worker.llm import make_llm
from worker.trace import Trace

app = FastAPI(title="AI worker console", docs_url=None, redoc_url=None, openapi_url=None)
STATIC = Path(__file__).parent / "static"

# Fault-injection presets offered in the UI.
CHAOS_PRESETS = {
    "none": {},
    "rough_day": {"erp_modal": True, "erp_session_drop": 1, "erp_submit_503": 1},
    "lost_response": {"erp_submit_ghost": 1},
}


@dataclass
class Run:
    id: str
    task: str
    trace: Trace
    human: QueueHuman
    result: RunResult | None = None
    error: str | None = None


RUNS: dict[str, Run] = {}


class StartRequest(BaseModel):
    task: str
    reset: bool = True
    chaos: str = "none"


class HumanResponse(BaseModel):
    request_id: int
    answer: str = ""
    approved: bool = False
    note: str = ""


def _execute(run: Run, settings: Settings) -> None:
    try:
        llm = make_llm(settings)
        run.result = Agent(llm, settings, run.human).run(run.task, run_id=run.id, trace=run.trace)
    except Exception as exc:  # surface crashes in the UI instead of leaving the run spinning
        run.error = f"{type(exc).__name__}: {exc}"


@app.post("/api/runs")
def start_run(req: StartRequest):
    if not req.task.strip():
        raise HTTPException(400, "Enter a task.")
    if req.chaos not in CHAOS_PRESETS:
        raise HTTPException(400, f"Unknown chaos preset {req.chaos!r}")
    if any(r.result is None and r.error is None for r in RUNS.values()):
        raise HTTPException(409, "A task is already running. Wait for it to finish.")
    settings = Settings.from_env()
    if req.reset or req.chaos != "none":
        sandbox.reset(**CHAOS_PRESETS[req.chaos])
    run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
    run_dir = settings.runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    run = Run(run_id, req.task.strip(), Trace(run_dir), QueueHuman())
    RUNS[run_id] = run
    threading.Thread(target=_execute, args=(run, settings), daemon=True, name=f"run-{run_id}").start()
    return {"id": run_id}


def _get(run_id: str) -> Run:
    if run_id not in RUNS:
        raise HTTPException(404, "No such run")
    return RUNS[run_id]


@app.get("/api/runs/{run_id}")
def poll_run(run_id: str, after: int = 0):
    run = _get(run_id)
    return {
        "id": run.id,
        "task": run.task,
        "done": run.result is not None or run.error is not None,
        "error": run.error,
        "events": run.trace.since(after),
        "pending": run.human.pending,
        "result": asdict(run.result) if run.result else None,
    }


@app.post("/api/runs/{run_id}/respond")
def respond(run_id: str, body: HumanResponse):
    if not _get(run_id).human.respond(body.request_id, body.model_dump()):
        raise HTTPException(409, "That request is no longer waiting for an answer.")
    return {"ok": True}


@app.get("/api/runs/{run_id}/files/{path:path}")
def run_file(run_id: str, path: str):
    run_dir = _get(run_id).trace.run_dir.resolve()
    target = (run_dir / path).resolve()
    if run_dir not in target.parents or not target.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(target)


@app.post("/api/reset")
def reset_data():
    """Restore the company systems to their starting data, e.g. before a demo."""
    if any(r.result is None and r.error is None for r in RUNS.values()):
        raise HTTPException(409, "A task is running. Wait for it to finish before resetting.")
    sandbox.reset()
    return {"ok": True}


@app.get("/api/info")
def info():
    settings = Settings.from_env()
    return {"model": settings.model, "sandbox_url": sandbox.base_url(), "chaos_presets": list(CHAOS_PRESETS)}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
