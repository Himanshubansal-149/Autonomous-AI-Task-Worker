from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from ..config import Settings
    from ..human import HumanChannel
    from ..memory import Memory
    from ..trace import Trace
    from .browser import BrowserSession


@dataclass
class ToolResult:
    content: str
    is_error: bool = False


@dataclass
class ToolContext:
    """Everything a tool may touch during one run."""
    settings: "Settings"
    run_dir: Path
    trace: "Trace"
    human: "HumanChannel"
    memory: "Memory"
    browser: "BrowserSession"
    task: str = ""
    plan: list[dict] = field(default_factory=list)
    human_log: list[dict] = field(default_factory=list)


Handler = Callable[[ToolContext, dict], ToolResult]
# Returns the facts the policy needs to judge a call, before it runs.
Describer = Callable[[ToolContext, dict], dict]


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Handler
    describe: Describer | None = None
    # Arguments in which a {{secret:NAME}} placeholder may be used.
    secret_fields: tuple[str, ...] = ()


class ToolRegistry:
    def __init__(self, tools: list[Tool]):
        self._tools = {t.name: t for t in tools}

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def subset(self, names: list[str]) -> "ToolRegistry":
        return ToolRegistry([self._tools[n] for n in names])

    def specs(self) -> list[dict]:
        # Sorted so the request prefix is byte-stable across turns (prompt caching).
        return [{"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for _, t in sorted(self._tools.items())]


_TYPES: dict[str, type | tuple] = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
                                   "array": list, "object": dict}


def validate(schema: dict, value: Any, path: str = "input") -> list[str]:
    """Check a tool input against the subset of JSON Schema the tools use."""
    errors: list[str] = []
    expected = schema.get("type")
    if expected:
        ok = isinstance(value, _TYPES[expected]) and not (expected in ("integer", "number") and isinstance(value, bool))
        if not ok:
            return [f"{path} must be of type {expected}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path} must be one of {schema['enum']}")
    if expected == "object":
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key} is required")
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                errors += validate(sub, value[key], f"{path}.{key}")
        unknown = set(value) - set(schema.get("properties", {}))
        if unknown and schema.get("additionalProperties") is False:
            errors.append(f"{path} has unknown fields: {sorted(unknown)}")
    if expected == "array" and "items" in schema:
        for i, item in enumerate(value):
            errors += validate(schema["items"], item, f"{path}[{i}]")
    return errors


def obj(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}
