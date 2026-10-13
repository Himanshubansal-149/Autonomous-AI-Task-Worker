"""Browser tools: a real Chromium driven through Playwright.

The model never sees pixels or raw HTML. Each observation is a compact text
rendering of the visible page in which every interactive element carries a
numbered ref, e.g. `[7] button "Save invoice"`. The model acts by ref. Refs are
re-assigned on every observation, so a stale ref fails loudly instead of
clicking the wrong thing.
"""
from __future__ import annotations

import re
from pathlib import Path

from .base import Tool, ToolContext, ToolResult, obj

MAX_PAGE_CHARS = 12000
ACTION_TIMEOUT_MS = 5000

# Walks the visible DOM and renders it as text, tagging interactive elements.
SNAPSHOT_JS = r"""
() => {
  document.querySelectorAll('[data-aw-ref]').forEach(e => e.removeAttribute('data-aw-ref'));
  const BLOCK = new Set(['DIV','P','SECTION','ARTICLE','HEADER','FOOTER','MAIN','NAV','UL','OL','LI','H1','H2','H3',
    'H4','H5','H6','FORM','TABLE','FIELDSET','LABEL','BR','HR','DL','DT','DD','PRE','ASIDE','DIALOG']);
  const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','SVG','HEAD']);
  const out = [];
  let n = 0;
  const q = s => JSON.stringify(String(s || '').replace(/\s+/g, ' ').trim().slice(0, 120));
  const hidden = el => {
    if (el.hidden) return true;
    const s = getComputedStyle(el);
    return s.display === 'none' || s.visibility === 'hidden';
  };
  const tag = el => { el.setAttribute('data-aw-ref', String(++n)); return '[' + n + ']'; };
  const walk = node => {
    if (node.nodeType === 3) {
      out.push(node.textContent.replace(/\s+/g, ' '));
      return;
    }
    if (node.nodeType !== 1) return;
    const el = node, name = el.tagName;
    if (SKIP.has(name) || hidden(el)) return;
    if (name === 'INPUT' && el.type === 'hidden') return;
    const off = el.disabled ? ' (disabled)' : '';
    if (name === 'A' && el.hasAttribute('href')) {
      out.push(' ' + tag(el) + ' link ' + q(el.getAttribute('aria-label') || el.innerText) + ' -> ' + el.getAttribute('href') + ' ');
      return;
    }
    if (name === 'BUTTON' || el.getAttribute('role') === 'button' ||
        (name === 'INPUT' && ['submit', 'button', 'reset'].includes(el.type))) {
      out.push(' ' + tag(el) + ' button ' + q(el.getAttribute('aria-label') || el.innerText || el.value) + off + ' ');
      return;
    }
    if (name === 'INPUT' && (el.type === 'checkbox' || el.type === 'radio')) {
      out.push(' ' + tag(el) + ' ' + el.type + ' name=' + el.name + (el.checked ? ' (checked)' : ' (unchecked)') + off + ' ');
      return;
    }
    if (name === 'INPUT' || name === 'TEXTAREA') {
      const shown = el.type === 'password' ? (el.value ? '(hidden, filled)' : '""') : q(el.value);
      out.push(' ' + tag(el) + ' ' + (name === 'TEXTAREA' ? 'textarea' : 'input type=' + el.type) + ' name=' + el.name +
        ' value=' + shown + (el.placeholder ? ' placeholder=' + q(el.placeholder) : '') + off + ' ');
      return;
    }
    if (name === 'SELECT') {
      const opts = [...el.options].slice(0, 60).map(o => q(o.text));
      const sel = el.selectedOptions[0];
      out.push(' ' + tag(el) + ' select name=' + el.name + ' selected=' + q(sel ? sel.text : '') +
        ' options=[' + opts.join(', ') + ']' + off + ' ');
      return;
    }
    const block = BLOCK.has(name);
    if (block || name === 'TR') out.push('\n');
    if (/^H[1-6]$/.test(name)) out.push('#'.repeat(Number(name[1])) + ' ');
    if (el.getAttribute('role') === 'alert') out.push('(!) ');
    if (el.getAttribute('role') === 'dialog') out.push('--- dialog over the page: ');
    for (const child of el.childNodes) walk(child);
    if (name === 'TD' || name === 'TH') out.push(' | ');
    if (block) out.push('\n');
  };
  walk(document.body);
  const text = out.join('').replace(/[ \t]+/g, ' ').replace(/ ?\n ?/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
  return { title: document.title, url: location.href, text };
}
"""

