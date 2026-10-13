from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


@dataclass
class Settings:
    provider: str = "anthropic"      # anthropic | gemini | groq | openrouter | openai
    api_key: str = ""                # for non-Anthropic providers; the Anthropic SDK reads its own variable
    base_url: str | None = None
    fallback_models: list[str] = field(default_factory=list)   # tried in order if the main model is unavailable
    model: str = "claude-opus-5-5"
    effort: str = "high"
    refusal_fallback: bool = True
    max_steps: int = 60
    verifier_max_steps: int = 20
    max_verify_rounds: int = 2
    workspace: Path = ROOT / "workspace"
    runs_dir: Path = ROOT / "runs"
    memory_file: Path = ROOT / "memory" / "lessons.json"
    # The worker may only touch these origins. Everything else is denied by policy.
    allowed_origins: list[str] = field(default_factory=lambda: ["http://127.0.0.1:8700"])
    headless: bool = True
    slow_mo_ms: int = 0
    # Named secrets. The model refers to them as {{secret:NAME}} and never sees the values.
    secrets: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(ROOT / ".env")
        env = os.environ
        sandbox_url = env.get("SANDBOX_URL", "http://127.0.0.1:8700").rstrip("/")
        origins = [o.strip().rstrip("/") for o in env.get("WORKER_ALLOWED_ORIGINS", sandbox_url).split(",") if o.strip()]
        from .llm import COMPAT_PROVIDERS, FALLBACK_MODELS

        # An explicit choice wins; otherwise use whichever provider has a key configured.
        provider = env.get("WORKER_PROVIDER", "").lower()
        if not provider:
            keyed = [name for name, (_, var, _) in COMPAT_PROVIDERS.items() if env.get(var)]
            provider = "anthropic" if env.get("ANTHROPIC_API_KEY") or not keyed else keyed[0]
        if provider != "anthropic" and provider not in COMPAT_PROVIDERS:
            raise ValueError(f"Unknown WORKER_PROVIDER {provider!r}. Use anthropic, {', '.join(COMPAT_PROVIDERS)}.")
        base_url, key_var, default_model = COMPAT_PROVIDERS.get(provider, (None, "ANTHROPIC_API_KEY", cls.model))
        return cls(
            provider=provider,
            api_key=env.get(key_var, ""),
            base_url=env.get("LLM_BASE_URL") or base_url,
            fallback_models=([m.strip() for m in env["WORKER_FALLBACK_MODELS"].split(",") if m.strip()]
                             if env.get("WORKER_FALLBACK_MODELS") is not None else FALLBACK_MODELS.get(provider, [])),
            model=env.get("WORKER_MODEL") or default_model,
            effort=env.get("WORKER_EFFORT", cls.effort),
            refusal_fallback=env.get("WORKER_REFUSAL_FALLBACK", "1") != "0",
            max_steps=int(env.get("WORKER_MAX_STEPS", cls.max_steps)),
            workspace=Path(env.get("WORKSPACE_DIR", ROOT / "workspace")).resolve(),
            runs_dir=Path(env.get("WORKER_RUNS_DIR", ROOT / "runs")).resolve(),
            memory_file=Path(env.get("WORKER_MEMORY_FILE", ROOT / "memory" / "lessons.json")).resolve(),
            allowed_origins=origins,
            headless=env.get("WORKER_HEADED", "0") != "1",
            secrets=_read_env_file(Path(env.get("VAULT_FILE", ROOT / "sandbox" / "vault.env"))),
        )
