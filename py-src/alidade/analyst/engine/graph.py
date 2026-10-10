# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""The analyst agent as a LangGraph state graph.

::

    START → prepare → agent ─┬─ tools ──────→ agent
                             ├─ act ─┬──────→ agent
                             │       ├─ await_user (interrupt) → agent
                             │       └─ finish
                             └─ finish → END

* ``agent``      one streaming LLM call; decides the next hop from the reply:
                 inspection tool calls → ``tools``; a committing action →
                 ``act``; plain text → ``finish`` (that text is the answer).
* ``tools``      runs inspection tools (``execute_python_script``,
                 ``inspect_source_data``, ``load_skill``, skill tools).
* ``act``        gates and dispatches the single committed action to the skill
                 that owns it and feeds its observation back to the model.
* ``await_user`` pauses the run with ``interrupt()`` after ``ask_user``; the
                 client resumes it with the run id and the user's reply.
* ``finish``     emits the terminal ``completion`` / ``error`` event.

Built-in workflow features:

* **plan-then-execute** — the agent lays out multi-step work with the
  ``update_plan`` tool first and keeps it current (streamed as ``plan``);
* **chart self-check** — every visualize result is checked by rules before it
  is shown; blocking problems go back to the model for a fix
  (see :mod:`.chart_check`, streamed as ``chart_check``);
* **context compression** — before each LLM call the conversation is trimmed /
  summarised once it exceeds the token budget (see :mod:`.compression`,
  streamed as ``context_compressed``).

Events reach the client through LangGraph's custom stream
(``get_stream_writer``); the wire format is unchanged from the previous loop.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any, Callable

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from alidade.agents.context import handle_inspect_source_data
from alidade.analyst.engine import compression, llm, planning
from alidade.analyst.engine.prompts import CORE_SKILL
from alidade.analyst.engine.state import AnalystState
from alidade.analyst.skills import SkillContext, ToolResult

if TYPE_CHECKING:  # pragma: no cover
    from alidade.analyst.agent import AnalystAgent

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 12          # inspection rounds allowed between two actions
RECURSION_LIMIT = 1000        # graph super-steps; the budgets below stop far earlier


def hard_ceiling(max_iterations: int) -> int:
    """Upper bound on action cycles (actions + rejected/gated attempts)."""
    return max(max_iterations * 3, 12)


def _writer() -> Callable[[dict[str, Any]], None]:
    try:
        return get_stream_writer()
    except Exception:  # noqa: BLE001 — called outside a streaming run (tests)
        return lambda _event: None


def _copy_messages(messages: list[dict]) -> list[dict]:
    return [dict(m) for m in messages]


def _set_observation(messages: list[dict], tool_call_id: str | None, observation: str | None) -> None:
    """Fill the committed action's placeholder tool result with its observation."""
    text = observation if observation else "ok"
    if tool_call_id:
        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            if msg.get("role") == "tool" and msg.get("tool_call_id") == tool_call_id:
                messages[i] = {**msg, "content": text}
                return
    messages.append({"role": "user", "content": text})