# Facts about an element the policy needs before the worker acts on it.
DESCRIBE_JS = r"""
el => {
  const form = el.form || el.closest('form');
  const changed = [];
  if (form) for (const c of form.elements) {
    if (!c.name) continue;
    if (c.tagName === 'SELECT') {
      // With no option marked selected in the markup, the browser's default is the first one.
      const initial = Math.max(0, [...c.options].findIndex(o => o.defaultSelected));
      if (c.selectedIndex !== initial) changed.push(c.name);
    }
    else if (c.type === 'checkbox' || c.type === 'radio') { if (c.checked !== c.defaultChecked) changed.push(c.name); }
    else if (c.type !== 'hidden' && c.value !== c.defaultValue) changed.push(c.name);
  }
  const href = el.tagName === 'A' ? (el.href || '') : '';
  const values = [];
  if (form) for (const c of form.elements) {
    if (c.name && !['hidden', 'password', 'submit', 'button'].includes(c.type)) {
      const v = c.tagName === 'SELECT' ? (c.selectedOptions[0] ? c.selectedOptions[0].text : '') : c.value;
      if (v) values.push(c.name + '=' + String(v).slice(0, 60));
    }
  }
  return {
    element: el.tagName.toLowerCase(),
    label: (el.getAttribute('aria-label') || el.innerText || el.value || el.name || '').replace(/\s+/g, ' ').trim().slice(0, 80),
    href,
    form_method: form && !href ? (form.method || 'get').toLowerCase() : '',
    form_action: form && !href ? form.action : '',
    changed_fields: form && !href ? changed.join(',') : '',
    form_values: form && !href ? values.slice(0, 20).join('; ') : '',
  };
}
"""


LOADING = re.compile(r"\b(loading|please wait|fetching)\b", re.I)


def stable_snapshot(page, max_ms: int = 6000, interval_ms: int = 500) -> dict:
    """Read the page until it has settled, so content that renders late (a table filled in by script
    after load) is captured instead of a placeholder. Settled means two readings agree and the page
    is not showing a loading message."""
    snap = page.evaluate(SNAPSHOT_JS)
    waited = 0
    while waited < max_ms:
        page.wait_for_timeout(interval_ms)
        waited += interval_ms
        again = page.evaluate(SNAPSHOT_JS)
        if again["text"] == snap["text"] and not LOADING.search(again["text"]):
            return again
        snap = again
    return snap


class BrowserError(Exception):
    """A browser action failed in a way the model should see and react to."""


