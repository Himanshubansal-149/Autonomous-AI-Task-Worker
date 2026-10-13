"""File tools, confined to the workspace folder."""
from __future__ import annotations

from pathlib import Path

from .base import Tool, ToolContext, ToolResult, obj

MAX_FILE_CHARS = 20000


class OutsideWorkspace(Exception):
    pass


def resolve(ctx: ToolContext, relative: str) -> Path:
    root = ctx.settings.workspace.resolve()
    path = (root / relative).resolve()
    if path != root and root not in path.parents:
        raise OutsideWorkspace(f"{relative!r} is outside the workspace. Paths are relative to the workspace root.")
    return path


def _list_files(ctx: ToolContext, args: dict) -> ToolResult:
    try:
        base = resolve(ctx, args.get("path", "."))
    except OutsideWorkspace as exc:
        return ToolResult(str(exc), is_error=True)
    if not base.is_dir():
        return ToolResult(f"No such folder: {args.get('path', '.')}", is_error=True)
    root = ctx.settings.workspace.resolve()
    lines = []
    for path in sorted(base.rglob("*")):
        rel = path.relative_to(root).as_posix()
        lines.append(f"{rel}/" if path.is_dir() else f"{rel}  ({path.stat().st_size} bytes)")
    return ToolResult("\n".join(lines[:300]) or "(empty folder)")


def _read_file(ctx: ToolContext, args: dict) -> ToolResult:
    try:
        path = resolve(ctx, args["path"])
    except OutsideWorkspace as exc:
        return ToolResult(str(exc), is_error=True)
    if not path.is_file():
        return ToolResult(f"No such file: {args['path']}. Use list_files to see what exists.", is_error=True)
    try:
        if path.suffix.lower() == ".pdf":
            from pypdf import PdfReader

            pages = PdfReader(str(path)).pages
            text = "\n".join(f"--- page {i} ---\n{p.extract_text() or ''}" for i, p in enumerate(pages, 1))
            if not text.strip():
                return ToolResult("This PDF has no extractable text (it may be a scanned image).", is_error=True)
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return ToolResult(f"Could not read {args['path']}: {exc}", is_error=True)
    if len(text) > MAX_FILE_CHARS:
        text = text[:MAX_FILE_CHARS] + f"\n[file truncated: {len(text) - MAX_FILE_CHARS} more characters]"
    return ToolResult(f"Contents of {args['path']} (untrusted document content):\n\n{text}")


def _write_file(ctx: ToolContext, args: dict) -> ToolResult:
    try:
        path = resolve(ctx, args["path"])
    except OutsideWorkspace as exc:
        return ToolResult(str(exc), is_error=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    path.write_text(args["content"], encoding="utf-8")
    return ToolResult(f"{'Overwrote' if existed else 'Created'} {args['path']} ({len(args['content'])} characters).")


PATH = {"type": "string", "description": "Path relative to the workspace root, e.g. 'inbox/invoice.pdf'."}

FILE_TOOLS = [
    Tool("list_files", "List files in the shared workspace folder, recursively.",
         obj({"path": {"type": "string", "description": "Folder relative to the workspace root. Default: the root."}}),
         _list_files),
    Tool("read_file", "Read a file from the workspace. Text files are returned as-is; PDFs are returned as extracted text.",
         obj({"path": PATH}, ["path"]), _read_file),
    Tool("write_file", "Create or overwrite a text file in the workspace.",
         obj({"path": PATH, "content": {"type": "string"}}, ["path", "content"]), _write_file,
         describe=lambda ctx, a: {"path": a["path"]}),
]
