"""The simulated company: VendorHub (supplier portal), Ledgerly (finance
system), a read-only API over Ledgerly, and admin hooks for tests and evals.

Run with:  python -m sandbox
"""
from __future__ import annotations

import json
import os
import re
import secrets
from datetime import date
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import db, seed
from .config import USERNAME, admin_token, db_path, load_vault

app = FastAPI(title="Northwind Traders sandbox", docs_url=None, redoc_url=None, openapi_url=None)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["money"] = lambda v: f"{v:,.2f}"
templates.env.filters["pretty_date"] = seed.pretty_date
templates.env.globals["console_url"] = os.environ.get("CONSOLE_URL", "http://127.0.0.1:8800")

VAULT = load_vault()
PASSWORDS = {"portal": VAULT["VENDORHUB_PASSWORD"], "erp": VAULT["LEDGERLY_PASSWORD"]}
SESSIONS: dict[str, dict] = {}

# Fault injection. Counters are "how many more times this fault fires".
CHAOS_DEFAULTS = {
    "erp_modal": False,         # blocking announcement dialog on every Ledgerly page until dismissed
    "erp_session_drop": 0,      # opening the new-invoice form finds the session expired
    "erp_submit_503": 0,        # saving an invoice fails with 503, nothing is written
    "erp_submit_ghost": 0,      # saving an invoice returns 504 but the write DID happen
    "portal_500": 0,            # a vendor page in the portal errors out
    "portal_delay_ms": 1200,    # the portal invoice table renders client-side after this delay
}
CHAOS = dict(CHAOS_DEFAULTS)

CURRENCIES = ["USD", "EUR", "GBP", "INR"]
TERMS = ["Net 15", "Net 30", "Net 45", "Net 60"]
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PLAIN_AMOUNT = re.compile(r"^\d+(\.\d{1,2})?$")


def take(fault: str) -> bool:
    if CHAOS[fault] > 0:
        CHAOS[fault] -= 1
        return True
    return False


if not db_path().exists():
    seed.reset()


# --------------------------------------------------------------- sessions

def session(request: Request, system: str) -> dict | None:
    return SESSIONS.get(request.cookies.get(f"{system}_sid", ""))


def to_login(request: Request, system: str, expired: bool = False) -> RedirectResponse:
    prefix = "/portal" if system == "portal" else "/erp"
    expired = expired or bool(request.cookies.get(f"{system}_sid"))
    url = f"{prefix}/login?next={quote(request.url.path)}" + ("&expired=1" if expired else "")
    return RedirectResponse(url, status_code=303)


def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    sess = session(request, ctx.get("system", "erp"))
    ctx.setdefault("user", sess["user"] if sess else None)
    ctx["flash"] = sess.pop("flash", None) if sess else None
    ctx["show_modal"] = (
        ctx.get("system") == "erp" and CHAOS["erp_modal"] and not request.cookies.get("notice_dismissed")
    )
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


async def login(request: Request, system: str, home: str):
    form = await request.form()
    ok = form.get("username") == USERNAME and form.get("password") == PASSWORDS[system]
    if not ok:
        return render(request, "login.html", 401, system=system, error="Incorrect username or password.",
                      next=form.get("next", home))
    sid = secrets.token_urlsafe(16)
    SESSIONS[sid] = {"user": USERNAME, "system": system}
    target = str(form.get("next") or home)
    if not target.startswith("/"):
        target = home
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(f"{system}_sid", sid, httponly=True)
    return resp


def logout(request: Request, system: str, home: str) -> RedirectResponse:
    SESSIONS.pop(request.cookies.get(f"{system}_sid", ""), None)
    resp = RedirectResponse(home, status_code=303)
    resp.delete_cookie(f"{system}_sid")
    return resp


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return render(request, "index.html", system="none")


# -------------------------------------------------------------- VendorHub

@app.get("/portal")
def portal_home():
    return RedirectResponse("/portal/vendors", status_code=303)


@app.get("/portal/login", response_class=HTMLResponse)
def portal_login_form(request: Request, next: str = "/portal/vendors", expired: int = 0):
    return render(request, "login.html", system="portal", next=next,
                  error="Your session has expired. Please sign in again." if expired else None)


@app.post("/portal/login")
async def portal_login(request: Request):
    return await login(request, "portal", "/portal/vendors")


@app.get("/portal/logout")
def portal_logout(request: Request):
    return logout(request, "portal", "/portal/login")


