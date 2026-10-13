"""Structured event log for a run: the single source for the console output,
the web UI timeline, the saved trace and the final report."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable

Listener = Callable[[dict], None]


class Trace:
    def __init__(self, run_dir: Path, listeners: list[Listener] | None = None):
        self.run_dir = run_dir
        self.events: list[dict] = []
        self._listeners = list(listeners or [])
        self._lock = threading.Lock()
        self._path = run_dir / "trace.jsonl"
        self._t0 = time.time()

    def emit(self, type: str, **data) -> dict:
        with self._lock:
            event = {"seq": len(self.events), "t": round(time.time() - self._t0, 2), "type": type, **data}
            self.events.append(event)
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        for listener in self._listeners:
            listener(event)
        return event

    def since(self, seq: int) -> list[dict]:
        with self._lock:
            return self.events[seq:]


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def console_listener(event: dict) -> None:
    """Compact human-readable rendering of the event stream for the terminal."""
    t, role = event["type"], event.get("role", "worker")
    tag = "" if role == "worker" else f"[{role}] "
    if t == "llm":
        if event.get("thinking"):
            print(f"  {tag}thinks: {_clip(event['thinking'], 300)}")
        if event.get("text"):
            print(f"  {tag}says: {_clip(event['text'], 300)}")
    elif t == "tool_call":
        print(f"{tag}step {event['step']}: {event['name']}({_clip(json.dumps(event['args'], ensure_ascii=False), 160)})")
    elif t == "tool_result":
        mark = "ERROR" if event["is_error"] else "ok"
        print(f"  {tag}-> {mark}: {_clip(event['content'], 220)}")
    elif t == "plan":
        for item in event["steps"]:
            box = {"done": "[x]", "in_progress": "[>]"}.get(item["status"], "[ ]")
            print(f"  plan {box} {item['step']}")
    elif t == "policy":
        print(f"  policy: {event['decision'].upper()} ({event['reason']})")
    elif t == "human":
        print(f"  human {event['kind']}: {_clip(event['prompt'], 200)} => {_clip(event['response'], 120)}")
    elif t == "verification":
        verdict = {True: "PASSED", False: "NOT CONFIRMED", None: "COULD NOT BE COMPLETED"}[event["verified"]]
        print(f"verification round {event['round']}: {verdict}")
        for check in event.get("checks", []):
            print(f"  {'pass' if check.get('passed') else 'FAIL'}: {_clip(check.get('criterion', ''), 110)}")
    elif t == "run_start":
        print(f"\n=== Task: {event['task']}\n=== Run {event['run_id']} (model {event['model']})\n")
    elif t == "notice":
        print(f"  {tag}notice: {event['message']}")
