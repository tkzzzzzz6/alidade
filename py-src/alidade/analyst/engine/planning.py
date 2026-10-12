# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Plan-then-execute support: the ``update_plan`` inspection tool.

For multi-step requests the agent first writes a short analysis plan with
``update_plan`` and then keeps it current as it works (one step
``in_progress`` at a time, ``done`` when finished). Each update is streamed to
the UI as a ``plan`` event, so the user sees the plan before the first chart
and can follow progress. Simple requests skip planning entirely, so they pay
no extra LLM call.

With sub-agents enabled (``ALIDADE_SUBAGENTS=1``, see
``docs/plans/parallel-subagents.md``) each step may also carry an ``id``, the
ids it ``depends_on`` and a self-contained ``goal``; ``ready_steps`` then tells
which steps could run at the same time. Malformed dependencies (unknown ids,
self-references, cycles) never break a run: they fall back to running the
steps in order.
"""

from __future__ import annotations

import copy
import os
import re
from typing import Any

MAX_PLAN_STEPS = 8
MAX_GOAL_CHARS = 600
STATUSES = ("pending", "in_progress", "done", "skipped")
FINISHED = ("done", "skipped")
_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,24}$")


def subagents_enabled() -> bool:
    """Feature flag for plan-driven parallel sub-agents (default: off)."""
    return os.environ.get("ALIDADE_SUBAGENTS", "0").strip().lower() in ("1", "true", "yes", "on")


_PLAN_TOOL_BASE: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "update_plan",
        "description": (
            "Create or change your analysis plan (shown to the user as a checklist). "
            "Use it FIRST, before any other tool, when the request needs three or more "
            "distinct analyses or charts. Step statuses advance by themselves as charts are "
            "shown, so call it again only to add, remove, reorder or skip steps — always "
            "sending the full list. Skip it for requests answered by one or two charts."
            # (with sub-agents, plan_tool() swaps in the two-independent-analyses rule)
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

_DEPENDENCY_FIELDS: dict[str, Any] = {
    "id": {
        "type": "string",
        "description": "Short unique step id such as 's1', 's2'; referenced by depends_on.",
    },
    "depends_on": {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "Ids of the steps whose results this step needs. Empty when the step can be "
            "done on its own — steps with no unfinished dependencies may run at the same time."
        ),
    },
    "goal": {
        "type": "string",
        "description": (
            "The full task for this step, readable without the conversation: what to compute, "
            "on which table/columns, the time range, units and filters agreed with the user, "
            "and what chart to draw."
        ),
    },
}


def plan_tool(with_dependencies: bool | None = None) -> dict[str, Any]:
    """The ``update_plan`` schema; with dependencies when sub-agents are on."""
    if with_dependencies is None:
        with_dependencies = subagents_enabled()
    if not with_dependencies:
        return _PLAN_TOOL_BASE
    tool = copy.deepcopy(_PLAN_TOOL_BASE)
    tool["function"]["description"] = (
        "Create or change your analysis plan (shown to the user as a checklist). Use it FIRST, "
        "before any other tool, when the request needs two or more analyses that do not need "
        "each other's results, or three or more in total — independent steps are worked on at "
        "the same time. Step statuses advance by themselves as charts are shown, so call it "
        "again only to add, remove, reorder or skip steps — always sending the full list. Skip "
        "it for requests answered by one chart.")
    item = tool["function"]["parameters"]["properties"]["steps"]["items"]
    item["properties"] = {**_DEPENDENCY_FIELDS, **item["properties"]}
    item["required"] = ["id", "title", "status", "depends_on"]
    return tool


PLAN_TOOL = _PLAN_TOOL_BASE   # the default schema (sub-agents off)

PLANNING_PROMPT = """\
## Planning

For a specific request that needs {when_to_plan}, call `update_plan` **first**
with {min_steps}–{max_steps} concrete steps that fit within your action budget,
before inspecting data, with the first step `in_progress`. If the request is vague ("analyse our sales"), do not plan
yet: clarify with `ask_user` first and plan once the user has picked a direction.

{status_rule} For {when_not_to_plan}, one number, or a
small edit to an existing chart, do not plan; just act."""

_STATUS_RULE = {
    True: """**You do not need to update step statuses.** Each time a chart is shown, the step
