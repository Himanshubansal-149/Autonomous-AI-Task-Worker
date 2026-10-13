"""HTTP tool for JSON APIs."""
from __future__ import annotations

import json
import time

import httpx

from .base import Tool, ToolContext, ToolResult, obj

MAX_BODY_CHARS = 12000
RETRIES = 2


def _http_request(ctx: ToolContext, args: dict) -> ToolResult:
    method = args.get("method", "GET").upper()
    headers = {"Accept": "application/json"}
    if args.get("bearer_token"):
        headers["Authorization"] = f"Bearer {args['bearer_token']}"

    # Reads are safe to repeat, so transient failures are retried here rather
    # than spending a model turn on them. Writes are never retried blindly.
    attempts = 1 + (RETRIES if method == "GET" else 0)
    failure = ""
    for attempt in range(1, attempts + 1):
        try:
            response = httpx.request(method, args["url"], headers=headers, json=args.get("json_body"), timeout=15)
        except httpx.HTTPError as exc:
            failure = f"{type(exc).__name__}: {exc}"
        else:
            if response.status_code < 500 or attempt == attempts:
                body = response.text
                try:
                    body = json.dumps(response.json(), indent=1)
                except ValueError:
                    pass
                if len(body) > MAX_BODY_CHARS:
                    body = body[:MAX_BODY_CHARS] + "\n[response truncated]"
                note = f" (after {attempt - 1} retry)" if attempt > 1 else ""
                return ToolResult(f"HTTP {response.status_code}{note}\n{body}", is_error=response.status_code >= 400)
            failure = f"HTTP {response.status_code}"
        if attempt < attempts:
            time.sleep(0.5 * attempt)
    return ToolResult(f"Request failed after {attempts} attempt(s): {failure}", is_error=True)


HTTP_REQUEST = Tool(
    "http_request",
    "Call an HTTP JSON API. Use for systems that document an API. Failed GET requests are retried automatically.",
    obj({
        "url": {"type": "string", "description": "Absolute URL including any query string."},
        "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"], "description": "Default GET."},
        "bearer_token": {"type": "string",
                         "description": "Bearer token for the Authorization header, normally a {{secret:NAME}} placeholder."},
        "json_body": {"type": "object", "description": "JSON body for write requests."},
    }, ["url"]),
    _http_request,
    describe=lambda ctx, a: {"url": a["url"], "http_method": a.get("method", "GET").upper()},
    secret_fields=("bearer_token",),
)
