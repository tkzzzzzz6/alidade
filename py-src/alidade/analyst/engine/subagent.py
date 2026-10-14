"""Sub-agents: one plan step, worked on in parallel with the others.

When a plan has two or more steps that do not depend on each other (and
``ALIDADE_SUBAGENTS=1``), the main agent hands each of them to a sub-agent.
A sub-agent is an ordinary :class:`AnalystAgent` in *sub-task mode*:

* its own instance, so its Python session (``CodeExecutor``), reasoning log,
  chart buffer and self-check bookkeeping are never shared with another run;
* the same graph and the same skills, minus what does not fit a step:
  no ``ask_user`` (it cannot pause for the user), no ``update_plan`` and no
  ``load_skill``; tools are ``execute_python_script``, ``inspect_source_data``
  and ``visualize`` only;
* a small action budget (2 charts by default) and a closing instruction to
  answer in a few lines with the numbers it computed;
* an in-memory checkpointer: a step never pauses, so nothing has to survive
  the process, and the main run's checkpoint store is left alone.

``run_subtask`` runs one step to completion and returns its result; the
dispatcher in ``graph.py`` runs several of them on a bounded thread pool and
forwards their events (tagged with ``step_id``) to the user's stream.

See ``docs/plans/parallel-subagents.md`` for the design and its trade-offs.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from typing import Any, Callable

from alidade import langsmith_tracing
from alidade.analyst.agent import AnalystAgent
from alidade.analyst.engine.execution import CodeExecutor
from alidade.analyst.engine.prompts import CORE_SKILL

logger = logging.getLogger(__name__)

# No inspect_source_data: the table summary a sub-agent starts with already has
# every column, type, value sample and data-quality note, so re-inspecting only
# cost a model round trip per step (measured: ~1.4 extra tool calls per step).
SUBTASK_TOOLS = frozenset({"execute_python_script", "visualize"})
DEFAULT_MAX_ACTIONS = 2
DEFAULT_MAX_PARALLEL = 3
DEFAULT_TIMEOUT_S = 180.0
MAX_FINDINGS_CHARS = 1500
# events that belong to the step's own lifecycle, not to the user's stream
_NOT_FORWARDED = frozenset({"completion", "interact", "context_info", "plan"})

SUBTASK_PROMPT = """

## You are working on one step of a larger analysis

The user's request was split into steps, and several steps are being worked on
at the same time by other analysts. You have **exactly one step**, described in
the user message under [YOUR STEP]. Do that step only — not the others, and not
the whole request.

- Show at most {max_actions} chart(s) for this step — usually one is enough.
- Do not ask the user anything and do not make a plan; if something is unclear,
  choose the most reasonable reading and say so.
- Keep to the time range, units and filters given in the step and in the user's
  answers — another step will be compared with yours.