in progress is marked `done` and the next one starts — automatically, without a
call. If a chart belongs to a different step than the one in progress, pass that
step's id as `plan_step` in `visualize`. Call `update_plan` again only to change
the plan itself (add, remove, reorder, or mark a step `skipped`); you may send it
in the same reply as your next action.""",
    # the behaviour before 2026-10-10, kept for A/B evaluation (ALIDADE_PLAN_AUTO_ADVANCE=0)
    False: """Then keep it current: mark a step `in_progress` when you start it and `done`
when its chart or finding is in place (`skipped` if it turned out unnecessary),
always resending the full list. Update the plan before your final answer so it
reflects what was done.""",
}


def auto_advance_enabled() -> bool:
    """Plan statuses advance by code as charts are shown (default: yes).
    ``ALIDADE_PLAN_AUTO_ADVANCE=0`` restores model-maintained statuses."""
    return os.environ.get("ALIDADE_PLAN_AUTO_ADVANCE", "1").strip().lower() not in ("0", "false", "no", "off")


# The planning threshold. Without sub-agents a plan only pays off for longer
# requests (each plan costs an LLM round); with them, two independent analyses
# are already worth a plan, because the plan is what lets them run in parallel.
_THRESHOLD = {
    False: {
        "when_to_plan": ('**three or more distinct analyses or charts** (e.g. "compare sales by '
                         'region, show the monthly trend of the top region, then break it down by product")'),
        "min_steps": "3",
        "when_not_to_plan": "a request answered by one or two charts",
    },
    True: {
        "when_to_plan": ('**two or more distinct analyses that do not need each other\'s results** '
                         '(e.g. "which product line has the best margin, and how does churn differ by region"), or '
                         'three or more analyses in total'),
        "min_steps": "2",
        "when_not_to_plan": "a request answered by one chart",
    },
}

DEPENDENCY_PROMPT = """
**Mark which steps depend on which.** Give every step an `id` (`s1`, `s2`, …),
list in `depends_on` the ids whose *results* it needs, and write a
self-contained `goal`. Steps that do not use another step's result (e.g. "margin
by product line" and "churn by region") get `depends_on: []` — they can be
worked on at the same time. A step that compares, ranks or explains earlier
results depends on them. Example:
`s1` margin by product line (`[]`), `s2` churn by region (`[]`), `s3` relate the
two (`["s1", "s2"]`). Each `goal` must stand on its own — it is read without this
conversation — so spell out the table, columns, time range, units and filters
the user agreed to."""


def planning_prompt(with_dependencies: bool | None = None, auto_advance: bool | None = None) -> str:
    """The planning section of the system prompt."""
    if with_dependencies is None:
        with_dependencies = subagents_enabled()
    if auto_advance is None:
        auto_advance = auto_advance_enabled()
    text = PLANNING_PROMPT.replace("{max_steps}", str(MAX_PLAN_STEPS))
    for key, value in _THRESHOLD[bool(with_dependencies)].items():
        text = text.replace("{" + key + "}", value)
    text = text.replace("{status_rule}", _STATUS_RULE[bool(auto_advance)])
    return text + DEPENDENCY_PROMPT if with_dependencies else text


def keep_subagent_info(new: list[dict[str, Any]], old: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Carry each step's ``subagent`` record (who worked on it, what it found)
    over to a plan the model just rewrote, matching steps by id."""
    info = {s.get("id"): s["subagent"] for s in old or [] if isinstance(s, dict) and s.get("subagent")}
    if not info:
        return new
    return [{**s, "subagent": info[s["id"]]} if s.get("id") in info and "subagent" not in s else s
            for s in new]


def normalize_steps(raw: Any) -> list[dict[str, Any]]:
    """Validate the tool arguments into ``[{id, title, status, depends_on[, goal]}]``.

    Ids are the model's own when valid and unique, otherwise ``step-<n>`` by
    position (stable, so the UI can animate status changes). ``depends_on``
    may name ids, ``step-<n>`` or plain positions (``"2"``); unknown names and
    self-references are dropped, and a cycle makes the plan run in order.
    """
    if not isinstance(raw, list):
        return []
    steps: list[dict[str, Any]] = []
    raw_deps: list[Any] = []
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
        position_id = f"step-{len(steps) + 1}"
        model_id = str(item.get("id", "")).strip()
        taken = {s["id"] for s in steps}
        step_id = model_id if _ID_RE.match(model_id) and model_id not in taken else position_id
        if step_id in taken:            # a model id already used "step-<n>"
            step_id = f"{position_id}-{len(steps)}"
        step: dict[str, Any] = {"id": step_id, "title": title[:120], "status": status, "depends_on": []}
        goal = str(item.get("goal") or "").strip()
        if goal:
            step["goal"] = goal[:MAX_GOAL_CHARS]
        steps.append(step)
        raw_deps.append(item.get("depends_on"))

    aliases: dict[str, str] = {}
    for n, step in enumerate(steps, 1):
        aliases.update({step["id"]: step["id"], f"step-{n}": step["id"], str(n): step["id"]})
    for step, deps in zip(steps, raw_deps):
        if isinstance(deps, (str, int)):
            deps = [deps]
        if not isinstance(deps, list):
            continue
        resolved: list[str] = []
        for d in deps:
            target = aliases.get(str(d).strip())
            if target and target != step["id"] and target not in resolved:
                resolved.append(target)
        step["depends_on"] = resolved
    if _has_cycle(steps):
        for prev, step in zip(steps, steps[1:]):
            step["depends_on"] = [prev["id"]]
        steps[0]["depends_on"] = []
    return steps


