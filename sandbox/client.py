"""Helpers for the CLI, tests and evals: start the sandbox, reset it, read its
true state. The worker never uses these."""
from __future__ import annotations

import threading
import time
from urllib.parse import urlparse

import httpx

from .config import admin_token, base_url


def is_up() -> bool:
    try:
        return httpx.get(base_url() + "/", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def ensure_running() -> bool:
    """Start the sandbox in a background thread unless one is already serving. Returns True if started here."""
    if is_up():
        return False
    import uvicorn

    from .app import app

    url = urlparse(base_url())
    server = uvicorn.Server(uvicorn.Config(app, host=url.hostname or "127.0.0.1", port=url.port or 8700,
                                           log_level="warning"))
    threading.Thread(target=server.run, daemon=True, name="sandbox").start()
    for _ in range(100):
        if is_up():
            return True
        time.sleep(0.1)
    raise RuntimeError(f"The sandbox did not start at {base_url()}")


def reset(**chaos) -> dict:
    """Re-seed all data and the workspace, then apply the given fault-injection settings."""
    r = httpx.post(base_url() + "/admin/reset", json=chaos, headers={"X-Admin-Token": admin_token()}, timeout=30)
    r.raise_for_status()
    return r.json()


def state() -> dict:
    """Ground truth, read straight from the sandbox database."""
    r = httpx.get(base_url() + "/admin/state", headers={"X-Admin-Token": admin_token()}, timeout=30)
    r.raise_for_status()
    return r.json()
