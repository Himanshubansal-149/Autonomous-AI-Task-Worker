from __future__ import annotations

from .base import Tool, ToolContext, ToolRegistry, ToolResult
from .browser import BROWSER_TOOLS, BROWSER_VIEW
from .control import CONTROL_TOOLS, FINISH, SUBMIT_VERDICT
from .files import FILE_TOOLS
from .http import HTTP_REQUEST

__all__ = ["Tool", "ToolContext", "ToolRegistry", "ToolResult", "worker_registry", "verifier_registry",
           "FINISH", "SUBMIT_VERDICT"]

# The verifier may look but not touch: no clicking, typing, writing or asking.
VERIFIER_TOOL_NAMES = ["browser_view", "http_request", "list_files", "read_file", "submit_verdict"]


def worker_registry() -> ToolRegistry:
    return ToolRegistry([*BROWSER_TOOLS, *FILE_TOOLS, HTTP_REQUEST, *CONTROL_TOOLS, FINISH])


def verifier_registry() -> ToolRegistry:
    tools = {t.name: t for t in [*BROWSER_TOOLS, BROWSER_VIEW, *FILE_TOOLS, HTTP_REQUEST, SUBMIT_VERDICT]}
    return ToolRegistry([tools[n] for n in VERIFIER_TOOL_NAMES])
