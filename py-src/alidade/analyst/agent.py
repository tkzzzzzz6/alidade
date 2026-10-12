# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""AnalystAgent — the unified data analyst agent.

The agent loop runs as a LangGraph state graph (see
:mod:`alidade.analyst.engine.graph`). This module is the facade the
HTTP route talks to: it owns the per-run runtime (LLM client, workspace, skill
registry, sandbox executor, reasoning log), builds the initial context, exposes
the execution substrate skills call through ``ctx.runtime``, and drives the
graph — streaming its events and checkpointing the run when it pauses.

Skills are unchanged: the core skill provides ``execute_python_script`` /
``inspect_source_data`` plus the ``visualize`` and ``ask_user`` actions; gated
skills (``report``, ``data-loading``) unlock more actions after ``load_skill``.

Pausing: ``ask_user`` pauses the graph with ``interrupt()``. The run's state is
checkpointed under its ``run_id`` (carried on the ``interact`` event) and the
client resumes it by sending that ``run_id`` with the user's reply — the
conversation never has to round-trip through the browser.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any, Callable, Generator

from alidade.agents.context import (
    build_focused_thread_context,
    build_lightweight_table_context,
    build_peripheral_thread_context,
)
from alidade.agents.client_utils import Client
from alidade.analyst.engine import checkpoints
from alidade.analyst.engine import chart_check
from alidade.analyst.engine import routing
from alidade.analyst.engine.execution import CodeExecutor
from alidade.analyst.engine.graph import RECURSION_LIMIT, AnalystGraph
from alidade.analyst.engine.trace import RunTrace
from alidade.analyst.engine.planning import plan_tool, planning_prompt
from alidade.analyst.engine.prompts import (
    CORE_SKILL,
    SKILL_LOADED_BANNER,
    SYSTEM_PROMPT,
    build_available_charts_context,
    snapshot_dialog,
)
from alidade.analyst.skills import (
    Event,
    SkillContext,
    SkillRegistry,
    build_registry,
)
from alidade.analyst.tools import build_tools

logger = logging.getLogger(__name__)

# Events that end a stream. They are held back until the graph has stopped and
# the run has been checkpointed (or cleaned up), so a client that disconnects
# as soon as it sees one can never cut the bookkeeping short.
_TERMINAL_EVENTS = frozenset({"completion", "interact"})


def _fast_client_for(client: Any) -> Any:
    """A client for ``ALIDADE_FAST_MODEL`` on the same provider, or None when
    routing is off (not set, same as the main model, or the client cannot switch)."""
    name = routing.fast_model_name()
    if not name or not hasattr(client, "with_model"):
        return None
    if name == getattr(client, "model_name", None) or name == getattr(client, "model", None):
        return None
    try:
        return client.with_model(name)
    except Exception:  # noqa: BLE001
        logger.warning("Could not create a client for %s; routing is off", name, exc_info=True)
        return None


