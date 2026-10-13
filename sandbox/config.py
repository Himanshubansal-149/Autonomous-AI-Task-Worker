from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

USERNAME = "ap.clerk"


def base_url() -> str:
    return os.environ.get("SANDBOX_URL", "http://127.0.0.1:8700").rstrip("/")


def workspace_dir() -> Path:
    return Path(os.environ.get("WORKSPACE_DIR", ROOT / "workspace")).resolve()


def db_path() -> Path:
    return Path(os.environ.get("SANDBOX_DB", ROOT / "sandbox" / "data" / "sandbox.db")).resolve()


def admin_token() -> str:
    return os.environ.get("SANDBOX_ADMIN_TOKEN", "sandbox-admin")


def load_vault() -> dict[str, str]:
    path = Path(os.environ.get("VAULT_FILE", ROOT / "sandbox" / "vault.env"))
    vault: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        vault[key.strip()] = value.strip()
    return vault
