"""End-to-end tests of the harness with a scripted model: real browser, real
sandbox, real policy and verification gates, no API key needed. These pin down
what the harness guarantees regardless of what the model decides."""
from __future__ import annotations

from conftest import BASE, observation, ref, scripted

from worker.agent import Agent
from worker.human import ScriptedHuman
from worker.llm import ScriptedLLM

TOKEN = "{{secret:LEDGERLY_API_TOKEN}}"


def sign_in(system: str, secret: str):
    return [
        lambda m: [("browser_navigate", {"url": f"{BASE}/{system}/login"})],
        lambda m: [("browser_type", {"ref": ref(m, "input type=text name=username"), "text": "ap.clerk"})],
        lambda m: [("browser_type", {"ref": ref(m, "input type=password"), "text": "{{secret:%s}}" % secret,
                                     "submit": True})],
    ]


def finish(status: str, summary: str):
    return lambda m: [("finish", {"status": status, "summary": summary,
                                  "success_criteria": [{"criterion": summary, "evidence": "Ledgerly"}]})]


def verdict(passed):
    def step(m):
        ok = passed(observation(m))
        return [("submit_verdict", {"verified": ok, "feedback": "" if ok else "The record is not in Ledgerly.",
                                    "checks": [{"criterion": "record exists", "passed": ok, "evidence": "API"}]})]
    return step


def test_portal_to_finance_system_end_to_end(sandbox, settings):
    portal = sandbox.state()["portal_invoices"][1]
    assert portal["number"] == "ACM-87954"
    worker = scripted([
        lambda m: [("read_file", {"path": "handbook.md"})],
        *sign_in("portal", "VENDORHUB_PASSWORD"),
        lambda m: [("browser_click", {"ref": ref(m, 'link "Acme Robotics"')})],
        lambda m: [("browser_snapshot", {"wait_seconds": 2})],       # table renders client-side after a delay
        lambda m: [("browser_click", {"ref": ref(m, 'link "ACM-87954"')})],
        lambda m: [("browser_click", {"ref": ref(m, 'link "Download PDF"')})],
        lambda m: [("browser_navigate", {"url": f"{BASE}/erp/invoices/new"})],   # not signed in: lands on login
        lambda m: [("browser_type", {"ref": ref(m, "input type=text name=username"), "text": "ap.clerk"})],
        lambda m: [("browser_type", {"ref": ref(m, "input type=password"),
                                     "text": "{{secret:LEDGERLY_PASSWORD}}", "submit": True})],
        lambda m: [("browser_select", {"ref": ref(m, "select name=vendor_id"), "option": "Acme Robotics"})],
        lambda m: [("browser_type", {"ref": ref(m, "input type=text name=number"), "text": "ACM-87954"})],
        lambda m: [("browser_type", {"ref": ref(m, "input type=text name=issue_date"), "text": portal["issue_date"]})],
        lambda m: [("browser_type", {"ref": ref(m, "input type=text name=due_date"), "text": portal["due_date"]})],
        lambda m: [("browser_type", {"ref": ref(m, "input type=text name=amount"), "text": "$18,450.00"})],
        lambda m: [("browser_click", {"ref": ref(m, 'button "Save invoice"')})],   # rejected: amount format
        lambda m: [("browser_type", {"ref": ref(m, "input type=text name=amount"), "text": "18450.00"})],
        lambda m: [("browser_click", {"ref": ref(m, 'button "Save invoice"')})],
        finish("completed", "ACM-87954 recorded in Ledgerly"),
    ])
    verifier = scripted([
        lambda m: [("http_request", {"url": f"{BASE}/api/v1/invoices?number=ACM-87954", "bearer_token": TOKEN})],
        verdict(lambda obs: '"amount": 18450.0' in obs),
    ])
    result = Agent(ScriptedLLM(worker), settings, verifier_llm=ScriptedLLM(verifier)).run("enter the latest Acme invoice")

    assert (result.status, result.verified) == ("completed", True)
    recorded = [i for i in sandbox.state()["invoices"] if i["number"] == "ACM-87954"]
    assert len(recorded) == 1
    assert (recorded[0]["amount"], recorded[0]["due_date"], recorded[0]["vendor_name"]) == \
        (18450.0, portal["due_date"], "Acme Robotics")
    assert (settings.workspace / "downloads" / "ACM-87954.pdf").exists()
    assert result.screenshots

    trace = (settings.runs_dir / result.run_id / "trace.jsonl").read_text(encoding="utf-8")
    assert "not saved" in trace                      # the format error was observed, not swallowed
    for secret in settings.secrets.values():         # credentials never reach the transcript or the log
        assert secret not in trace