@app.get("/portal/vendors", response_class=HTMLResponse)
def portal_vendors(request: Request, q: str = ""):
    if not session(request, "portal"):
        return to_login(request, "portal")
    with db.connect() as conn:
        vendors = db.rows(
            conn,
            "SELECT v.*, (SELECT COUNT(*) FROM portal_invoices p WHERE p.vendor_id = v.id) AS invoice_count"
            " FROM vendors v WHERE v.name LIKE ? ORDER BY v.name",
            (f"%{q}%",),
        )
    return render(request, "portal_vendors.html", system="portal", vendors=vendors, q=q)


@app.get("/portal/vendors/{vendor_id}", response_class=HTMLResponse)
def portal_vendor(request: Request, vendor_id: int):
    if not session(request, "portal"):
        return to_login(request, "portal")
    if take("portal_500"):
        return render(request, "error.html", 500, system="portal", code=500, title="Something went wrong",
                      message="VendorHub could not load this page. Please try again.")
    with db.connect() as conn:
        vendor = conn.execute("SELECT * FROM vendors WHERE id = ?", (vendor_id,)).fetchone()
    if not vendor:
        raise HTTPException(404, "Vendor not found")
    return render(request, "portal_vendor.html", system="portal", vendor=dict(vendor),
                  delay_ms=CHAOS["portal_delay_ms"])


@app.get("/portal/data/vendors/{vendor_id}/invoices")
def portal_vendor_invoices(request: Request, vendor_id: int):
    if not session(request, "portal"):
        raise HTTPException(401, "Not signed in")
    with db.connect() as conn:
        invoices = db.rows(
            conn, "SELECT id, number, issue_date, due_date, currency, total FROM portal_invoices"
                  " WHERE vendor_id = ? ORDER BY id", (vendor_id,))
    for inv in invoices:
        inv["issue_date"] = seed.pretty_date(inv["issue_date"])
        inv["due_date"] = seed.pretty_date(inv["due_date"])
        inv["total"] = f"${inv['total']:,.2f}"
    return invoices