class AnalystAgent:
    """Unified data analyst agent — core actions + on-demand skills."""

    def __init__(
        self,
        client: Client,
        workspace,
        skill_registry: SkillRegistry | None = None,
        agent_exploration_rules: str = "",
        agent_coding_rules: str = "",
        language_instruction: str = "",
        max_iterations: int = 5,
        max_repair_attempts: int = 2,
        identity_id: str | None = None,
    ):
        self.client = client
        # model routing: a cheaper model of the same provider for routine steps
        self.fast_client = _fast_client_for(client)
        # ... and/or a different reasoning effort per tier (same model)
        self.tier_efforts = routing.tier_efforts()
        self.routing_on = self.fast_client is not None or any(self.tier_efforts.values())
        self.workspace = workspace
        self.registry = skill_registry or build_registry()
        self.agent_exploration_rules = agent_exploration_rules
        self.agent_coding_rules = agent_coding_rules
        self.language_instruction = language_instruction
        self.max_iterations = max_iterations
        self.max_repair_attempts = max_repair_attempts
        self.identity_id = identity_id

        from alidade.agents.reasoning_log import ReasoningLogger, _NullReasoningLogger
        self._session_id = uuid.uuid4().hex[:12]
        if identity_id:
            try:
                self._reasoning_log = ReasoningLogger(identity_id, "AnalystAgent", self._session_id)
            except Exception:  # noqa: BLE001
                logger.warning("Failed to initialise ReasoningLogger", exc_info=True)
                self._reasoning_log = _NullReasoningLogger()
        else:
            self._reasoning_log = _NullReasoningLogger()

        self._knowledge_store = None
        self._injected_knowledge: list[dict[str, Any]] = []
        self._injected_rules: list[str] = []
        user_home = getattr(workspace, "user_home", None)
        if user_home:
            try:
                from alidade.knowledge.store import KnowledgeStore
                self._knowledge_store = KnowledgeStore(user_home)
            except Exception:  # noqa: BLE001
                logger.warning("Failed to initialise KnowledgeStore", exc_info=True)

        # Per-run runtime state (never checkpointed).
        self.executor: CodeExecutor | None = None
        self._run_payload: dict[str, Any] = {}
        self._skill_state: dict[str, Any] = {}
        # Chart self-check bookkeeping for the action being dispatched.
        self._chart_check_failures = 0
        self._chart_check_note = ""
        self._write: Callable[[dict[str, Any]], None] = lambda _event: None

    def effort_for(self, tier: str) -> str | None:
        """The reasoning effort for a routing tier (None = the usual one)."""
        return self.tier_efforts.get(tier)

    def client_for(self, tier: str) -> Client:
        """The client for a routing tier (see :mod:`.engine.routing`)."""
        if tier == routing.FAST and self.fast_client is not None:
            return self.fast_client
        return self.client

    # ==================================================================
    # Public API
    # ==================================================================

    def run(
        self,
        input_tables: list[dict[str, Any]],
        user_question: str,
        focused_thread: list[dict[str, Any]] | None = None,
        other_threads: list[dict[str, Any]] | None = None,
        primary_tables: list[str] | None = None,
        attached_images: list[str] | None = None,
        charts: list[dict[str, Any]] | None = None,
        scratch_files: list[str] | None = None,
        conversation_id: str = "",
        resume_run_id: str | None = None,
        checkpoint_db_path=None,
    ) -> Generator[dict[str, Any], None, None]:
        """Run (or resume) the analyst graph, yielding wire events.

        Event ``type`` values: ``context_info``, ``thinking_text``,
        ``tool_start`` / ``tool_result``, ``skill_loaded``, ``action``,
        ``text_delta``, ``result``, ``error`` and the terminal ``completion``
        or ``interact`` (a paused run; it carries ``run_id``).

        To resume a paused run pass ``resume_run_id`` and the user's reply as
        ``user_question``.
        """
        from langgraph.types import Command

        rlog = self._reasoning_log
        started = time.time()
        resuming = bool(resume_run_id)
        run_id = resume_run_id or uuid.uuid4().hex
        self._skill_state = {}
        self.executor = CodeExecutor(self.workspace, run_id)
        # set when this run ends for any reason, including the client going away
        # (Stop): running sub-agents watch it and stop too
        self.run_ended = threading.Event()
        held: list[dict[str, Any]] = []
        outcome = "aborted"
        trace = RunTrace(
            getattr(self.workspace, "user_home", None), run_id, question=user_question,
            model=getattr(self.client, "model_name", None) or getattr(self.client, "model", ""),
            fast_model=(getattr(self.fast_client, "model_name", "") if self.fast_client is not None else ""),
            resumed=resuming,
        )

        rlog.log(
            "session_start",
            agent="AnalystAgent",
            session_id=self._session_id,
            run_id=run_id,
            resumed=resuming,
            user_question=user_question,
            input_tables=[t.get("name", "") for t in input_tables],
            model=getattr(self.client, "model", None),
            rules_injected=[r for r in [self.agent_exploration_rules, self.agent_coding_rules] if r],
        )
        try:
            with checkpoints.open_checkpointer(self.identity_id, checkpoint_db_path) as saver:
                graph = AnalystGraph(self).build().compile(checkpointer=saver)
                config = {"configurable": {"thread_id": run_id}, "recursion_limit": RECURSION_LIMIT}

                if resuming:
                    if not checkpoints.has_run(saver, run_id):
                        outcome = "expired"
                        yield self._error_event(
                            0,
                            "This conversation step has expired. Please ask your question again.",
                            message_code="agent.runExpired",
                        )
                        return
                    self.executor.open(restore=True)
                    graph_input: Any = Command(resume=user_question)
                else:
                    self.executor.open()
                    graph_input = {
                        "run_id": run_id,
                        "user_question": user_question,
                        "input_tables": input_tables,
                        "focused_thread": focused_thread,
                        "other_threads": other_threads,
                        "primary_tables": primary_tables,
                        "attached_images": attached_images,
                        "scratch_files": scratch_files,
                        "conversation_id": conversation_id,
                        "charts": list(charts or []),
                        "max_iterations": self.max_iterations,
                    }

                # Keep a reference to the stream: if the client goes away (Stop),
                # closing it waits for the running node, so tell running
                # sub-agents to stop first.
                stream = graph.stream(graph_input, config, stream_mode="custom", durability="exit")
                try:
                    for event in stream:
                        if not isinstance(event, dict):
                            continue
                        trace.observe(event)
                        if event.get("type") in _TERMINAL_EVENTS:
                            held.append(event)
                        else:
                            yield event
                except GeneratorExit:
                    self.run_ended.set()
                    stream.close()
                    raise

                snapshot = graph.get_state(config)
                if snapshot.next:
                    outcome = "paused"
                    checkpoints.touch_run(saver, run_id)
                    self.executor.suspend()
                else:
                    outcome = snapshot.values.get("status") or "success"
                    checkpoints.forget_run(saver, run_id)
                    self.executor.close(discard_saved=True)
            yield from held
        finally:
            self.run_ended.set()
            trace.save(outcome)
            if self.executor is not None:
                self.executor.close()
            rlog.log(
                "session_end",
                status=outcome,
                run_id=run_id,
                total_latency_ms=int((time.time() - started) * 1000),
            )
            rlog.close()

    # ==================================================================
    # Skills: loading, gating, dispatch
    # ==================================================================

    def _legal_actions(self, loaded: set[str]) -> frozenset[str]:
        legal: set[str] = set()
        for name in loaded:
            meta = self.registry.metas.get(name)
            if meta:
                legal.update(meta.action_names)
        return frozenset(legal)

    def _current_tools(self, loaded: set[str]) -> list[dict[str, Any]]:
        """Inspection tools (core + load_skill + loaded skills' tools) plus the
        action tools of loaded skills."""
        return build_tools(
            self.registry.gated_skill_names(),
            [*self.registry.tools_for(loaded), plan_tool()],
            action_tools=self.registry.action_tools_for(loaded),
        )

    def _loaded_skill_tool_map(self, loaded: set[str]) -> dict[str, Any]:
        mapping: dict[str, Any] = {}
        for name in loaded:
            skill = self.registry.get_skill(name)
            if skill is None:
                continue
            for spec in self.registry.tools_for([name]):
                fn_name = spec.get("function", {}).get("name")
                if fn_name:
                    mapping[fn_name] = skill
        return mapping

    def _build_skill_body_message(
        self, name: str, loaded: list[str] | set[str],
    ) -> tuple[bool, str, dict | None]:
        """Resolve a skill's ``SKILL.md`` body into a ``user`` message.

        Returns ``(ok, message, body_msg)``; ``body_msg`` is ``None`` when the
        skill is already loaded. The caller appends it after the round's tool
        results so the message order stays provider-valid.
        """
        requested = name
        name = self.registry.canonical_name(name)
        if not self.registry.has(name):
            return False, f"Unknown skill: {requested!r}", None
        if name in loaded:
            return True, f"Skill '{name}' already loaded.", None
        try:
            body = self.registry.load_body(name)
        except Exception as e:  # noqa: BLE001
            logger.warning("[AnalystAgent] Failed to load skill body %s", name, exc_info=True)
            return False, f"Failed to load skill {name!r}: {e}", None

        meta = self.registry.metas[name]
        unlocks = ", ".join(meta.action_names) if meta.action_names else "(none)"
        tool_names = [
            spec.get("function", {}).get("name") for spec in self.registry.tools_for([name])
        ]
        tool_names = [t for t in tool_names if t]
        tools_line = f" New tools available: {', '.join(tool_names)}.\n" if tool_names else ""
        body_msg = {
            "role": "user",
            "content": (
                f"{SKILL_LOADED_BANNER.format(name=name)} You can now use the action(s): {unlocks}.\n"
                f"{tools_line}\n{body}"
            ),
        }
        return True, f"Skill '{name}' loaded; unlocked: {unlocks}.", body_msg

    def _begin_payload(self, state: dict[str, Any]) -> None:
        """Expose run data to skill handlers for one node execution."""
        self._run_payload = {
            "input_tables": state.get("input_tables") or [],
            "charts": [dict(c) for c in (state.get("charts") or [])],
            "focused_thread": state.get("focused_thread"),
            "other_threads": state.get("other_threads"),
            "primary_tables": state.get("primary_tables"),
            "conversation_id": state.get("conversation_id", ""),
            # Shared by all skill calls of this process run (not checkpointed).
            "skill_state": self._skill_state,
        }

    def _end_payload(self) -> list[dict[str, Any]]:
        """Charts after the node (a visualize may have registered new ones)."""
        return list(self._run_payload.get("charts") or [])

    def _skill_context(self, messages: list[dict], **extra_payload: Any) -> SkillContext:
        return SkillContext(
            client=self.client,
            workspace=self.workspace,
            language_instruction=self.language_instruction,
            trajectory=messages,
            payload={**self._run_payload, **extra_payload},
            runtime=self,
        )

    def _dispatch_skill_action(
        self,
        skill_name: str,
        action_type: str,
        action: dict[str, Any],
        messages: list[dict],
        iteration: int,
        completed_steps: list[dict[str, Any]],
        *,
        narration: str = "",
        suppress_channel: str | None = None,
        write: Callable[[dict[str, Any]], None],
        run_id: str = "",
    ) -> str | None:
        """Let the owning skill render the action; route its events; return the
        observation (``None`` means the run pauses for the user)."""
        skill = self.registry.get_skill(skill_name)
        if skill is None or not hasattr(skill, "handle_action"):
            self._reasoning_log.log("action_execution", action=action_type, status="no_handler",
                                    iteration=iteration, skill=skill_name)
            write(self._error_event(
                iteration, f"Skill '{skill_name}' has no handler for '{action_type}'.",
                message_code="agent.skillNoHandler",
            ))
            return (
                f"[SKILL ERROR] The '{skill_name}' skill cannot render "
                f"'{action_type}'. Choose a core action instead."
            )
        ctx = self._skill_context(
            messages,
            completed_step_count=len(completed_steps),
            action_narration=narration,
        )
        self._reasoning_log.log("action_execution", action=action_type, status="ok",
                                iteration=iteration, skill=skill_name)
        return self._route_skill_events(
            skill.handle_action(action_type, action, ctx),
            iteration, completed_steps, suppress_channel, write, run_id,
        )

    @staticmethod
    def _route_skill_events(
        gen: Generator[Event, None, str | None],
        iteration: int,
        completed_steps: list[dict[str, Any]],
        suppress_channel: str | None,
        write: Callable[[dict[str, Any]], None],
        run_id: str,
    ) -> str | None:
        """Forward a skill's events to the client and return its observation.

        * stamps ``iteration`` on every event;
        * records each ``result`` as a completed visualization step;
        * stamps ``run_id`` on ``interact`` (the client resumes with it);
        * drops ``error`` events — they are recoverable, and the matching
          observation already tells the model what went wrong;
        * drops the buffered re-emission of content that was already streamed
          live (the ``action`` event and its channel's ``text_delta``).
        """
        try:
            ev = next(gen)
            while True:
                ev.setdefault("iteration", iteration)
                etype = ev.get("type")
                drop = etype == "error" or (bool(suppress_channel) and (
                    etype == "action"
                    or (etype == "text_delta" and ev.get("channel") == suppress_channel)
                ))
                if not drop:
                    if etype == "result":
                        content = ev.get("content", {}) or {}
                        result = content.get("result") or {}
                        completed_steps.append({
                            "display_instruction": content.get("question", ""),
                            "code": result.get("code", ""),
                        })
                    elif etype == "interact":
                        ev["run_id"] = run_id
                        ev.setdefault("completed_step_count", len(completed_steps))
                    write(ev)
                ev = gen.send(None)
        except StopIteration as stop:
            return stop.value

    # ==================================================================
    # Runtime facade — what skills call through ``ctx.runtime``
    # ==================================================================

    def run_explore_code(self, code: str, input_tables: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return self.executor.run_explore_code(code)

    def run_visualize_code(self, messages: list[dict] | None = None, **kwargs) -> dict[str, Any]:
        """Run visualize code, then self-check the chart before it is shown.

        A blocking problem withholds the chart and returns a repairable error
        the model reads as its observation (up to ``MAX_CHART_RETRIES``
        consecutive times); after that, or for mere warnings, the chart is shown
        and the findings are attached to it and to the observation.
        """
        result = self.executor.run_visualize_code(dialog=snapshot_dialog(messages), **kwargs)
        df = result.pop("dataframe", None)
        if result.get("status") != "ok" or df is None:
            return result

        transform_result = result["transform_result"]
        report = chart_check.check_chart(df, kwargs.get("chart_spec") or {})
        display_instruction = kwargs.get("display_instruction", "")

        if report.blocking and self._chart_check_failures < chart_check.MAX_CHART_RETRIES:
            self._chart_check_failures += 1
            self._write({
                "type": "chart_check",
                "status": "retrying",
                "attempt": self._chart_check_failures,
                "display_instruction": display_instruction,
                "issues": report.to_public(),
            })
            return {
                "status": "error",
                "error_message": chart_check.describe_for_model(report, retrying=True),
                "error_code": "agent.chartCheckFailed",
            }

        if report.issues:
            status = "warning"
        elif self._chart_check_failures:
            status = "fixed"
        else:
            status = "passed"
        transform_result["chart_check"] = {
            "status": status,
            "attempts": self._chart_check_failures + 1,
            "issues": report.to_public(),
        }
        if status != "passed":
            self._write({
                "type": "chart_check",
                "status": status,
                "attempt": self._chart_check_failures + 1,
                "display_instruction": display_instruction,
                "chart_id": transform_result.get("chart_id"),
                "issues": report.to_public(),
            })
        if report.issues:
            self._chart_check_note = chart_check.describe_for_model(report, retrying=False)
        self._chart_check_failures = 0
        return result

    def register_run_chart(self, transform_result: dict[str, Any], chart_spec: dict[str, Any]) -> None:
        """Register a chart created mid-run so later actions (e.g. a same-run
        report) can reference and inspect it by its id."""
        chart_id = transform_result.get("chart_id")
        if not chart_id:
            return
        content = transform_result.get("content", {}) or {}
        table_name = (content.get("virtual", {}) or {}).get("table_name", "")
        rows = content.get("rows", []) or []
        charts = self._run_payload.setdefault("charts", [])
        if any(c.get("chart_id") == chart_id for c in charts):
            return
        charts.append({
            "chart_id": chart_id,
            "chart_type": chart_spec.get("type") or chart_spec.get("chart_type") or "Unknown",
            "encodings": dict(chart_spec.get("encodings", {}) or {}),
            "table_ref": table_name,
            "code": transform_result.get("code", ""),
            "chart_data": {"name": table_name, "rows": rows[:50]},
        })

    @staticmethod
    def _error_event(
        iteration: int,
        message: str,
        *,
        display_instruction: str = "",
        message_code: str = "",
        message_params: dict | None = None,
    ) -> dict[str, Any]:
        event: dict[str, Any] = {"type": "error", "iteration": iteration, "message": message}
        if message_code:
            event["message_code"] = message_code
        if message_params:
            event["message_params"] = message_params
        if display_instruction:
            event["display_instruction"] = display_instruction
        return event

    # ==================================================================
    # Initial context
    # ==================================================================

    def _build_system_prompt(
        self,
        has_primary_tables: bool = False,
        has_focused_thread: bool = False,
        has_other_threads: bool = False,
        has_attached_images: bool = False,
        has_charts: bool = False,
    ) -> str:
        rules_block = ""
        if self.agent_exploration_rules and self.agent_exploration_rules.strip():
            rules_block = (
                "\n## Additional exploration rules\n\n"
                + self.agent_exploration_rules.strip()
                + "\n\nPlease follow the above rules when exploring data."
            )

        context_lines = []
        if has_primary_tables:
            context_lines.append(
                "- **[PRIMARY TABLE(S)]**: The table(s) the user is focused on. "
                "Prioritize these, but freely use other available tables if needed."
            )
            context_lines.append(
                "- **[OTHER AVAILABLE TABLES]**: Additional tables in the workspace."
            )
        else:
            context_lines.append(
                "- **[AVAILABLE TABLES]**: All tables in the workspace."
            )
        context_lines.append(
            "  Use `inspect_source_data` to get detailed stats and sample rows. "
            "Use `execute_python_script` for custom computations."
        )
        if has_focused_thread:
            context_lines.append(
                "- **[FOCUSED THREAD]**: The thread the user is continuing. "
                "Build on this — do not repeat visualizations already created here."
            )
        if has_other_threads:
            context_lines.append(
                "- **[OTHER THREADS]**: Brief summaries of other exploration threads in this workspace. "
            )
        if has_charts:
            context_lines.append(
                "- **[AVAILABLE CHARTS]**: Charts the user already created (with their "
                "ids, types, and encodings). These already exist — build on them or "
                "reference them; do not re-create an equivalent chart. When asked to "
                "write up / summarize / report on the exploration, load the `report` "
                "skill and embed these by id rather than producing new visualizations."
            )
        if has_attached_images:
            context_lines.append(
                "- **[USER ATTACHMENT(S)]**: Image(s) provided by the user. "
                "Refer to these when relevant to the user's question."
            )
        context_guide = "\n".join(context_lines)

        # The skill catalog is static capability config (fixed at agent build,
        # independent of the user's question), so it belongs in the frame next to
        # the skills mechanism — not in the per-run user message. The only truly
        # dynamic skill data is a loaded skill body, which arrives as a
        # ``load_skill`` tool result.
        skills_block = self.registry.render_registry_block() or "_(no loadable skills)_"

        # Fill the system frame's slots via plain substitution (brace-safe: any
        # other braces in the text stay literal). The frame is the agent's own
        # contract — identity, tools-vs-actions, skills mechanism, budget.
        substitutions = {
            "{context_guide}": context_guide,
            "{skills_block}": skills_block,
            "{max_iterations}": str(self.max_iterations),
            "{planning}": planning_prompt(),
            "{agent_exploration_rules}": rules_block,
        }
        prompt = SYSTEM_PROMPT
        for slot, value in substitutions.items():
            prompt = prompt.replace(slot, value)

        # Append the always-loaded ``core`` skill's capability body (the concrete
        # tools + action schemas). It is plain content — no placeholders — and is
        # framed with the same ``[SKILL: <name>]`` header as on-demand skills (see
        # _load_skill_into_context) so every capability bundle reads as one family:
        # core is the always-active baseline, gated skills announce themselves when
        # loaded.
        core_body = self.registry.load_body(CORE_SKILL)
        prompt += (
            f"\n\n[SKILL: {CORE_SKILL}] Always-on baseline — these tools and "
            f"actions are active for the whole run.\n\n{core_body}"
        )

        if self._knowledge_store:
            knowledge_rules = self._knowledge_store.load_always_apply_rules()
            self._injected_rules = [r["title"] for r in knowledge_rules]
            prompt += self._knowledge_store.format_rules_block(knowledge_rules)
        else:
            self._injected_rules = []

        if self.agent_coding_rules and self.agent_coding_rules.strip():
            prompt += (
                "\n\n## Agent Coding Rules\n\n"
                + self.agent_coding_rules.strip()
            )

        if self.language_instruction:
            prompt = prompt + "\n\n" + self.language_instruction
        return prompt

    def _build_initial_messages(
        self,
        input_tables: list[dict[str, Any]],
        user_question: str,
        focused_thread: list[dict[str, Any]] | None = None,
        other_threads: list[dict[str, Any]] | None = None,
        primary_tables: list[str] | None = None,
        attached_images: list[str] | None = None,
        charts: list[dict[str, Any]] | None = None,
        scratch_files: list[str] | None = None,
    ) -> list[dict]:
        """Build the initial messages with 3-tier context."""
        table_summaries = self._build_lightweight_table_context(input_tables, primary_tables=primary_tables)

        focused_block = ""
        if focused_thread:
            focused_block = self._build_focused_thread_context(focused_thread)

        peripheral_block = ""
        if other_threads:
            peripheral_block = self._build_peripheral_thread_context(other_threads)

        if primary_tables:
            user_content = f"{table_summaries}\n\n"
        else:
            user_content = f"[AVAILABLE TABLES]\n\n{table_summaries}\n\n"
        if focused_block:
            user_content += f"{focused_block}\n\n"
        if peripheral_block:
            user_content += f"{peripheral_block}\n\n"

        # Surface the charts the user already created so the agent treats them as
        # existing material — to build on, reference, or report from — rather than
        # re-creating them. The chart_ids here are exactly what the report skill's
        # ``inspect_chart`` / ``![caption](chart://chart_id)`` embeds expect.
        charts_block = self._build_available_charts_context(charts)
        if charts_block:
            user_content += f"{charts_block}\n\n"

        self._injected_knowledge = []
        if self._knowledge_store:
            always_apply_rules = self._knowledge_store.load_always_apply_rules()
            if always_apply_rules:
                rules_text = "\n\n".join([f"### {r['title']}\n{r['body']}" for r in always_apply_rules])
                user_content += f"[USER RULES - MUST FOLLOW]\n\n{rules_text}\n\n"

        # Non-image attachments were uploaded to the workspace scratch/ folder
        # (raw bytes). Surface them and the two natural uses: read as context
        # for analysis, or bring into the workspace via data loading (which
        # shares scratch/ access). Keep it neutral — the agent picks.
        if scratch_files:
            file_list = "\n".join(f"- {p}" for p in scratch_files)
            user_content += (
                "[ATTACHED FILES]\n\n"
                "The user attached file(s), saved in the workspace scratch/ "
                "folder:\n"
                f"{file_list}\n\n"
                "Read them with execute_python_script "
                "(e.g. pd.read_excel('scratch/<name>') or "
                "pd.read_csv('scratch/<name>')) to use as temporary context for "
                "your analysis. Only tables materialized by a supported data "
                "operation become workspace inputs.\n\n"
            )

        user_content += f"[USER QUESTION]\n\n{user_question}"

        system_prompt = self._build_system_prompt(
            has_primary_tables=bool(primary_tables),
            has_focused_thread=bool(focused_thread),
            has_other_threads=bool(other_threads),
            has_attached_images=bool(attached_images),
            has_charts=bool(charts_block),
        )

        has_images = bool(attached_images) and len(attached_images) > 0

        if has_images:
            content_parts: list[dict] = [{"type": "text", "text": user_content}]
            label = "[USER ATTACHMENT]" if len(attached_images) == 1 else "[USER ATTACHMENTS]"
            content_parts.append({"type": "text", "text": f"\n{label} (image(s) provided by the user):"})
            for img in attached_images:
                if img.startswith("data:"):
                    content_parts.append({"type": "image_url", "image_url": {"url": img, "detail": "low"}})
            return [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content_parts},
            ]
        else:
            return [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ]

    def _build_focused_thread_context(
        self, focused_thread: list[dict[str, Any]]
    ) -> str:
        return build_focused_thread_context(focused_thread)

    def _build_peripheral_thread_context(
        self, other_threads: list[dict[str, Any]]
    ) -> str:
        return build_peripheral_thread_context(other_threads)

    @staticmethod
    def _build_available_charts_context(charts: list[dict[str, Any]] | None) -> str:
        return build_available_charts_context(charts)

    def _build_lightweight_table_context(
        self, input_tables: list[dict[str, Any]], primary_tables: list[str] | None = None
    ) -> str:
        return build_lightweight_table_context(
            input_tables,
            self.workspace,
            primary_tables,
        )