class AnalystGraph:
    """Builds the graph; nodes close over the agent runtime (client, workspace,
    skill registry, sandbox executor), which never enters checkpointed state."""

    def __init__(self, agent: "AnalystAgent"):
        self.agent = agent
        self.registry = agent.registry

    # ── wiring ─────────────────────────────────────────────────────────

    def build(self) -> StateGraph:
        g = StateGraph(AnalystState)
        g.add_node("prepare", self.prepare)
        g.add_node("agent", self.agent_step)
        g.add_node("tools", self.tools_step)
        g.add_node("act", self.act_step)
        g.add_node("await_user", self.await_user)
        g.add_node("finish", self.finish)

        g.add_edge(START, "prepare")
        g.add_edge("prepare", "agent")
        g.add_conditional_edges("agent", _route, ["tools", "act", "agent", "finish"])
        g.add_edge("tools", "agent")
        g.add_conditional_edges("act", _route, ["agent", "await_user", "finish"])
        g.add_edge("await_user", "agent")
        g.add_edge("finish", END)
        return g

    # ── nodes ──────────────────────────────────────────────────────────

    def prepare(self, state: AnalystState) -> dict[str, Any]:
        agent = self.agent
        write = _writer()
        messages = agent._build_initial_messages(
            state.get("input_tables") or [],
            state.get("user_question", ""),
            state.get("focused_thread"),
            state.get("other_threads"),
            primary_tables=state.get("primary_tables"),
            attached_images=state.get("attached_images"),
            charts=state.get("charts"),
            scratch_files=state.get("scratch_files"),
        )
        agent._reasoning_log.log(
            "context_built",
            system_prompt_tokens=len(messages[0].get("content", "")) // 4 if messages else 0,
            user_msg_tokens=len(str(messages[1].get("content", ""))) // 4 if len(messages) > 1 else 0,
            total_tables=len(state.get("input_tables") or []),
            primary_tables=state.get("primary_tables") or [],
            knowledge_rules_injected=agent._injected_rules,
            knowledge_injected=agent._injected_knowledge,
        )
        if agent._injected_rules or agent._injected_knowledge:
            write({
                "type": "context_info",
                "rules_injected": agent._injected_rules,
                "knowledge_injected": [
                    {"category": k["category"], "title": k["title"]}
                    for k in agent._injected_knowledge
                ],
            })
        return {
            "messages": messages,
            "loaded_skills": [CORE_SKILL],
            "completed_steps": [],
            "tool_rounds": 0,
            "cycles": 0,
            "actions_committed": 0,
            "pending_action": None,
            "pending_tool_calls": [],
            "streamed_channels": {},
            "chart_retries": {},
            "plan": [],
            "compressed_upto": 0,
        }

    def agent_step(self, state: AnalystState) -> dict[str, Any]:
        agent = self.agent
        write = _writer()
        rlog = agent._reasoning_log
        cycles = state.get("cycles", 0)
        max_iterations = state.get("max_iterations", agent.max_iterations)

        if cycles >= hard_ceiling(max_iterations):
            return {"route": "finish", "status": "max_iterations",
                    "final_text": "Reached the maximum number of exploration steps."}

        tool_rounds = state.get("tool_rounds", 0) + 1
        if tool_rounds > MAX_TOOL_ROUNDS:
            logger.warning("[AnalystGraph] Exceeded %d tool rounds without an action", MAX_TOOL_ROUNDS)
            return {"route": "finish", "status": "tool_rounds_exhausted", "final_text": ""}

        loaded = set(state.get("loaded_skills") or [CORE_SKILL])
        tools = agent._current_tools(loaded)
        messages = _copy_messages(state.get("messages") or [])
        messages = self._compress(messages, state, write)

        rlog.log("llm_request", iteration=cycles + 1, round=tool_rounds,
                 messages_count=len(messages),
                 tools_available=[t["function"]["name"] for t in tools])
        t0 = time.time()
        try:
            resp = llm.stream_completion(
                agent.client, messages, tools, self.registry.action_stream_spec, write,
            )
        except Exception as exc:  # noqa: BLE001 — surfaced as a run error
            from alidade.security.sanitize import classify_llm_error
            logger.error("[AnalystGraph] LLM call failed", exc_info=exc)
            rlog.log("llm_response", iteration=cycles + 1, round=tool_rounds,
                     latency_ms=int((time.time() - t0) * 1000),
                     finish_reason="error", error=type(exc).__name__)
            return {"route": "finish", "status": "llm_error", "messages": messages,
                    "error_message": classify_llm_error(exc)}

        latency = int((time.time() - t0) * 1000)
        content = resp.content or ""
        tool_calls = resp.tool_calls or []
        rlog.log("llm_response", iteration=cycles + 1, round=tool_rounds, latency_ms=latency,
                 finish_reason="tool_calls" if tool_calls else resp.finish_reason,
                 tool_calls=[{"name": tc["function"]["name"]} for tc in tool_calls] or None)

        if not tool_calls:
            # Plain text, no action: the run's closing answer.
            messages.append(llm.assistant_message(content, None, resp.reasoning_content))
            return {"route": "finish", "status": "success", "messages": messages,
                    "final_text": content.strip(), "tool_rounds": tool_rounds}

        if content.strip():
            write({"type": "thinking_text", "content": content.strip()})

        action_names = self.registry.action_names()
        action_calls = [tc for tc in tool_calls if tc["function"]["name"] in action_names]
        readonly_calls = [tc for tc in tool_calls if tc["function"]["name"] not in action_names]

        if not action_calls:
            messages.append(llm.assistant_message(content, readonly_calls, resp.reasoning_content))
            return {"route": "tools", "messages": messages, "pending_tool_calls": readonly_calls,
                    "tool_rounds": tool_rounds}

        return self._commit_action(
            action_calls, readonly_calls, messages, content, resp, cycles, tool_rounds, write,
        )

    def _commit_action(self, action_calls, readonly_calls, messages, content, resp,
                       cycles, tool_rounds, write) -> dict[str, Any]:
        """One action per turn, first-wins: keep the first action, drop the rest
        (and any inspection calls made alongside it), and tell the model so."""
        rlog = self.agent._reasoning_log
        chosen = action_calls[0]
        name = chosen["function"]["name"]
        dropped_actions = [tc["function"]["name"] for tc in action_calls[1:]]
        dropped_readonly = [tc["function"]["name"] for tc in readonly_calls]

        try:
            args = json.loads(chosen["function"]["arguments"] or "{}")
        except json.JSONDecodeError:
            args = {}
        if not isinstance(args, dict):
            args = {}
        llm.rescue_unpack_json_strings(args)
        args["action"] = name

        messages.append(llm.assistant_message(content, [chosen], resp.reasoning_content))

        missing = [f for f in self.registry.action_required_fields(name) if not args.get(f)]
        if missing:
            correction = (
                f"The '{name}' action is missing required field(s): {', '.join(missing)}. "
                "Call it again with those fields filled in."
            )
            messages.append({"role": "tool", "tool_call_id": chosen["id"], "content": f"ERROR: {correction}"})
            rlog.log("tool_execution", iteration=cycles + 1, tool=name,
                     input_summary="action_missing_fields", output_summary=", ".join(missing),
                     latency_ms=0, status="error")
            write({"type": "tool_result", "tool": name, "status": "error",
                   "error": f"Missing fields: {', '.join(missing)}"})
            return {"route": "agent", "messages": messages, "tool_rounds": tool_rounds}

        messages.append({"role": "tool", "tool_call_id": chosen["id"], "content": ""})
        if dropped_actions or dropped_readonly:
            parts = []
            if dropped_actions:
                parts.append(f"additional action call(s) ({', '.join(dropped_actions)})")
            if dropped_readonly:
                parts.append(f"inspection call(s) ({', '.join(dropped_readonly)}) made alongside it")
            messages.append({
                "role": "user",
                "content": (
                    f"[SYSTEM] A turn commits exactly one action. Kept '{name}'; ignored "
                    f"{' and '.join(parts)}. Do any inspection in its own round before the "
                    "action, and emit only one action per turn."
                ),
            })

        rlog.log("tool_execution", iteration=cycles + 1, tool=name,
                 input_summary="action_committed", output_summary="ok", latency_ms=0, status="ok")
        return {
            "route": "act",
            "messages": messages,
            "pending_action": {
                "action": name,
                "args": args,
                "tool_call_id": chosen["id"],
                "narration": (content or "").strip(),
            },
            "streamed_channels": dict(resp.streamed_channels or {}),
            "tool_rounds": tool_rounds,
        }

    def _compress(self, messages: list[dict], state: AnalystState, write) -> list[dict]:
        """Keep the conversation under the token budget (see :mod:`.compression`)."""
        agent = self.agent

        def complete(msgs: list[dict]) -> str:
            return llm.plain_completion(agent.client, msgs)

        compressed, info = compression.compress_if_needed(messages, complete)
        if info is None:
            return messages
        plan = state.get("plan") or []
        if plan and info.get("stage") == "summary":
            # The plan's update_plan calls may have been summarised away.
            for i, m in enumerate(compressed):
                if isinstance(m.get("content"), str) and m["content"].startswith("[EARLIER STEPS SUMMARY]"):
                    compressed[i] = {**m, "content": m["content"] + "\n\nCurrent " + planning.summarize_plan(plan)}
                    break
        agent._reasoning_log.log("context_compressed", **info)
        logger.info("[AnalystGraph] Context compressed: %s", info)
        write({"type": "context_compressed", **info})
        return compressed

    def tools_step(self, state: AnalystState) -> dict[str, Any]:
        agent = self.agent
        write = _writer()
        rlog = agent._reasoning_log
        cycles = state.get("cycles", 0)
        messages = _copy_messages(state.get("messages") or [])
        loaded = list(state.get("loaded_skills") or [CORE_SKILL])
        input_tables = state.get("input_tables") or []
        plan = list(state.get("plan") or [])
        agent._begin_payload(state)

        skill_tool_owners = agent._loaded_skill_tool_map(set(loaded))
        pending_images: list[str] = []
        pending_skill_bodies: list[dict] = []  # must land AFTER all tool results

        for tc in state.get("pending_tool_calls") or []:
            tool_name = tc["function"]["name"]
            try:
                tool_args = json.loads(tc["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                tool_args = {}
            if not isinstance(tool_args, dict):
                tool_args = {}

            write({
                "type": "tool_start",
                "tool": tool_name,
                "purpose": tool_args.get("purpose") if tool_name == "execute_python_script" else None,
                "code": tool_args.get("code") if tool_name == "execute_python_script" else None,
                "table_names": tool_args.get("table_names") if tool_name == "inspect_source_data" else None,
                "skill": tool_args.get("name") if tool_name == "load_skill" else None,
            })
            t0 = time.time()
            status = "ok"

            if tool_name == "execute_python_script":
                result = agent.executor.run_explore_code(tool_args.get("code", ""))
                tool_content = result.get("stdout", "")
                status = result.get("status", "ok")
                if result.get("error"):
                    tool_content += f"\n\nError: {result['error']}"
                write({"type": "tool_result", "tool": tool_name, "status": status,
                       "stdout": result.get("stdout", ""), "error": result.get("error")})
            elif tool_name == "update_plan":
                plan = planning.normalize_steps(tool_args.get("steps"))
                tool_content = planning.summarize_plan(plan)
                write({"type": "plan", "steps": plan})
                write({"type": "tool_result", "tool": tool_name, "status": "ok", "stdout": tool_content})
            elif tool_name == "inspect_source_data":
                tool_content = handle_inspect_source_data(
                    tool_args.get("table_names", []), input_tables, agent.workspace,
                )
                write({"type": "tool_result", "tool": tool_name, "status": "ok", "stdout": tool_content})
            elif tool_name == "load_skill":
                skill_name = self.registry.canonical_name(tool_args.get("name", ""))
                ok, message, body_msg = agent._build_skill_body_message(skill_name, loaded)
                status = "ok" if ok else "error"
                tool_content = message
                if ok and body_msg is not None:
                    pending_skill_bodies.append(body_msg)
                    loaded.append(skill_name)
                if ok:
                    write({
                        "type": "skill_loaded",
                        "skill": skill_name,
                        "unlocks": list(self.registry.metas[skill_name].action_names)
                        if self.registry.has(skill_name) else [],
                    })
                write({"type": "tool_result", "tool": tool_name, "status": status,
                       "stdout": message, "error": None if ok else message})
            elif tool_name in skill_tool_owners:
                skill = skill_tool_owners[tool_name]
                ctx = agent._skill_context(messages)
                try:
                    result = skill.handle_tool(tool_name, tool_args, ctx)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[AnalystGraph] Skill tool %r failed", tool_name, exc_info=exc)
                    result = ToolResult(text=f"Tool '{tool_name}' failed: {exc}")
                    status = "error"
                tool_content = result.text
                if result.images:
                    pending_images.extend(result.images)
                write({"type": "tool_result", "tool": tool_name, "status": status, "stdout": tool_content})
            else:
                tool_content = f"Unknown tool: {tool_name}"
                status = "error"

            summary = tool_content if len(tool_content) <= 200 else tool_content[:200] + "..."
            rlog.log("tool_execution", iteration=cycles + 1, tool=tool_name,
                     input_summary=str(tool_args.get("purpose", ""))[:200],
                     output_summary=summary, latency_ms=int((time.time() - t0) * 1000),
                     status=status)
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": tool_content})

        if pending_images:
            blocks: list[dict[str, Any]] = [{
                "type": "text",
                "text": "[INSPECTED IMAGE(S)] Rendered images for the tool call(s) you just made, in request order:",
            }]
            for url in pending_images:
                blocks.append({"type": "image_url", "image_url": {"url": url, "detail": "high"}})
            messages.append({"role": "user", "content": blocks})
        messages.extend(pending_skill_bodies)

        return {"messages": messages, "loaded_skills": loaded, "pending_tool_calls": [],
                "charts": agent._end_payload(), "plan": plan}

    def act_step(self, state: AnalystState) -> dict[str, Any]:
        agent = self.agent
        write = _writer()
        rlog = agent._reasoning_log
        pending = state.get("pending_action") or {}
        name = pending.get("action", "")
        tool_call_id = pending.get("tool_call_id")
        cycles = state.get("cycles", 0) + 1
        messages = _copy_messages(state.get("messages") or [])
        loaded = set(state.get("loaded_skills") or [CORE_SKILL])
        update: dict[str, Any] = {"cycles": cycles, "pending_action": None, "streamed_channels": {}}

        owner = self.registry.action_owner(name)
        if owner is None:
            legal = ", ".join(sorted(agent._legal_actions(loaded)))
            _set_observation(messages, tool_call_id,
                             f"[ERROR] Unknown action '{name}'. Choose one of: {legal}, "
                             "or load a skill that unlocks the action you need.")
            write(agent._error_event(cycles, f"Unknown action: {name}", message_code="agent.unknownAction"))
            return {**update, "messages": messages, "route": "agent"}
        if owner not in loaded:
            _set_observation(messages, tool_call_id,
                             f"[GATED] The '{name}' action requires the '{owner}' skill. "
                             f"Call load_skill(\"{owner}\") first, follow its instructions, "
                             "then emit the action again.")
            rlog.log("action_gated", action=name, skill=owner, iteration=cycles)
            return {**update, "messages": messages, "route": "agent"}

        completed_steps = list(state.get("completed_steps") or [])
        suppress = (state.get("streamed_channels") or {}).get(tool_call_id or "")
        agent._begin_payload(state)
        agent._write = write
        failures_before = int((state.get("chart_retries") or {}).get("consecutive", 0))
        agent._chart_check_failures = failures_before
        agent._chart_check_note = ""
        observation = agent._dispatch_skill_action(
            owner, name, pending.get("args") or {}, messages, cycles, completed_steps,
            narration=pending.get("narration", ""), suppress_channel=suppress, write=write,
            run_id=state.get("run_id", ""),
        )
        if observation is not None and agent._chart_check_note:
            observation = f"{observation}\n\n{agent._chart_check_note}"
        _set_observation(messages, tool_call_id, observation)
        update.update({"messages": messages, "completed_steps": completed_steps,
                       "charts": agent._end_payload(), "tool_rounds": 0,
                       "chart_retries": {"consecutive": agent._chart_check_failures}})

        if observation is None:
            # ask_user rendered its question widget: pause for the reply.
            return {**update, "route": "await_user"}

        # A chart withheld by the self-check is a repair round, not a new step:
        # it does not consume the action budget (cycles still bound the loop).
        withheld = agent._chart_check_failures > failures_before
        actions_committed = state.get("actions_committed", 0) + (0 if withheld else 1)
        update["actions_committed"] = actions_committed
        remaining = state.get("max_iterations", agent.max_iterations) - actions_committed
        if remaining <= 0:
            return {**update, "route": "finish", "status": "max_iterations",
                    "final_text": "Reached the maximum number of actions for this run."}
        if remaining == 1 and not withheld:
            messages.append({
                "role": "user",
                "content": (
                    "[SYSTEM] You have 1 action left in your budget. Make it count, or wrap "
                    "up by giving your final answer in plain text (which ends the run)."
                ),
            })
        return {**update, "route": "agent"}

    def await_user(self, state: AnalystState) -> dict[str, Any]:
        reply = interrupt({"run_id": state.get("run_id", "")})
        messages = _copy_messages(state.get("messages") or [])
        messages.append({"role": "user", "content": str(reply or "")})
        return {"messages": messages, "tool_rounds": 0}

    def finish(self, state: AnalystState) -> dict[str, Any]:
        write = _writer()
        status = state.get("status") or "success"
        cycles = state.get("cycles", 0)
        if status == "llm_error":
            write(self.agent._error_event(
                cycles, state.get("error_message") or "LLM API error",
                message_code="agent.llmApiError",
            ))
            return {}
        content: dict[str, Any] = {
            "summary": state.get("final_text", ""),
            "total_steps": len(state.get("completed_steps") or []),
        }
        if status == "max_iterations":
            content["summary_code"] = "agent.maxIterationsSummary"
        write({
            "type": "completion",
            "iteration": cycles,
            "status": status,
            "run_id": state.get("run_id", ""),
            "content": content,
        })
        return {}


def _route(state: AnalystState) -> str:
    return state.get("route") or "finish"
