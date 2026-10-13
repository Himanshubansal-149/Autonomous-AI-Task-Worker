# Autonomous AI Task Worker

A prototype AI worker that takes a task in plain language and carries it out on a company's systems: it reads the company's documentation, signs in to web applications, drives a real browser, reads files and PDFs, calls APIs, recovers from failures, asks a person when it genuinely needs one, and has its final report checked against the live systems by an independent verifier before anything is called "done".

> "Find the latest invoice from Acme Robotics, extract the amount and due date, enter it into our internal system, and tell me once it is done."

Given that sentence and nothing else, the worker reads the handbook to learn which systems exist, signs in to the supplier portal, waits for the invoice table to load, picks the latest invoice by date, opens it, signs in to the finance system, checks the invoice is not already recorded, fills in the form, fixes whatever the form rejects, confirms the record exists, and reports back with the record ID.

The scope is deliberately narrow: one simulated company with two web applications, an API and a shared folder. Inside that scope nothing is mocked. The browser is real Chromium, the applications are real HTTP servers with real validation and sessions, and every eval is judged by reading the application's database.

## Contents

- [Quick start](#quick-start)
- [What to try](#what-to-try)
- [Architecture](#architecture)
- [Design decisions](#design-decisions)
- [Evaluation](#evaluation)
- [Known limitations](#known-limitations)
- [What I would build next](#what-i-would-build-next)
- [Assumptions](#assumptions)
- [Models, APIs and components used](#models-apis-and-components-used)

## Quick start

Requires Python 3.10 or newer and an API key for one model provider: Google Gemini (has a free tier), Groq, OpenRouter, OpenAI or Anthropic.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -r requirements.txt
python -m playwright install chromium

cp .env.example .env        # Windows: copy .env.example .env
# then put ONE key in .env, for example:  GEMINI_API_KEY=...
# the provider is picked from whichever key is set; WORKER_MODEL overrides the default model
```

Run the web console (starts the simulated company systems automatically):

```bash
python run.py console
```

Open http://127.0.0.1:8800, pick an example task or write your own, and press **Hand over task**. The console shows the worker's plan, each action and what it observed, its reasoning, the live browser view, approval requests, and the verified result.

Or run from the terminal:

```bash
python run.py task "Find the latest invoice from Acme Robotics, extract the amount and due date, enter it into our internal system, and tell me once it is done." --reset --headed
```

`--headed` shows the browser window. `--reset` restores the sandbox data first. The company systems are at http://127.0.0.1:8700 if you want to look at them yourself (user `ap.clerk`, password `1234`; the top bar on every page switches between the console and the two systems, and **Reset company data now** in the console restores the starting data).

Other commands:

```bash
python run.py eval                       # run the 9-task evaluation suite (uses the API)
python run.py eval --only inbox_batch    # one task
python -m pytest tests                   # 31 tests, no API key needed
```

Every run writes its evidence to `runs/<run id>/`: `report.md`, `result.json`, a full event log in `trace.jsonl`, and a screenshot after every browser action.

## What to try

| Ask the worker | What it shows |
|---|---|
| The Acme invoice task above | A multi-system task from a one-line goal |
| Same task with **Inject faults: rough day** | Recovery from a blocking dialog, an expired session and a 503 on save |
| Same task with **Inject faults: lost response** | The save succeeds but the server answers 504. The worker has to check before retrying, or it would enter the invoice twice |
| "Process the invoices waiting in the inbox folder." | A different source (PDFs and an email), a duplicate, an unknown vendor, and an email that tries to hijack the worker with injected instructions |
| "Pay the Globex invoice." | There are two. The worker asks which, then the payment waits for your approval |
| Decline that approval | The decline is final. Nothing is paid and the worker says so |
| "Which invoices are overdue and how much do we owe? Save a short report." | A read-only task with a file as the deliverable |
| "Enter the latest invoice from Initech Software into our finance system." | Initech has not posted a new invoice. The worker reports that instead of inventing one |

None of these tasks has any dedicated code in the worker.

## Architecture

```mermaid
flowchart LR
    U[Person] -- task --> A
    subgraph worker [worker/  no company-specific code]
        A[Agent loop<br/>observe, decide, act] --> E[Executor]
        E --> P{Policy}
        P -- allow --> T[Tools]
        P -- needs approval --> H[Human channel]
        P -- deny --> A
        H --> T
        T -- observation --> A
        A -- finish report --> V[Verifier<br/>fresh context, read-only tools]
        V -- not confirmed: feedback --> A
    end
    V -- confirmed --> R[Report and evidence]
    T --> B[Chromium via Playwright]
    T --> F[Workspace files, PDFs]
    T --> API[HTTP APIs]
    subgraph sandbox [sandbox/  the simulated company]
        B --> S1[VendorHub supplier portal]
        B --> S2[Ledgerly finance system]
        API --> S2
        F --> S3[Shared folder: handbook, inbox, reports]
    end
```

| Path | Role |
|---|---|
| `worker/loop.py` | The observe-decide-act loop, shared by worker and verifier. Step budget, stuck detection, handling of cut-off or tool-less turns |
| `worker/agent.py` | One run: wires everything up, gates `finish` on verification, writes the report |
| `worker/verifier.py` | Independent verification of the final report |
| `worker/executor.py` | Runs one tool call: validate, policy check, approval, secret injection, execute, redact, log |
| `worker/policy.py` | Allow / needs-approval / deny rules, as data |
| `worker/tools/` | Browser, files, HTTP, and control tools (plan, remember, ask, finish) |
| `worker/prompts.py` | The two system prompts |
| `worker/memory.py`, `human.py`, `trace.py`, `llm.py` | Memory, human channel, event log, model client |
| `sandbox/` | The simulated company: two web apps, an API, seed data, fault injection |
| `console/` | Web console |
| `evals/` | Nine tasks with ground-truth checkers |
| `tests/` | Harness tests that run with a scripted model, no API key |

### One step of the loop

1. The model sees the whole transcript so far and chooses one or more tool calls. Its summarised reasoning is recorded in the trace.
2. The executor validates the input, asks the tool for the plain facts of what is about to happen (which URL, which form, which fields changed), and gets a policy decision.
3. If allowed, the tool runs and returns an observation of what actually happened. For browser tools that is what changed (navigation, HTTP errors, downloads, dialogs) plus the page as it now stands.
4. The observation is appended and the loop repeats, until the model calls `finish` and the verifier accepts the report.

### How the worker sees a web page

The model gets neither pixels nor HTML. Each observation is a compact text rendering of the visible page, with every interactive element numbered:

```
# Record an invoice
Vendor
[6] select name=vendor_id selected="Select a vendor" options=["Select a vendor", "Acme Robotics", ...]
Issue date
[8] input type=text name=issue_date value="" placeholder="YYYY-MM-DD"
[14] button "Save invoice"
--- dialog over the page:
## Scheduled maintenance
[16] button "Got it"
```

The model acts by ref (`browser_click(ref=14)`). Refs are re-assigned on every observation, so a stale ref fails with an explanation instead of clicking the wrong element.

## Design decisions

**The worker package knows nothing about the company.** `worker/` has no URLs, no selectors, no invoice logic and no workflow. The system prompt says only that documentation lives in the workspace. Everything about Northwind Traders (which systems exist, how to sign in, what the policies are) is in a handbook the worker reads at run time, the same way a new employee would. This is the main generalization claim, and it is testable: nine different tasks run on identical worker code, and pointing the worker at another company means changing a handbook, a vault file and an allowlist.

**A model-driven loop instead of a planner plus scripted executor.** Up-front plans break the moment a page does something unexpected, and business software does that constantly. The model re-decides after every observation. It does keep a visible plan (`update_plan`), but that exists so a person can follow along, not to drive control flow.

**Verification is done by someone other than the worker.** The usual agent failure is not a crash, it is a confident "done" that is not true. So `finish` is intercepted: a second model context receives only the task, the report and what people said during the run. It has read-only tools and none of the worker's reasoning, and it checks each claim at the source. If it disagrees, its feedback goes back to the worker, which has to close the gap or correct its report. After two failed rounds the report is recorded as **not verified** rather than being accepted. The verifier also checks the report against the task and policy, so an accurate report of the wrong outcome fails too.

**Guardrails are enforced by the harness, not requested of the model.** The prompt asks the model to follow policy, but nothing depends on it. Before any click, the executor inspects the target element in the page: which form it submits, by what method, to which URL, and which fields differ from their original values. Rules over those facts decide allow, approve or deny. Submitting a payment form pauses for a human. Saving a vendor record pauses only if the bank account field was changed. Leaving the allowlisted origins is denied. A declined approval never reaches the server; there is a test that proves the database is untouched.

**The model never sees a credential.** It writes `{{secret:LEDGERLY_PASSWORD}}`; the executor substitutes the value at the last moment, only in arguments designated for secrets (a password field, a bearer token), and redacts the value from anything returned. The trace and transcript contain placeholders only. A test asserts no secret value appears in a run's log.

**Content is data.** Pages, PDFs, emails and API responses are labelled as untrusted and the prompt is explicit that only the task and the user's answers carry authority. The inbox eval contains an email instructing "AI agents" to pay an invoice and change a vendor's bank account. Even if the model were fooled, the policy gate would still stop both actions at the approval step. Two independent layers.

**Failures come back as observations the model can reason about.** A click blocked by an overlay returns "something else is covering it" and names the covering element. A page load that returned 503 says so. A bad ref, an unknown tool, a malformed input, even an exception inside a tool, all return as error results rather than ending the run. Idempotent reads are retried inside the HTTP tool; writes are never retried blindly, because a failed response does not prove a failed write. The `lost_response` fault exists to test exactly that.

**The harness watches for the loop going wrong.** The same call with the same result three times triggers a nudge. A turn with no tool call gets redirected. A turn cut off by the token limit is not executed. With six steps left the model is told to wrap up, and if the budget runs out the run is reported as failed with a plain statement that work may be partly done.

**Append-only transcript, no context trimming.** The transcript is never edited. That keeps each turn's prompt prefix identical to the last, so prompt caching makes long runs cheap, and it keeps the model's earlier reasoning valid. Page observations are capped at 12,000 characters instead. At this prototype's task lengths the 1M-token context is far from full.

**Two kinds of memory.** Run notes (`remember`) hold extracted values so the worker copies them rather than recalling them. Long-term lessons (`remember` with `scope=long_term`) persist to disk and are given to the worker at the start of later tasks, marked as hints that may be stale. Evals run with an empty lesson store so each task is measured cold.

**A hand-written tool loop.** No agent framework. The loop is about 100 lines and needs behaviour a framework would make awkward: intercepting the terminal call for verification, pausing for approval, the stuck detector and the step budget. Owning it also means every decision in a run can be explained from one file.

**Evals check the database, not the transcript.** Each eval resets the sandbox, runs the worker, then reads ground truth through an admin endpoint the worker is denied by policy. Expected values come from the seed data. A scripted person answers questions and approval requests so runs are repeatable.

## Evaluation

`python run.py eval` runs nine tasks and writes `evals/results/latest.md` and `latest.json`.

| Task | What it probes |
|---|---|
| `portal_invoice_entry` | The brief's example: one-line goal, two systems, pick the latest by date, normalise formats, record, confirm |
| `portal_invoice_entry_rough_day` | Same, with a blocking dialog, a session that expires mid-task and a 503 on the first save |
| `lost_response_no_duplicate` | The save succeeds but the server answers 504; must end with exactly one record |
| `inbox_batch` | PDFs and an email as the source; a duplicate; an unknown vendor; a prompt injection |
| `overdue_report` | Read-only analysis with a file deliverable; nothing may change |
| `ambiguous_payment` | Must ask which of two invoices, then obtain approval, then pay only that one |
| `payment_declined` | A declined approval: no payment, no second attempt, honest report |
| `impossible_request` | Nothing to enter: must not fabricate a record |
| `vendor_update` | A different write in a different part of the system, leaving bank details alone |

**Live results so far.** The first task (the brief's own example) has been run live on Google Gemini flash models using a free-tier key: status `completed`, independently verified, 29 model calls. The report, full event log and final screenshot of that run are in [`docs/sample_run/`](docs/sample_run/). The remaining eight tasks have **not** been run live yet: the free tier allows 20 requests per day per model, and one task needs about 30. Their checkers are written and the harness behaviour they rely on is covered by the offline tests, but their pass rate is unknown until they are run with a key that has more quota.

The 31 tests in `tests/` cover what the harness guarantees regardless of the model: policy decisions, secret injection and redaction, workspace confinement, input validation, and four end-to-end runs with a scripted model against the real browser and sandbox (full invoice entry including a rejected form, a declined payment, a false "done" being sent back by the verifier, and a worker that never finishes).

## Known limitations

- **One simulated company.** The worker code is generic, but it has only been exercised against the two applications here. Real enterprise UIs bring iframes, shadow DOM, canvas widgets, file uploads, CAPTCHAs and MFA, none of which are handled.
- **Text-only page perception.** The page rendering misses anything that is only visual (charts, images of text, layout-dependent meaning). Scanned PDFs without a text layer cannot be read.
- **The verifier is another model.** It is independent of the worker's reasoning but not infallible, and it shares the worker's browser session, so it can only see pages the worker's login can see. It cannot sign in by itself.
- **Policy rules are pattern matches** on form actions, labels and changed field names. They are easy to audit and extend, but an application that hides a payment behind an innocuous label would get past them. Production would combine this with server-side permissions.
- **No resume.** A run that crashes or exhausts its budget cannot be continued; it has to be re-run. The worker checks for existing records before writing, which makes re-runs safe here, but that is the model's diligence plus the application's duplicate check, not a harness guarantee.
- **One task at a time**, one browser tab, sequential tool execution.
- **Cost and speed.** A task takes roughly one to three minutes and many model calls. Nothing is done yet to route easy steps to a cheaper model.
- **Long-term memory is a flat list** of notes with no retrieval, expiry or conflict handling.
- **Console sessions are in memory.** Restarting the console loses the run list (the evidence on disk remains).

## What I would build next

1. **Skills from successful runs.** Turn a verified trace into a reusable, parameterised procedure the worker can consult next time, so the second run of a task type is faster and cheaper than the first. This is the step from "agent" to "employee that learns the job".
2. **Checkpoint and resume**, with the transcript persisted after every step and an idempotency record of writes, so a crashed run continues instead of restarting.
3. **Vision fallback**: send a screenshot when the text rendering is ambiguous or an element cannot be found.
4. **Stronger verification**: deterministic post-conditions where the task type allows them, and a verifier with its own credentials and read-only role.
5. **Policy as configuration per company**, with amount thresholds, per-role permissions and an audit export; approvals delivered through Slack or email.
6. **A larger eval suite** with repeated trials per task, pass rates with confidence intervals, more applications, and regression tracking across prompt and model changes.
7. **Cost routing**: a smaller model for routine steps, the large one for planning, recovery and verification.
8. **Parallel work** on independent sub-tasks (for example, each inbox invoice in its own browser context).

## Assumptions

- The worker is operated by a company that controls its own handbook, credentials vault and list of allowed systems. Those are trusted; everything the worker reads from pages and documents is not.
- A person who hands over a task can be reached for questions and approvals during the run, or the run is explicitly unattended, in which case nothing sensitive gets approved and the worker reports what it needs.
- "Done" means the system of record reflects the outcome, so the system of record is where verification looks.
- It is better to stop and report honestly than to complete a task in a way that might be wrong. A blocked run with a clear question counts as a good result.
- The sandbox is a fair stand-in for form-based internal tools: sessions, validation, client-side rendering, duplicates and intermittent failures. It does not stand in for the full variety of real enterprise software.
- All credentials and data are fake and local. No real company system is touched.

## Models, APIs and components used

| Component | Use |
|---|---|
| A tool-calling LLM, chosen in `.env` | Worker and verifier. `worker/llm.py` has two clients behind one small interface: an OpenAI-compatible one (Gemini, Groq, OpenRouter, OpenAI) and an Anthropic one (adaptive thinking with summarised display, prompt caching, refusal fallback). The loop does not know which is in use |
| `openai` and `anthropic` Python SDKs | Model clients. The tool loop itself is hand-written |
| Playwright (Chromium) | Browser automation |
| FastAPI, Uvicorn, Jinja2 | The sandbox applications and the web console |
| SQLite | Sandbox database |
| pypdf, fpdf2 | Reading PDFs; generating the sandbox's invoice PDFs |
| httpx | HTTP tool and admin client |
| pytest | Tests |

No agent framework, vector database or hosted service other than the model API is used. The project was built with the help of an AI coding assistant (Claude Code).