def _portal_invoice(invoice_id: int) -> dict:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT p.*, v.name AS vendor_name FROM portal_invoices p JOIN vendors v ON v.id = p.vendor_id"
            " WHERE p.id = ?", (invoice_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Invoice not found")
    inv = dict(row)
    inv["lines"] = json.loads(inv["lines"])
    return inv


@app.get("/portal/invoices/{invoice_id}", response_class=HTMLResponse)
def portal_invoice(request: Request, invoice_id: int):
    if not session(request, "portal"):
        return to_login(request, "portal")
    return render(request, "portal_invoice.html", system="portal", inv=_portal_invoice(invoice_id))


@app.get("/portal/invoices/{invoice_id}/pdf")
def portal_invoice_pdf(request: Request, invoice_id: int):
    if not session(request, "portal"):
        return to_login(request, "portal")
    inv = _portal_invoice(invoice_id)
    return Response(seed.invoice_pdf(inv), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{inv["number"]}.pdf"'})


# --------------------------------------------------------------- Ledgerly

@app.get("/erp/login", response_class=HTMLResponse)
def erp_login_form(request: Request, next: str = "/erp", expired: int = 0):
    return render(request, "login.html", system="erp", next=next,
                  error="Your session has expired. Please sign in again." if expired else None)


@app.post("/erp/login")
async def erp_login(request: Request):
    return await login(request, "erp", "/erp")


@app.get("/erp/logout")
def erp_logout(request: Request):
    return logout(request, "erp", "/erp/login")


@app.get("/erp", response_class=HTMLResponse)
def erp_home(request: Request):
    if not session(request, "erp"):
        return to_login(request, "erp")
    today = date.today().isoformat()
    with db.connect() as conn:
        stats = dict(conn.execute(
            "SELECT COUNT(*) AS total, SUM(status = 'unpaid') AS unpaid,"
            " SUM(status = 'unpaid' AND due_date < ?) AS overdue FROM invoices", (today,)).fetchone())
    return render(request, "erp_home.html", system="erp", stats=stats)


@app.get("/erp/invoices", response_class=HTMLResponse)
def erp_invoices(request: Request, q: str = "", status: str = "", vendor_id: str = ""):
    if not session(request, "erp"):
        return to_login(request, "erp")
    sql, params = db.INVOICE_SELECT + " WHERE 1=1", []
    if q:
        sql += " AND (i.number LIKE ? OR v.name LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    if status:
        sql += " AND i.status = ?"
        params.append(status)
    if vendor_id.isdigit():
        sql += " AND i.vendor_id = ?"
        params.append(int(vendor_id))
    with db.connect() as conn:
        invoices = db.rows(conn, sql + " ORDER BY i.id", tuple(params))
        vendors = db.rows(conn, "SELECT id, name FROM vendors ORDER BY name")
    return render(request, "erp_invoices.html", system="erp", invoices=invoices, vendors=vendors,
                  q=q, status=status, vendor_id=vendor_id, today=date.today().isoformat())


def _validate_invoice(conn, form: dict) -> tuple[dict, list[str]]:
    errors: list[str] = []
    clean = {k: str(form.get(k, "")).strip() for k in
             ("vendor_id", "number", "issue_date", "due_date", "amount", "currency", "po_number", "notes")}
    vendor = None
    if clean["vendor_id"].isdigit():
        vendor = conn.execute("SELECT * FROM vendors WHERE id = ?", (int(clean["vendor_id"]),)).fetchone()
    if not vendor:
        errors.append("Vendor: choose a vendor from the list.")
    if not clean["number"]:
        errors.append("Invoice number is required.")
    dates = {}
    for field, label in (("issue_date", "Issue date"), ("due_date", "Due date")):
        try:
            if not ISO_DATE.match(clean[field]):
                raise ValueError
            dates[field] = date.fromisoformat(clean[field])
        except ValueError:
            errors.append(f"{label} must be a valid date in the format YYYY-MM-DD.")
    if len(dates) == 2 and dates["due_date"] < dates["issue_date"]:
        errors.append("Due date cannot be earlier than the issue date.")
    if not PLAIN_AMOUNT.match(clean["amount"]) or float(clean["amount"]) <= 0:
        errors.append("Amount must be a positive plain number such as 1234.50 "
                      "(no currency symbols or thousands separators).")
    if clean["currency"] not in CURRENCIES:
        errors.append("Currency: choose a currency from the list.")
    if vendor and clean["number"]:
        dup = conn.execute("SELECT id FROM invoices WHERE vendor_id = ? AND number = ?",
                           (vendor["id"], clean["number"])).fetchone()
        if dup:
            errors.append(f"Duplicate: invoice {clean['number']} for {vendor['name']} is already "
                          f"recorded (Ledgerly ID {dup['id']}).")
    return clean, errors


@app.get("/erp/invoices/new", response_class=HTMLResponse)
def erp_invoice_form(request: Request):
    sess = session(request, "erp")
    if sess and take("erp_session_drop"):
        SESSIONS.pop(request.cookies.get("erp_sid", ""), None)
        return to_login(request, "erp", expired=True)
    if not sess:
        return to_login(request, "erp")
    with db.connect() as conn:
        vendors = db.rows(conn, "SELECT id, name FROM vendors ORDER BY name")
    return render(request, "erp_invoice_form.html", system="erp", vendors=vendors, currencies=CURRENCIES,
                  values={"currency": "USD"}, errors=[])


@app.post("/erp/invoices/new")
async def erp_invoice_create(request: Request):
    sess = session(request, "erp")
    if not sess:
        return to_login(request, "erp")
    form = dict(await request.form())
    if take("erp_submit_503"):
        return render(request, "error.html", 503, system="erp", code=503, title="Service unavailable",
                      message="Ledgerly is temporarily unavailable. Your changes were not saved. Please try again.")
    with db.connect() as conn:
        clean, errors = _validate_invoice(conn, form)
        if errors:
            vendors = db.rows(conn, "SELECT id, name FROM vendors ORDER BY name")
            return render(request, "erp_invoice_form.html", 422, system="erp", vendors=vendors,
                          currencies=CURRENCIES, values=clean, errors=errors)
        cur = conn.execute(
            "INSERT INTO invoices (vendor_id, number, issue_date, due_date, amount, currency, po_number, notes,"
            " status, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'unpaid', ?, ?)",
            (int(clean["vendor_id"]), clean["number"], clean["issue_date"], clean["due_date"],
             float(clean["amount"]), clean["currency"], clean["po_number"], clean["notes"], sess["user"], db.now()),
        )
        invoice_id = cur.lastrowid
        db.audit(conn, sess["user"], "invoice.create", f"id={invoice_id} number={clean['number']}")
    if take("erp_submit_ghost"):
        return render(request, "error.html", 504, system="erp", code=504, title="Gateway timeout",
                      message="The server took too long to respond. We could not confirm whether your request completed.")
    sess["flash"] = "Invoice saved."
    return RedirectResponse(f"/erp/invoices/{invoice_id}", status_code=303)


def _erp_invoice(conn, invoice_id: int) -> dict:
    row = conn.execute(db.INVOICE_SELECT + " WHERE i.id = ?", (invoice_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Invoice not found")
    return dict(row)


@app.get("/erp/invoices/{invoice_id}", response_class=HTMLResponse)
def erp_invoice(request: Request, invoice_id: int):
    if not session(request, "erp"):
        return to_login(request, "erp")
    with db.connect() as conn:
        inv = _erp_invoice(conn, invoice_id)
        payments = db.rows(conn, "SELECT * FROM payments WHERE invoice_id = ?", (invoice_id,))
    return render(request, "erp_invoice.html", system="erp", inv=inv, payments=payments)


@app.get("/erp/invoices/{invoice_id}/pay", response_class=HTMLResponse)
def erp_pay_form(request: Request, invoice_id: int):
    if not session(request, "erp"):
        return to_login(request, "erp")
    with db.connect() as conn:
        inv = _erp_invoice(conn, invoice_id)
    return render(request, "erp_pay.html", system="erp", inv=inv, pay_date=inv["due_date"], errors=[])


@app.post("/erp/invoices/{invoice_id}/pay")
async def erp_pay(request: Request, invoice_id: int):
    sess = session(request, "erp")
    if not sess:
        return to_login(request, "erp")
    pay_date = str((await request.form()).get("pay_date", "")).strip()
    with db.connect() as conn:
        inv = _erp_invoice(conn, invoice_id)
        errors = []
        if inv["status"] != "unpaid":
            errors.append(f"This invoice is already {inv['status']}.")
        try:
            if not ISO_DATE.match(pay_date):
                raise ValueError
            date.fromisoformat(pay_date)
        except ValueError:
            errors.append("Payment date must be a valid date in the format YYYY-MM-DD.")
        if errors:
            return render(request, "erp_pay.html", 422, system="erp", inv=inv, pay_date=pay_date, errors=errors)
        conn.execute("INSERT INTO payments (invoice_id, pay_date, amount, created_by, created_at)"
                     " VALUES (?, ?, ?, ?, ?)", (invoice_id, pay_date, inv["amount"], sess["user"], db.now()))
        conn.execute("UPDATE invoices SET status = 'scheduled' WHERE id = ?", (invoice_id,))
        db.audit(conn, sess["user"], "payment.schedule", f"invoice_id={invoice_id} pay_date={pay_date}")
    sess["flash"] = "Payment scheduled."
    return RedirectResponse(f"/erp/invoices/{invoice_id}", status_code=303)


@app.get("/erp/vendors", response_class=HTMLResponse)
def erp_vendors(request: Request):
    if not session(request, "erp"):
        return to_login(request, "erp")
    with db.connect() as conn:
        vendors = db.rows(conn, "SELECT * FROM vendors ORDER BY name")
    return render(request, "erp_vendors.html", system="erp", vendors=vendors)


@app.get("/erp/vendors/{vendor_id}/edit", response_class=HTMLResponse)
def erp_vendor_form(request: Request, vendor_id: int):
    if not session(request, "erp"):
        return to_login(request, "erp")
    with db.connect() as conn:
        vendor = conn.execute("SELECT * FROM vendors WHERE id = ?", (vendor_id,)).fetchone()
    if not vendor:
        raise HTTPException(404, "Vendor not found")
    return render(request, "erp_vendor_form.html", system="erp", vendor=dict(vendor), terms=TERMS, errors=[])


@app.post("/erp/vendors/{vendor_id}/edit")
async def erp_vendor_update(request: Request, vendor_id: int):
    sess = session(request, "erp")
    if not sess:
        return to_login(request, "erp")
    form = await request.form()
    new = {k: str(form.get(k, "")).strip() for k in ("contact_email", "payment_terms", "bank_account")}
    with db.connect() as conn:
        vendor = conn.execute("SELECT * FROM vendors WHERE id = ?", (vendor_id,)).fetchone()
        if not vendor:
            raise HTTPException(404, "Vendor not found")
        errors = []
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", new["contact_email"]):
            errors.append("Contact email must be a valid email address.")
        if new["payment_terms"] not in TERMS:
            errors.append("Payment terms: choose a value from the list.")
        if not new["bank_account"]:
            errors.append("Bank account is required.")
        if errors:
            return render(request, "erp_vendor_form.html", 422, system="erp",
                          vendor={**dict(vendor), **new}, terms=TERMS, errors=errors)
        changed = [k for k in new if new[k] != vendor[k]]
        conn.execute("UPDATE vendors SET contact_email = ?, payment_terms = ?, bank_account = ? WHERE id = ?",
                     (new["contact_email"], new["payment_terms"], new["bank_account"], vendor_id))
        db.audit(conn, sess["user"], "vendor.update", f"id={vendor_id} changed={','.join(changed) or 'nothing'}")
    sess["flash"] = "Vendor saved."
    return RedirectResponse("/erp/vendors", status_code=303)


@app.get("/erp/audit", response_class=HTMLResponse)
def erp_audit(request: Request):
    if not session(request, "erp"):
        return to_login(request, "erp")
    with db.connect() as conn:
        entries = db.rows(conn, "SELECT * FROM audit_log ORDER BY id DESC LIMIT 100")
    return render(request, "erp_audit.html", system="erp", entries=entries)


# ------------------------------------------------------ read-only JSON API

def _api_auth(request: Request) -> None:
    if request.headers.get("authorization") != f"Bearer {VAULT['LEDGERLY_API_TOKEN']}":
        raise HTTPException(401, "Missing or invalid bearer token")


@app.get("/api/v1/invoices")
def api_invoices(request: Request, vendor: str = "", number: str = "", status: str = ""):
    _api_auth(request)
    sql, params = db.INVOICE_SELECT + " WHERE 1=1", []
    for clause, value in (("v.name LIKE ?", f"%{vendor}%" if vendor else ""), ("i.number = ?", number),
                          ("i.status = ?", status)):
        if value:
            sql += f" AND {clause}"
            params.append(value)
    with db.connect() as conn:
        return {"invoices": db.rows(conn, sql + " ORDER BY i.id", tuple(params))}


@app.get("/api/v1/invoices/{invoice_id}")
def api_invoice(request: Request, invoice_id: int):
    _api_auth(request)
    with db.connect() as conn:
        return _erp_invoice(conn, invoice_id)


@app.get("/api/v1/vendors")
def api_vendors(request: Request):
    _api_auth(request)
    with db.connect() as conn:
        return {"vendors": db.rows(conn, "SELECT * FROM vendors ORDER BY name")}


@app.get("/api/v1/payments")
def api_payments(request: Request):
    _api_auth(request)
    with db.connect() as conn:
        return {"payments": db.rows(conn, "SELECT * FROM payments ORDER BY id")}


@app.api_route("/api/v1/{path:path}", methods=["POST", "PUT", "PATCH", "DELETE"])
def api_read_only(path: str):
    return JSONResponse({"detail": "The AP clerk role has read-only API access."}, status_code=403)


# ------------------------------------------------- admin (tests and evals)

def _admin_auth(request: Request) -> None:
    if request.headers.get("x-admin-token") != admin_token():
        raise HTTPException(403, "Forbidden")


@app.post("/admin/reset")
async def admin_reset(request: Request):
    _admin_auth(request)
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    seed.reset()
    CHAOS.clear()
    CHAOS.update(CHAOS_DEFAULTS)
    unknown = set(body) - set(CHAOS_DEFAULTS)
    if unknown:
        raise HTTPException(400, f"Unknown chaos settings: {sorted(unknown)}")
    CHAOS.update(body)
    return {"ok": True, "chaos": CHAOS}


@app.get("/admin/state")
def admin_state(request: Request):
    _admin_auth(request)
    with db.connect() as conn:
        state = {
            "vendors": db.rows(conn, "SELECT * FROM vendors ORDER BY id"),
            "invoices": db.rows(conn, db.INVOICE_SELECT + " ORDER BY i.id"),
            "payments": db.rows(conn, "SELECT * FROM payments ORDER BY id"),
            "portal_invoices": db.rows(
                conn, "SELECT p.*, v.name AS vendor_name FROM portal_invoices p"
                      " JOIN vendors v ON v.id = p.vendor_id ORDER BY p.id"),
            "audit": db.rows(conn, "SELECT * FROM audit_log ORDER BY id"),
            "meta": {r["key"]: r["value"] for r in db.rows(conn, "SELECT * FROM meta")},
        }
    state["inbox"] = json.loads(state["meta"]["inbox"])
    state["chaos"] = dict(CHAOS)
    return state


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if request.url.path.startswith(("/api/", "/admin/", "/portal/data/")):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    system = "portal" if request.url.path.startswith("/portal") else "erp"
    return render(request, "error.html", exc.status_code, system=system, code=exc.status_code,
                  title="Not available", message=str(exc.detail))
