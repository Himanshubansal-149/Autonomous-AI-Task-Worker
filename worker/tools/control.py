"""Tools that change the run rather than the world: plan, memory, asking a
person, and the two terminal calls (finish for the worker, submit_verdict for
the verifier). The terminal calls are intercepted by the loop, never executed.
"""
from __future__ import annotations

from .base import Tool, ToolContext, ToolResult, obj


def _update_plan(ctx: ToolContext, args: dict) -> ToolResult:
    ctx.plan = args["steps"]
    ctx.trace.emit("plan", steps=ctx.plan)
    return ToolResult("Plan recorded.")


def _remember(ctx: ToolContext, args: dict) -> ToolResult:
    long_term = args.get("scope") == "long_term"
    ctx.memory.remember(args["note"], long_term, ctx.task)
    ctx.trace.emit("memory", note=args["note"], scope="long_term" if long_term else "run")
    return ToolResult("Saved for future tasks." if long_term else "Noted for this task.")


def _ask_user(ctx: ToolContext, args: dict) -> ToolResult:
    answer = ctx.human.ask(args["question"], args.get("options"))
    ctx.human_log.append({"kind": "question", "prompt": args["question"], "response": answer})
    ctx.trace.emit("human", kind="question", prompt=args["question"], response=answer)
    return ToolResult(f"The user answered: {answer}")


def _intercepted(ctx: ToolContext, args: dict) -> ToolResult:
    raise RuntimeError("terminal tools are handled by the agent loop")


CONTROL_TOOLS = [
    Tool(
        "update_plan",
        "Record or revise your plan for the task so the person watching can follow along. Send the full list each time.",
        obj({"steps": {"type": "array", "items": obj(
            {"step": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "done"]}},
            ["step", "status"])}}, ["steps"]),
        _update_plan,
    ),
    Tool(
        "remember",
        "Save a fact. scope='run' (default) for values you found and will need later in this task. "
        "scope='long_term' for a durable lesson about how these systems behave that would help on future tasks.",
        obj({"note": {"type": "string"}, "scope": {"type": "string", "enum": ["run", "long_term"]}}, ["note"]),
        _remember,
    ),
    Tool(
        "ask_user",
        "Ask the person who gave you the task a question and wait for the answer. Use when the task is ambiguous in "
        "a way that changes what you would do, or information is missing and cannot be looked up.",
        obj({"question": {"type": "string"},
             "options": {"type": "array", "items": {"type": "string"}, "description": "Suggested answers, if any."}},
            ["question"]),
        _ask_user,
    ),
]

FINISH = Tool(
    "finish",
    "End the task and report back. Your report is checked against the live systems by an independent verifier "
    "before it is accepted.",
    obj({
        "status": {"type": "string", "enum": ["completed", "partially_completed", "blocked", "failed"],
                   "description": "completed: everything asked for is done. partially_completed: some of it is done. "
                                  "blocked: you need a decision or input from a person. failed: it could not be done."},
        "summary": {"type": "string", "description": "What the person needs to know, outcome first. A few sentences."},
        "success_criteria": {
            "type": "array",
            "description": "Each concrete outcome you are claiming, with where it can be checked.",
            "items": obj({"criterion": {"type": "string", "description": "A specific, checkable statement."},
                          "evidence": {"type": "string", "description": "Record ID, URL, file path or value that shows it."}},
                         ["criterion", "evidence"]),
        },
        "needs_from_user": {"type": "string", "description": "Anything the person must decide or do next. Omit if nothing."},
    }, ["status", "summary", "success_criteria"]),
    _intercepted,
)

SUBMIT_VERDICT = Tool(
    "submit_verdict",
    "Deliver your verdict on the worker's report.",
    obj({
        "verified": {"type": "boolean", "description": "True only if every material claim checked out against the live systems."},
        "checks": {"type": "array", "items": obj(
            {"criterion": {"type": "string"}, "passed": {"type": "boolean"},
             "evidence": {"type": "string", "description": "What you observed, and where."}},
            ["criterion", "passed", "evidence"])},
        "feedback": {"type": "string", "description": "If not verified: exactly what is wrong or missing, so the worker can fix it."},
    }, ["verified", "checks"]),
    _intercepted,
)