def test_payment_needs_approval_and_a_decline_is_final(sandbox, settings):
    before = sandbox.state()
    invoice = next(i for i in before["invoices"] if i["number"] == "UF-2177")
    worker = scripted([
        *sign_in("erp", "LEDGERLY_PASSWORD"),
        lambda m: [("browser_navigate", {"url": f"{BASE}/erp/invoices/{invoice['id']}/pay"})],
        lambda m: [("browser_click", {"ref": ref(m, 'button "Confirm payment"')})],
        finish("blocked", "Payment was not approved"),
    ])
    verifier = scripted([
        lambda m: [("http_request", {"url": f"{BASE}/api/v1/payments", "bearer_token": TOKEN})],
        verdict(lambda obs: True),
    ])
    human = ScriptedHuman(approvals=False, decline_note="Hold until the dispute is resolved.")
    result = Agent(ScriptedLLM(worker), settings, human, verifier_llm=ScriptedLLM(verifier)).run("pay UF-2177")

    assert sandbox.state()["payments"] == before["payments"]         # the click never reached the server
    assert result.status == "blocked"
    approval = result.human_log[0]
    assert approval["kind"] == "approval" and "declined" in approval["response"]
    assert "/pay" in approval["prompt"] and "Confirm payment" in approval["prompt"]


def test_unverified_claim_is_sent_back_then_reported_honestly(sandbox, settings):
    worker = scripted([
        finish("completed", "ACM-87954 recorded in Ledgerly"),            # claims success without doing anything
        finish("failed", "Could not record ACM-87954"),
    ])
    checks = {"n": 0}

    def judge(obs: str) -> bool:
        checks["n"] += 1
        return checks["n"] > 1      # first report is false, second ("failed") is accurate

    lookup = lambda m: [("http_request", {"url": f"{BASE}/api/v1/invoices?number=ACM-87954", "bearer_token": TOKEN})]
    verifier = scripted([lookup, verdict(judge), lookup, verdict(judge)])
    result = Agent(ScriptedLLM(worker), settings, verifier_llm=ScriptedLLM(verifier)).run("enter the latest Acme invoice")

    assert result.verify_rounds == 2
    assert (result.status, result.verified) == ("failed", True)


def test_worker_that_never_finishes_is_reported_as_failed(sandbox, settings):
    settings.max_steps = 8
    worker = scripted([lambda m: [("list_files", {})]])
    result = Agent(ScriptedLLM(worker), settings).run("do something")
    assert (result.status, result.verified) == ("failed", None)
    assert "step budget" in result.summary
    trace = (settings.runs_dir / result.run_id / "trace.jsonl").read_text(encoding="utf-8")
    assert "repeated list_files detected" in trace


def test_read_only_view_waits_for_late_content(sandbox, settings):
    """The portal fills its invoice table in by script after load; a view must capture it, not 'Loading'."""
    worker = scripted([
        *sign_in("portal", "VENDORHUB_PASSWORD"),
        lambda m: [("browser_snapshot", {})],
        finish("completed", "looked"),
    ])
    seen = {}

    def look(m):
        return [("browser_view", {"url": f"{BASE}/portal/vendors/1"})]

    def judge(m):
        seen["page"] = observation(m)
        return [("submit_verdict", {"verified": True, "checks": [{"criterion": "x", "passed": True, "evidence": "y"}]})]

    Agent(ScriptedLLM(worker), settings, verifier_llm=ScriptedLLM(scripted([look, judge]))).run("look")
    assert "ACM-87954" in seen["page"] and "Loading invoices" not in seen["page"]