- Finish with plain text: **3–5 lines** stating what you found, with the
  concrete numbers you computed. If you made an assumption the main analyst
  should know about, add a line starting with "Open question:"."""


def max_parallel() -> int:
    """Env default only; a run uses ``agent.settings.max_parallel``."""
    try:
        return max(1, int(os.environ.get("ALIDADE_MAX_SUBAGENTS", DEFAULT_MAX_PARALLEL)))
    except ValueError:
        return DEFAULT_MAX_PARALLEL


def timeout_s() -> float:
    try:
        return max(1.0, float(os.environ.get("ALIDADE_SUBAGENT_TIMEOUT", DEFAULT_TIMEOUT_S)))
    except ValueError:
        return DEFAULT_TIMEOUT_S


class SubtaskAgent(AnalystAgent):
    """An analyst restricted to one plan step (see module docstring)."""

    def __init__(self, *args: Any, max_actions: int = DEFAULT_MAX_ACTIONS,
                 should_stop: Callable[[], bool] | None = None, **kwargs: Any):
        # one action more than the prompt allows, so that hitting the chart
        # limit still leaves a turn for the closing text (the findings)
        kwargs.setdefault("max_iterations", max_actions + 1)
        super().__init__(*args, **kwargs)
        self.max_actions = max_actions
        self.should_stop = should_stop

    def _current_tools(self, loaded: set[str]) -> list[dict[str, Any]]:
        return [t for t in super()._current_tools(loaded)
                if t.get("function", {}).get("name") in SUBTASK_TOOLS]

    def _legal_actions(self, loaded: set[str]) -> frozenset[str]:
        return frozenset(a for a in super()._legal_actions(loaded) if a in SUBTASK_TOOLS)

    def _dispatch_skill_action(self, skill_name: str, action_type: str, *args: Any, **kwargs: Any):
        if action_type not in SUBTASK_TOOLS:
            # not offered to a sub-agent; a hallucinated call must not pause the run
            return (f"[ERROR] '{action_type}' is not available while working on a single step. "
                    "Use execute_python_script / inspect_source_data / visualize, then answer in text.")
        return super()._dispatch_skill_action(skill_name, action_type, *args, **kwargs)

    def _build_system_prompt(self, *args: Any, **kwargs: Any) -> str:
        """A prompt for one step only (about half the main agent's).

        The main agent's prompt explains asking the user, planning, skills,
        reports, saved definitions and loading data — none of which a sub-agent
        can do. Each sub-agent starts cold (no shared prompt cache with its
        siblings, which start at the same moment), so every token here is paid
        at full price and in latency; it keeps only what charting a step needs.
        """
        core_body = self.registry.load_body(CORE_SKILL)
        parts = [SUBTASK_FRAME]
        if self.agent_exploration_rules and self.agent_exploration_rules.strip():
            parts.append("## Additional exploration rules\n\n" + self.agent_exploration_rules.strip())
        parts.append(_section(core_body, "### `visualize`"))
        parts.append(_section(core_body, "## Chart Creation Guide"))
        if self._knowledge_store:
            rules = self._knowledge_store.load_always_apply_rules()
            self._injected_rules = [r["title"] for r in rules]
            block = self._knowledge_store.format_rules_block(rules)
            if block.strip():
                parts.append(block.strip())
        if self.agent_coding_rules and self.agent_coding_rules.strip():
            parts.append("## Agent Coding Rules\n\n" + self.agent_coding_rules.strip())
        prompt = "\n\n".join(p for p in parts if p) + SUBTASK_PROMPT.format(max_actions=self.max_actions)
        if self.language_instruction:
            prompt += "\n\n" + self.language_instruction
        return prompt


SUBTASK_FRAME = """\
You are a data analyst working on **one step** of a larger analysis. Other
analysts are doing the other steps at the same time; a lead analyst combines
everyone's results for the user.

## How you work

- **Tool** `execute_python_script(code)`: Python with pandas, numpy, scipy and
  duckdb in a session that persists between calls; its printed output comes
  back to you only. Use it only when you must check something before charting.
- **Action** `visualize`: its code computes a DataFrame from the workspace tables
  and renders it as a chart for the user; the result comes back to you. Take one
  action per reply and read its result before the next.
- The table summary in your first message is complete — every column, its type,
  value samples and any data-quality notes. Do not re-read schemas: when the
  step is straightforward, go straight to `visualize`.
- When a table has a *Data quality check* (duplicates, numbers or dates stored
  as text, inconsistent labels, missing values), fix it in your code before
  computing and mention the fix in your findings.
- Finish by replying with plain text and no tool call."""


def _section(body: str, heading: str) -> str:
    """The part of ``body`` from the line starting with ``heading`` up to the
    next heading of the same or a higher level (empty if absent)."""
    lines = body.splitlines()
    level = len(heading) - len(heading.lstrip("#"))
    start = next((i for i, l in enumerate(lines) if l.startswith(heading)), None)
    if start is None:
        return ""
    end = len(lines)
    for i in range(start + 1, len(lines)):
        stripped = lines[i].lstrip("#")
        hashes = len(lines[i]) - len(stripped)
        if 0 < hashes <= level and stripped.startswith(" "):
            end = i
            break
    return "\n".join(lines[start:end]).strip()


def subtask_question(task: dict[str, Any]) -> str:
    """The user message a sub-agent starts from."""
    lines = [f"[YOUR STEP] {task['step_id']}: {task.get('title', '')}", "", task.get("goal") or task.get("title", "")]
    if task.get("user_question"):
        lines += ["", "[THE USER'S REQUEST — context only; do just your step]", task["user_question"]]
    if task.get("user_answers"):
        lines += ["", "[WHAT THE USER ANSWERED WHEN ASKED]"] + [f"- {a}" for a in task["user_answers"]]
    if task.get("lead_checks"):
        lines += ["", "[WHAT THE LEAD ANALYST ALREADY CHECKED — reuse it, do not repeat it]"]
        lines += [f"---\n{c}" for c in task["lead_checks"]]
    return "\n".join(lines)


def run_subtask(
    parent: AnalystAgent,
    task: dict[str, Any],
    emit: Callable[[dict[str, Any]], None],
    stop: threading.Event | None = None,
) -> dict[str, Any]:
    """Run one plan step to completion and return its result.

    ``task``: ``step_id``, ``title``, ``goal``, ``user_question``,
    ``user_answers``, ``input_tables``, ``parent_run_id`` and optionally
    ``max_actions``. ``emit`` receives the step's events (already tagged with
    ``step_id``); it is called from the worker thread. ``stop`` ends the step
    at its next LLM turn (timeout or the user stopping the run).

    Returns ``{step_id, status, findings, charts, completed_steps,
    duration_s, usage}``; ``status`` is ``success`` / ``budget_exhausted`` /
    ``stopped`` / ``failed``. ``usage`` counts the step's model calls, tokens
    and tool calls (from its own ``llm_call`` / ``tool_start`` events).
    """
    from langgraph.checkpoint.memory import InMemorySaver

    from alidade.analyst.engine.graph import RECURSION_LIMIT, AnalystGraph

    step_id = str(task["step_id"])
    started = time.time()
    stop = stop or threading.Event()
    agent = SubtaskAgent(
        parent.client, parent.workspace,
        agent_exploration_rules=parent.agent_exploration_rules,
        agent_coding_rules=parent.agent_coding_rules,
        language_instruction=parent.language_instruction,
        max_actions=int(task.get("max_actions") or DEFAULT_MAX_ACTIONS),
        should_stop=stop.is_set,
        settings=getattr(parent, "settings", None),
    )
    run_id = f"{task.get('parent_run_id') or 'run'}-{step_id}-{uuid.uuid4().hex[:6]}"
    agent.executor = CodeExecutor(agent.workspace, run_id)
    result: dict[str, Any] = {"step_id": step_id, "status": "failed", "findings": "", "charts": [],
                              "chart_titles": [], "completed_steps": [], "duration_s": 0.0}
    usage = {"calls": 0, "prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0, "tool_calls": 0}
    result["usage"] = usage
    try:
        agent.executor.open()
        graph = AnalystGraph(agent).build().compile(checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": run_id}, "recursion_limit": RECURSION_LIMIT}
        # shows up inside its branch of the main run in LangSmith (when on)
        langsmith_tracing.run_config(config, f"subagent {step_id}",
                                     {"step_id": step_id, "step_title": task.get("title", "")}, ["subagent"])
        graph_input = {
            "run_id": run_id,
            "user_question": subtask_question(task),
            "input_tables": task.get("input_tables") or [],
            "charts": [],
            "max_iterations": agent.max_iterations,
        }
        for event in graph.stream(graph_input, config, stream_mode="custom", durability="exit"):
            if not isinstance(event, dict) or event.get("type") in _NOT_FORWARDED:
                continue
            if event.get("type") == "llm_call":
                usage["calls"] += 1
                for key in ("prompt_tokens", "cached_tokens", "completion_tokens"):
                    usage[key] += int(event.get(key) or 0)
            elif event.get("type") == "tool_start":
                usage["tool_calls"] += 1
            if event.get("type") == "result" and event.get("status") == "success":
                res = (event.get("content") or {}).get("result") or {}
                result["chart_titles"].append(((res.get("refined_goal") or {}).get("title")) or "")
            emit({**event, "step_id": step_id})
        values = graph.get_state(config).values
        status = values.get("status") or "success"
        findings = (values.get("final_text") or "").strip() if status == "success" else ""
        if not findings and result["chart_titles"]:
            # no closing text (budget or stop): the charts are what it found
            findings = "Charts shown (no written summary): " + "; ".join(t for t in result["chart_titles"] if t)
        result.update({
            "status": {"success": "success", "max_iterations": "budget_exhausted",
                       "stopped": "stopped"}.get(status, "failed"),
            "findings": findings[:MAX_FINDINGS_CHARS],
            "charts": list(values.get("charts") or []),
            "completed_steps": list(values.get("completed_steps") or []),
        })
        if status not in ("success", "max_iterations", "stopped"):
            result["error"] = values.get("error_message") or status
    except Exception as exc:  # noqa: BLE001 — a failed step must not fail the run
        logger.warning("[SubAgent] step %s failed", step_id, exc_info=True)
        result["error"] = f"{type(exc).__name__}: {exc}"[:300]
    finally:
        if agent.executor is not None:
            agent.executor.close(discard_saved=True)
        result["duration_s"] = round(time.time() - started, 1)
    return result