def _has_cycle(steps: list[dict[str, Any]]) -> bool:
    deps = {s["id"]: s["depends_on"] for s in steps}
    state: dict[str, int] = {}          # 1 = visiting, 2 = done

    def visit(node: str) -> bool:
        if state.get(node) == 1:
            return True
        if state.get(node) == 2:
            return False
        state[node] = 1
        if any(visit(d) for d in deps.get(node, [])):
            return True
        state[node] = 2
        return False

    return any(visit(s["id"]) for s in steps)


def advance_plan(steps: list[dict[str, Any]], step_id: str | None = None) -> list[dict[str, Any]] | None:
    """A chart was shown: mark its step done and start the next one.

    The step is ``step_id`` when the chart names one (``plan_step``), else the
    step in progress, else the first step ready to start. The next step
    started is the first one whose dependencies are now finished. Returns the
    new plan, or None when there is no plan or nothing changed.
    """
    if not steps:
        return None
    plan = [dict(s) for s in steps]
    by_id = {s["id"]: s for s in plan}
    target = by_id.get(str(step_id)) if step_id else None
    if target is None:
        target = next((s for s in plan if s["status"] == "in_progress"), None)
    if target is None:
        ready = ready_steps(plan)
        target = ready[0] if ready else None
    if target is None or target["status"] in FINISHED:
        return None
    target["status"] = "done"
    if not any(s["status"] == "in_progress" for s in plan):
        nxt = ready_steps(plan)
        if nxt:
            nxt[0]["status"] = "in_progress"
    return plan


def close_plan(steps: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """The run finished: a step still in progress is done (its work is in the
    answer). Pending steps stay pending — the plan shows what was not reached."""
    if not any(s.get("status") == "in_progress" for s in steps or []):
        return None
    return [{**s, "status": "done"} if s.get("status") == "in_progress" else dict(s) for s in steps]


def startable_steps(steps: list[dict[str, Any]], exclude: Any = ()) -> list[dict[str, Any]]:
    """Unfinished steps whose dependencies are all finished, pending or just
    marked in progress (a fresh plan usually starts its first step) — what a
    dispatcher can hand out now. ``exclude``: ids already handed out."""
    status = {s["id"]: s.get("status") for s in steps}
    skip = set(exclude or ())
    return [s for s in steps
            if s.get("status") in ("pending", "in_progress") and s["id"] not in skip
            and all(status.get(d) in FINISHED for d in s.get("depends_on") or [])]


def ready_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pending steps whose dependencies are all finished (done or skipped).

    Two or more of them at once is what a dispatcher can hand to sub-agents
    in parallel; one is just "the next step".
    """
    status = {s["id"]: s.get("status") for s in steps}
    return [s for s in steps
            if s.get("status") == "pending"
            and all(status.get(d) in FINISHED for d in s.get("depends_on") or [])]


def summarize_plan(steps: list[dict[str, Any]]) -> str:
    """One-line confirmation returned to the model (and kept in summaries)."""
    if not steps:
        return "Plan cleared."
    marks = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]", "skipped": "[-]"}
    with_deps = any(s.get("depends_on") for s in steps)

    def line(s: dict[str, Any]) -> str:
        if not with_deps:
            return f"{marks[s['status']]} {s['title']}"
        after = f" (after {', '.join(s['depends_on'])})" if s.get("depends_on") else ""
        return f"{marks[s['status']]} {s['id']} {s['title']}{after}"

    body = "; ".join(line(s) for s in steps)
    return f"Plan updated ({len(steps)} steps): {body}"