class BrowserSession:
    """One browser, one tab, owned by a single run. Started on first use."""

    def __init__(self, headless: bool, downloads_dir: Path, screens_dir: Path, slow_mo_ms: int = 0):
        self.headless = headless
        self.downloads_dir = downloads_dir
        self.screens_dir = screens_dir
        self.slow_mo_ms = slow_mo_ms
        self.page = None
        self._pw = None
        self._browser = None
        self._context = None
        self._downloads: list = []
        self._dialogs: list[str] = []
        self._nav_status: int | None = None
        self._shots = 0
        self.last_screenshot: str | None = None

    # -- lifecycle

    def _ensure(self):
        if self.page is not None:
            return self.page
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless, slow_mo=self.slow_mo_ms)
        self._context = self._browser.new_context(accept_downloads=True, viewport={"width": 1280, "height": 860})
        self._context.set_default_timeout(ACTION_TIMEOUT_MS)
        self.page = self._context.new_page()
        self.page.on("dialog", self._on_dialog)
        self.page.on("download", lambda download: self._downloads.append(download))
        self.page.on("response", self._on_response)
        return self.page

    def close(self) -> None:
        for closer in (getattr(self._context, "close", None), getattr(self._browser, "close", None),
                       getattr(self._pw, "stop", None)):
            try:
                if closer:
                    closer()
            except Exception:
                pass
        self.page = self._context = self._browser = self._pw = None

    def _on_dialog(self, dialog) -> None:
        self._dialogs.append(f"{dialog.type}: {dialog.message}")
        dialog.accept()

    def _on_response(self, response) -> None:
        try:
            if response.request.is_navigation_request() and response.frame == self.page.main_frame:
                self._nav_status = response.status
        except Exception:
            pass

    # -- observation

    def _settle(self) -> None:
        from playwright.sync_api import Error

        for state, timeout in (("domcontentloaded", ACTION_TIMEOUT_MS), ("networkidle", 2500)):
            try:
                self.page.wait_for_load_state(state, timeout=timeout)
            except Error:
                pass

    def screenshot(self) -> str | None:
        try:
            self._shots += 1
            path = self.screens_dir / f"{self._shots:03d}.png"
            self.screens_dir.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(path))
            self.last_screenshot = f"screens/{path.name}"
            return self.last_screenshot
        except Exception:
            return None

    @staticmethod
    def render(snap: dict) -> str:
        text = snap["text"]
        if len(text) > MAX_PAGE_CHARS:
            text = text[:MAX_PAGE_CHARS] + f"\n[page truncated: {len(snap['text']) - MAX_PAGE_CHARS} more characters]"
        return f"Page: {snap['title']}\nURL: {snap['url']}\n\n{text}"

    def snapshot(self, wait_seconds: float = 0) -> str:
        page = self._ensure()
        if wait_seconds:
            page.wait_for_timeout(min(wait_seconds, 10) * 1000)
        self._settle()
        text = self.render(stable_snapshot(page))
        self.screenshot()
        return text

    def view(self, url: str) -> str:
        """Open a URL in a throwaway tab that shares the session's cookies. Read-only."""
        self._ensure()
        page = self._context.new_page()
        try:
            response = page.goto(url, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=4000)
            except Exception:
                pass
            status = response.status if response else None
            note = f"HTTP {status}\n" if status and status >= 400 else ""
            return note + self.render(stable_snapshot(page))
        finally:
            page.close()

    # -- actions

    def _locator(self, ref: int):
        page = self._ensure()
        loc = page.locator(f'[data-aw-ref="{ref}"]')
        if loc.count() != 1:
            raise BrowserError(f"No element with ref [{ref}] on the current page. Refs are re-assigned whenever the "
                               "page is observed; use a ref from the most recent observation.")
        return loc

    def describe_ref(self, ref: int) -> dict:
        info = self._locator(ref).evaluate(DESCRIBE_JS)
        info["page_url"] = self.page.url
        return info

    def act(self, action) -> str:
        """Run one action, then report what changed and what the page looks like now."""
        from playwright.sync_api import Error, TimeoutError

        page = self._ensure()
        before, self._nav_status = page.url, None
        self._dialogs.clear()
        try:
            action(page)
        except TimeoutError as exc:
            raise BrowserError(_explain_timeout(str(exc))) from None
        except Error as exc:
            if "Download is starting" not in str(exc):
                raise BrowserError(str(exc).splitlines()[0]) from None
        page.wait_for_timeout(250)
        self._settle()

        notes = []
        for download in self._downloads:
            self.downloads_dir.mkdir(parents=True, exist_ok=True)
            name = Path(download.suggested_filename).name
            download.save_as(str(self.downloads_dir / name))
            notes.append(f"Downloaded a file. Saved in the workspace as downloads/{name}")
        self._downloads.clear()
        notes += [f"The page showed a browser dialog ({d}); it was accepted." for d in self._dialogs]
        if page.url != before:
            notes.append(f"Navigated: {before} -> {page.url}")
        if self._nav_status and self._nav_status >= 400:
            notes.append(f"WARNING: the server answered this page load with HTTP {self._nav_status}.")
        text = self.render(stable_snapshot(page))
        self.screenshot()
        return "\n".join(notes + ["", text]).strip()

    def goto(self, url: str) -> str:
        return self.act(lambda page: page.goto(url, wait_until="domcontentloaded", timeout=15000))

    def click(self, ref: int) -> str:
        loc = self._locator(ref)
        return self.act(lambda page: loc.click())

    def type(self, ref: int, text: str, submit: bool) -> str:
        loc = self._locator(ref)
        if loc.evaluate("el => el.tagName") == "SELECT":
            raise BrowserError(f"[{ref}] is a dropdown. Use browser_select to choose one of its options.")

        def action(page):
            loc.fill(text)
            if submit:
                loc.press("Enter")
        return self.act(action)

    def select(self, ref: int, option: str) -> str:
        loc = self._locator(ref)
        options = loc.evaluate("el => el.tagName === 'SELECT' ? [...el.options].map(o => o.text.trim()) : null")
        if options is None:
            raise BrowserError(f"[{ref}] is not a dropdown. Use browser_type for text fields.")
        match = next((o for o in options if o.lower() == option.strip().lower()), None)
        if match is None:
            raise BrowserError(f"Option {option!r} is not in this dropdown. Available options: {options}")
        return self.act(lambda page: loc.select_option(label=match))


