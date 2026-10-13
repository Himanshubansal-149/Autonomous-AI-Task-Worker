from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

import pytest

# Isolated sandbox for tests: own port, database and workspace. Must be set before sandbox imports.
_TMP = Path(tempfile.mkdtemp(prefix="worker-tests-"))
os.environ["SANDBOX_URL"] = "http://127.0.0.1:8711"
os.environ["SANDBOX_DB"] = str(_TMP / "sandbox.db")
os.environ["WORKSPACE_DIR"] = str(_TMP / "workspace")

from sandbox import client  # noqa: E402
from worker.config import Settings  # noqa: E402

BASE = os.environ["SANDBOX_URL"]


@pytest.fixture(scope="session")
def sandbox_server():
    client.ensure_running()
    return BASE


@pytest.fixture
def sandbox(sandbox_server):
    client.reset()
    return client


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings.from_env()
    s.runs_dir = tmp_path / "runs"
    s.memory_file = tmp_path / "lessons.json"
    s.allowed_origins = [BASE]
    s.headless = True
    return s


def observation(messages: list[dict]) -> str:
    """Text of the most recent tool results in a transcript."""
    content = messages[-1]["content"]
    if isinstance(content, str):
        return content
    return "\n".join(str(b.get("content", b.get("text", ""))) for b in content)


def ref(messages: list[dict], pattern: str) -> int:
    """Ref number of the first element in the latest observation matching `[n] <pattern>`."""
    match = re.search(r"\[(\d+)\] " + pattern, observation(messages))
    assert match, f"no element matching {pattern!r} in:\n{observation(messages)}"
    return int(match.group(1))


def scripted(steps):
    """Build a ScriptedLLM script from a list of callables, one per turn."""
    turn = {"i": 0}

    def script(messages):
        step = steps[min(turn["i"], len(steps) - 1)]
        turn["i"] += 1
        return step(messages)
    return script
