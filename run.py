"""Command line entry point.

    python run.py task "Find the latest invoice from Acme Robotics and enter it into our finance system"
    python run.py console            # web console at http://127.0.0.1:8800
    python run.py eval               # run the evaluation suite
    python run.py sandbox            # just the simulated company systems
"""
from __future__ import annotations

import argparse
import json
import os
import sys

CHAOS_HELP = ("fault injection as JSON, e.g. "
              "'{\"erp_modal\": true, \"erp_session_drop\": 1, \"erp_submit_503\": 1}'")


def _utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _require_key() -> None:
    from worker.config import Settings

    settings = Settings.from_env()  # loads .env
    has_key = settings.api_key if settings.provider != "anthropic" else (
        os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    if not has_key:
        sys.exit("No model API key found. Open .env and set one of GEMINI_API_KEY, GROQ_API_KEY, "
                 "OPENROUTER_API_KEY, OPENAI_API_KEY or ANTHROPIC_API_KEY.")


def cmd_task(args: argparse.Namespace) -> int:
    _require_key()
    from sandbox import client
    from worker.agent import Agent, render_report
    from worker.config import Settings
    from worker.human import ConsoleHuman, NoHuman
    from worker.llm import make_llm
    from worker.trace import console_listener

    settings = Settings.from_env()
    settings.headless = not args.headed
    settings.slow_mo_ms = 350 if args.headed else 0
    client.ensure_running()
    if args.reset or args.chaos:
        client.reset(**json.loads(args.chaos or "{}"))

    llm = make_llm(settings)
    human = NoHuman() if args.unattended else ConsoleHuman()
    result = Agent(llm, settings, human, [console_listener]).run(args.task)
    print("\n" + "=" * 78 + "\n" + render_report(result))
    print(f"Evidence saved in {result.run_dir}")
    return 0 if result.status == "completed" and result.verified else 1


def cmd_console(args: argparse.Namespace) -> int:
    _require_key()
    import uvicorn

    from sandbox import client

    client.ensure_running()
    print(f"Web console: http://127.0.0.1:{args.port}   (sandbox: {client.base_url()})")
    uvicorn.run("console.server:app", host="127.0.0.1", port=args.port, log_level="warning")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    _require_key()
    from evals.runner import main as eval_main

    return eval_main(only=args.only, verbose=args.verbose)


def cmd_sandbox(args: argparse.Namespace) -> int:
    import uvicorn

    from sandbox import seed
    from sandbox.config import base_url
    from urllib.parse import urlparse

    if args.reset:
        seed.reset()
    url = urlparse(base_url())
    print(f"Sandbox: {base_url()}")
    uvicorn.run("sandbox.app:app", host=url.hostname, port=url.port, log_level="warning")
    return 0


def main() -> int:
    _utf8_console()
    parser = argparse.ArgumentParser(description="Autonomous AI task worker")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("task", help="run one task from the terminal")
    p.add_argument("task", help="the task, in plain language")
    p.add_argument("--headed", action="store_true", help="show the browser window while the worker uses it")
    p.add_argument("--reset", action="store_true", help="reset the sandbox data before running")
    p.add_argument("--chaos", help=CHAOS_HELP + " (implies --reset)")
    p.add_argument("--unattended", action="store_true", help="nobody is available to answer questions or approve")
    p.set_defaults(fn=cmd_task)

    p = sub.add_parser("console", help="start the web console")
    p.add_argument("--port", type=int, default=8800)
    p.set_defaults(fn=cmd_console)

    p = sub.add_parser("eval", help="run the evaluation suite against the sandbox")
    p.add_argument("--only", help="comma-separated task names")
    p.add_argument("--verbose", action="store_true", help="print each step")
    p.set_defaults(fn=cmd_eval)

    p = sub.add_parser("sandbox", help="run only the simulated company systems")
    p.add_argument("--reset", action="store_true")
    p.set_defaults(fn=cmd_sandbox)

    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