def _explain_timeout(message: str) -> str:
    for line in message.splitlines():
        if "intercepts pointer events" in line:
            return ("The element could not be clicked because something else is covering it: "
                    f"{line.strip().lstrip('- ')}. A dialog or overlay is probably open; observe the page and deal "
                    "with that first.")
    if "not enabled" in message or "disabled" in message:
        return "The element is disabled and cannot be used right now."
    return "The action timed out: the element did not become ready, or the page did not respond in time."


# ------------------------------------------------------------------ tools

REF = {"type": "integer", "description": "The [n] ref of the element, from the most recent page observation."}


def _guard(fn):
    def handler(ctx: ToolContext, args: dict) -> ToolResult:
        try:
            return ToolResult(fn(ctx, args))
        except BrowserError as exc:
            return ToolResult(f"Browser action failed: {exc}", is_error=True)
    return handler


def _describe_ref(ctx: ToolContext, args: dict) -> dict:
    try:
        return ctx.browser.describe_ref(args["ref"])
    except BrowserError:
        return {}  # the handler will report the bad ref


def _describe_type(ctx: ToolContext, args: dict) -> dict:
    # Typing only has an effect worth gating when it also submits the form.
    return _describe_ref(ctx, args) if args.get("submit") else {}


BROWSER_TOOLS = [
    Tool(
        "browser_navigate",
        "Open a URL in the browser. Returns the resulting page as text: visible content, with each interactive "
        "element marked by a numbered ref like [3].",
        obj({"url": {"type": "string", "description": "Absolute URL."}}, ["url"]),
        _guard(lambda ctx, a: ctx.browser.goto(a["url"])),
        describe=lambda ctx, a: {"url": a["url"]},
    ),
    Tool(
        "browser_snapshot",
        "Re-read the current page without acting on it. Use this when content was still loading, or to confirm the "
        "current state. Optionally wait first.",
        obj({"wait_seconds": {"type": "number", "description": "Seconds to wait before reading (max 10)."}}),
        _guard(lambda ctx, a: ctx.browser.snapshot(a.get("wait_seconds", 0))),
    ),
    Tool(
        "browser_click",
        "Click a link, button, checkbox or radio button by its ref. Returns what changed (navigation, downloads, "
        "dialogs, HTTP errors) and the page as it is afterwards.",
        obj({"ref": REF}, ["ref"]),
        _guard(lambda ctx, a: ctx.browser.click(a["ref"])),
        describe=_describe_ref,
    ),
    Tool(
        "browser_type",
        "Replace the contents of a text field with the given text. Set submit=true to press Enter afterwards.",
        obj({"ref": REF, "text": {"type": "string"},
             "submit": {"type": "boolean", "description": "Press Enter after typing. Default false."}}, ["ref", "text"]),
        _guard(lambda ctx, a: ctx.browser.type(a["ref"], a["text"], a.get("submit", False))),
        describe=_describe_type,
        secret_fields=("text",),
    ),
    Tool(
        "browser_select",
        "Choose an option in a dropdown by its visible text.",
        obj({"ref": REF, "option": {"type": "string", "description": "Visible text of the option to choose."}},
            ["ref", "option"]),
        _guard(lambda ctx, a: ctx.browser.select(a["ref"], a["option"])),
    ),
]

BROWSER_VIEW = Tool(
    "browser_view",
    "Open a URL read-only and return the page as text. Shares the signed-in browser session of the worker, so pages "
    "the worker could see are visible. You cannot click or type.",
    obj({"url": {"type": "string", "description": "Absolute URL."}}, ["url"]),
    _guard(lambda ctx, a: ctx.browser.view(a["url"])),
    describe=lambda ctx, a: {"url": a["url"]},
)
