"""Unit tests for the parts that must hold no matter what the model does:
policy decisions, secret handling, workspace confinement, input validation."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from worker.executor import Executor
from worker.human import NoHuman, ScriptedHuman
from worker.memory import Memory
from worker.policy import Policy
from worker.tools import worker_registry
from worker.tools.base import Tool, ToolContext, ToolRegistry, ToolResult, obj, validate
from worker.trace import Trace

ORIGIN = "http://127.0.0.1:8700"
policy = Policy([ORIGIN])


def form(action: str, label: str, changed: str = "") -> dict:
    return {"element": "button", "label": label, "href": "", "form_method": "post",
            "form_action": ORIGIN + action, "changed_fields": changed}


@pytest.mark.parametrize("facts, expected", [
    ({"url": ORIGIN + "/erp/invoices"}, "allow"),
    ({"url": "https://evil.example/collect"}, "deny"),
    ({"url": "file:///etc/passwd"}, "deny"),
    ({"url": ORIGIN + "/admin/reset"}, "deny"),
    ({"href": "https://evil.example/", "label": "Click here"}, "deny"),
    ({"href": ORIGIN + "/erp/invoices/3/pay", "label": "Schedule payment"}, "allow"),   # opening the form is harmless
    (form("/erp/invoices/3/pay", "Confirm payment", "pay_date"), "approve"),
    (form("/erp/invoices/new", "Save invoice", "vendor_id,number,amount"), "allow"),
    (form("/erp/vendors/4/edit", "Save vendor", "contact_email,payment_terms"), "allow"),
    (form("/erp/vendors/4/edit", "Save vendor", "bank_account"), "approve"),
    (form("/erp/records/9", "Delete record"), "approve"),
    ({"url": ORIGIN + "/api/v1/invoices", "http_method": "GET"}, "allow"),
    ({"url": ORIGIN + "/api/v1/invoices", "http_method": "POST"}, "approve"),
    ({}, "allow"),
])
def test_policy_decisions(facts, expected):
    assert policy.decide(facts).kind == expected


def test_read_only_policy_denies_writes():
    read_only = Policy([ORIGIN], rules=[], read_only=True)
    assert read_only.decide({"url": ORIGIN + "/api/v1/x", "http_method": "POST"}).kind == "deny"
    assert read_only.decide({"url": ORIGIN + "/api/v1/x", "http_method": "GET"}).kind == "allow"


@pytest.fixture
def ctx(tmp_path):
    settings = SimpleNamespace(workspace=tmp_path / "ws", secrets={"DB_PASSWORD": "hunter2-very-secret"},
                               allowed_origins=[ORIGIN])
    settings.workspace.mkdir()
    return ToolContext(settings=settings, run_dir=tmp_path, trace=Trace(tmp_path), human=NoHuman(),
                       memory=Memory(tmp_path / "lessons.json"), browser=SimpleNamespace(last_screenshot=None))


def echo_registry(seen: list) -> ToolRegistry:
    def handler(ctx, args):
        seen.append(args)
        return ToolResult(f"sent {args}")
    schema = obj({"password": {"type": "string"}, "note": {"type": "string"}})
    return ToolRegistry([Tool("echo", "test tool", schema, handler, secret_fields=("password",))])


def test_secrets_are_injected_for_the_tool_and_redacted_from_the_model(ctx):
    seen: list = []
    result = Executor(echo_registry(seen), ctx, policy).execute("echo", {"password": "{{secret:DB_PASSWORD}}"}, 1)
    assert seen[0]["password"] == "hunter2-very-secret"
    assert "hunter2" not in result.content and "[secret:DB_PASSWORD]" in result.content


def test_secrets_are_refused_outside_designated_fields(ctx):
    seen: list = []
    executor = Executor(echo_registry(seen), ctx, policy)
    assert executor.execute("echo", {"note": "{{secret:DB_PASSWORD}}"}, 1).is_error
    assert executor.execute("echo", {"password": "{{secret:NOPE}}"}, 1).is_error
    assert seen == []


def test_bad_calls_come_back_as_errors_not_exceptions(ctx):
    executor = Executor(worker_registry(), ctx, policy)
    assert "Unknown tool" in executor.execute("launch_missiles", {}, 1).content
    assert "is required" in executor.execute("read_file", {}, 1).content
    assert "must be of type integer" in executor.execute("browser_click", {"ref": "seven"}, 1).content


def test_files_are_confined_to_the_workspace(ctx):
    executor = Executor(worker_registry(), ctx, policy)
    assert "outside the workspace" in executor.execute("read_file", {"path": "../../secrets.txt"}, 1).content
    assert "outside the workspace" in executor.execute("write_file", {"path": "/tmp/x", "content": "x"}, 1).content
    assert not executor.execute("write_file", {"path": "reports/a.md", "content": "hello"}, 1).is_error
    assert "hello" in executor.execute("read_file", {"path": "reports/a.md"}, 1).content


def test_api_write_waits_for_approval_and_respects_a_decline(ctx):
    ctx.human = ScriptedHuman(approvals=False)
    result = Executor(worker_registry(), ctx, policy).execute(
        "http_request", {"url": ORIGIN + "/api/v1/invoices", "method": "POST", "json_body": {}}, 1)
    assert result.is_error and "declined" in result.content
    assert ctx.human_log[0]["kind"] == "approval"


def test_validate_reports_nested_problems():
    schema = obj({"steps": {"type": "array", "items": obj({"status": {"type": "string", "enum": ["a", "b"]}}, ["status"])}},
                 ["steps"])
    assert validate(schema, {"steps": [{"status": "a"}]}) == []
    assert validate(schema, {"steps": [{"status": "z"}, {}]})
    assert validate(schema, {"steps": "nope"}) == ["input.steps must be of type array"]
    assert validate(schema, {"steps": [], "extra": 1})
