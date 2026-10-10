# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Plan-then-execute support: the ``update_plan`` inspection tool.

For multi-step requests the agent first writes a short analysis plan with
``update_plan`` and then keeps it current as it works (one step
``in_progress`` at a time, ``done`` when finished). Each update is streamed to
the UI as a ``plan`` event, so the user sees the plan before the first chart
and can follow progress. Simple requests skip planning entirely, so they pay
no extra LLM call.
"""

from __future__ import annotations

from typing import Any

MAX_PLAN_STEPS = 8
STATUSES = ("pending", "in_progress", "done", "skipped")

PLAN_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "update_plan",
        "description": (
            "Create or update your analysis plan (shown to the user as a checklist). "
            "Use it FIRST, before any other tool, when the request needs several distinct "
            "analyses or charts; then call it again to move steps to in_progress / done as "
            "you work. Always send the full list of steps. Skip it for simple requests "
            "answered by one chart or one fact."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "description": f"The full plan, 2-{MAX_PLAN_STEPS} short steps in order.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": "≤ 12 words, in the user's language, e.g. 'Compare revenue by region'.",
                            },
                            "status": {"type": "string", "enum": list(STATUSES)},
                        },
                        "required": ["title", "status"],
                    },
                },
            },
            "required": ["steps"],
        },
    },
}

PLANNING_PROMPT = """\
## Planning

For a specific request that needs **several distinct analyses or charts** (e.g.
"compare sales by region, then show the monthly trend of the top region"), call
`update_plan` **first** with 2–{max_steps} concrete steps that fit within your
action budget, before inspecting data. If the request is vague ("analyse our
sales"), do not plan yet: clarify with `ask_user` first and plan once the user has
picked a direction. Then keep it current: mark a step
`in_progress` when you start it and `done` when its chart or finding is in place
(`skipped` if it turned out unnecessary), always resending the full list. Update
the plan before your final answer so it reflects what was done. For a simple
request — one chart, one number, a small edit to an existing chart — do not plan;
just act."""


def normalize_steps(raw: Any) -> list[dict[str, Any]]:
    """Validate the tool arguments into ``[{id, title, status}]``.

    Ids are stable by position so the UI can animate status changes.
    """
    if not isinstance(raw, list):
        return []
    steps: list[dict[str, Any]] = []
    for item in raw:
        if len(steps) >= MAX_PLAN_STEPS:
            break
        if isinstance(item, str):
            item = {"title": item, "status": "pending"}
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        if not title:
            continue
        status = str(item.get("status", "pending")).strip().lower()
        if status not in STATUSES:
            status = "pending"
        steps.append({"id": f"step-{len(steps) + 1}", "title": title[:120], "status": status})
    return steps


def summarize_plan(steps: list[dict[str, Any]]) -> str:
    """One-line confirmation returned to the model (and kept in summaries)."""
    if not steps:
        return "Plan cleared."
    marks = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]", "skipped": "[-]"}
    body = "; ".join(f"{marks[s['status']]} {s['title']}" for s in steps)
    return f"Plan updated ({len(steps)} steps): {body}"
