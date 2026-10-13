"""Two kinds of memory.

Run notes: facts discovered during this task (IDs, extracted values). They live
in the transcript, and are also collected here so the final report and the
verifier can see them.

Lessons: durable knowledge about how this company's systems behave, written by
the worker at the end of one task and handed to it at the start of the next.
They are hints, not ground truth: the worker is told to re-check them.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

MAX_LESSONS = 30


class Memory:
    def __init__(self, lessons_file: Path):
        self.lessons_file = lessons_file
        self.notes: list[str] = []

    def lessons(self) -> list[dict]:
        if not self.lessons_file.exists():
            return []
        try:
            return json.loads(self.lessons_file.read_text(encoding="utf-8"))[-MAX_LESSONS:]
        except (json.JSONDecodeError, OSError):
            return []

    def remember(self, note: str, long_term: bool, task: str = "") -> None:
        if not long_term:
            self.notes.append(note)
            return
        lessons = self.lessons()
        if any(l["note"] == note for l in lessons):
            return
        lessons.append({"note": note, "learned": datetime.now().isoformat(timespec="seconds"), "task": task[:120]})
        self.lessons_file.parent.mkdir(parents=True, exist_ok=True)
        self.lessons_file.write_text(json.dumps(lessons[-MAX_LESSONS:], indent=2), encoding="utf-8")
