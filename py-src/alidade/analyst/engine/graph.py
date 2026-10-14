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
import threading
import time
from typing import TYPE_CHECKING, Any, Callable

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from alidade.agents.context import handle_inspect_source_data
from alidade.analyst.engine import compression, llm, planning, routing
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
        g.add_node("dispatch", self.dispatch_step)
        g.add_node("subagent", self.subagent_branch)
        g.add_node("merge", self.merge_step)
        g.add_node("finish", self.finish)

        g.add_edge(START, "prepare")
        g.add_edge("prepare", "agent")
        g.add_conditional_edges("agent", _route, ["tools", "act", "agent", "finish"])
        # parallel sub-agents: dispatch -> Send("subagent", task) per step -> merge
        g.add_conditional_edges("tools", _route_after_tools, ["agent", "dispatch"])
        g.add_conditional_edges("dispatch", _send_subtasks, ["subagent"])
        g.add_edge("subagent", "merge")
        g.add_conditional_edges("merge", _route_after_tools, ["agent", "dispatch"])
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
        if agent._applied_definitions:
            write({
                "type": "definitions_applied",
                "terms": [d["term"] for d in agent._applied_definitions],
                "definitions": [{k: d.get(k) for k in ("id", "term", "meaning", "tables")}
                                for d in agent._applied_definitions],
            })
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
            "after_tools": "agent",
            "dispatched_steps": [],
            "subagent_rounds": 0,
        }

    def agent_step(self, state: AnalystState) -> dict[str, Any]:
        agent = self.agent
        write = _writer()
        rlog = agent._reasoning_log
        cycles = state.get("cycles", 0)
        max_iterations = state.get("max_iterations", agent.max_iterations)

        should_stop = getattr(agent, "should_stop", None)
        if should_stop is not None and should_stop():
            # a sub-agent past its time limit (or the run was stopped)
            return {"route": "finish", "status": "stopped", "final_text": ""}

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
        choice = routing.pick(messages, routing_on=agent.routing_on,
                              sub_agent=should_stop is not None)
        t0 = time.time()
        try:
            resp = self._call_model(choice, messages, tools, write)
            if not resp.tool_calls and choice.tier == routing.FAST:
                # the cheap model wants to end the run: check its answer first
                why = routing.review_final(resp.content or "", messages)
                if why:
                    choice = routing.Choice(routing.STRONG, why)
                    resp = self._call_model(choice, messages, tools, write)
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
            action_calls, readonly_calls, messages, content, resp, cycles, tool_rounds, write, state,
        )

    def _call_model(self, choice: "routing.Choice", messages: list[dict], tools: list[dict],
                    write) -> Any:
        """One streaming LLM call on the model ``choice`` picks; reports it as an
        ``llm_call`` event (model, tier, why, time, tokens) for the run trace."""
        agent = self.agent
        client = agent.client_for(choice.tier)
        effort = agent.effort_for(choice.tier)
        t0 = time.time()
        resp = llm.stream_completion(client, messages, tools, self.registry.action_stream_spec, write,
                                     effort=effort)
        usage = resp.usage or {}
        write({
            "type": "llm_call",
            "model": getattr(client, "model_name", None) or getattr(client, "model", ""),
            "effort": effort,
            "tier": choice.tier,
            "reason": choice.reason,
            "duration_ms": int((time.time() - t0) * 1000),
            "prompt_tokens": usage.get("prompt"),
            "completion_tokens": usage.get("completion"),
            "cached_tokens": usage.get("cached"),
            "tool_calls": [tc["function"]["name"] for tc in resp.tool_calls or []],
        })
        return resp

    def _commit_action(self, action_calls, readonly_calls, messages, content, resp,
                       cycles, tool_rounds, write, state=None) -> dict[str, Any]:
        """One action per turn, first-wins: keep the first action, drop the rest
        (and any inspection calls made alongside it), and tell the model so."""
        rlog = self.agent._reasoning_log
        chosen = action_calls[0]
        name = chosen["function"]["name"]
        # A plan change may ride along with the action (it commits nothing the
        # user has to wait for); any other inspection call next to an action
        # is dropped, since the action could not have used its result.
        # Saving a definition the user just settled may ride along too.
        plan_calls = [tc for tc in readonly_calls if tc["function"]["name"] == "update_plan"]
        save_calls = [tc for tc in readonly_calls if tc["function"]["name"] == "save_definition"]
        readonly_calls = [tc for tc in readonly_calls
                          if tc["function"]["name"] not in ("update_plan", "save_definition")]
        dropped_actions = [tc["function"]["name"] for tc in action_calls[1:]]
        dropped_readonly = [tc["function"]["name"] for tc in readonly_calls]

        raw_args = chosen["function"]["arguments"] or "{}"
        bad_json = False
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError:
            args, bad_json = {}, True
        if not isinstance(args, dict):
            args, bad_json = {}, True
        llm.rescue_unpack_json_strings(args)
        args["action"] = name

        messages.append(llm.assistant_message(content, [*plan_calls, *save_calls, chosen], resp.reasoning_content))
        new_plan = None
        for tc in plan_calls:
            try:
                plan_args = json.loads(tc["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                plan_args = {}
            new_plan = planning.keep_subagent_info(
                planning.normalize_steps((plan_args or {}).get("steps") if isinstance(plan_args, dict) else None),
                (state or {}).get("plan"))
            summary = planning.summarize_plan(new_plan)
            write({"type": "tool_start", "tool": "update_plan"})
            write({"type": "plan", "steps": new_plan})
            write({"type": "tool_result", "tool": "update_plan", "status": "ok", "stdout": summary})
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": summary})
        plan_update = {} if new_plan is None else {"plan": new_plan}
        for tc in save_calls:
            try:
                save_args = json.loads(tc["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                save_args = {}
            save_args = save_args if isinstance(save_args, dict) else {}
            write({"type": "tool_start", "tool": "save_definition", "term": save_args.get("term")})
            text_, status_ = self.agent.save_definition(
                save_args, (state or {}).get("user_question", ""), (state or {}).get("input_tables") or [])
            write({"type": "tool_result", "tool": "save_definition", "status": status_, "stdout": text_,
                   "term": save_args.get("term"), "meaning": save_args.get("meaning")})
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": text_})

        missing = [f for f in self.registry.action_required_fields(name) if not args.get(f)]
        if missing:
            correction = (
                f"The '{name}' action is missing required field(s): {', '.join(missing)}. "
                "Call it again with those fields filled in."
            )
            if bad_json:
                shown = raw_args if len(raw_args) <= 300 else raw_args[:300] + "…"
                correction = (
                    f"The '{name}' arguments were not a valid JSON object, so nothing could be read "
                    f"(received: {shown}). Call it again with a complete JSON object."
                )
            messages.append({"role": "tool", "tool_call_id": chosen["id"], "content": f"ERROR: {correction}"})
            rlog.log("tool_execution", iteration=cycles + 1, tool=name,
                     input_summary="action_missing_fields", output_summary=", ".join(missing),
                     latency_ms=0, status="error")
            write({"type": "tool_result", "tool": name, "status": "error",
                   "error": f"Missing fields: {', '.join(missing)}"})
            return {"route": "agent", "messages": messages, "tool_rounds": tool_rounds, **plan_update}

        messages.append({"role": "tool", "tool_call_id": chosen["id"], "content": ""})
        if dropped_actions or dropped_readonly:
            write({"type": "calls_dropped", "kept": name, "dropped": dropped_actions + dropped_readonly})
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
            **plan_update,
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
            tier = routing.FAST if agent.routing_on else routing.MAIN
            client = agent.client_for(tier)
            t0 = time.time()
            text, usage = llm.plain_completion_with_usage(client, msgs)
            usage = usage or {}
            write({"type": "llm_call", "model": getattr(client, "model_name", None) or getattr(client, "model", ""),
                   "tier": tier, "reason": "compress earlier steps",
                   "duration_ms": int((time.time() - t0) * 1000),
                   "prompt_tokens": usage.get("prompt"), "completion_tokens": usage.get("completion"),
                   "cached_tokens": usage.get("cached"), "tool_calls": []})
            return text

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
        plan_changed = False
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
                "term": tool_args.get("term") if tool_name == "save_definition" else None,
                **({"table_names": tool_args.get("table_names")} if tool_name == "find_insights" else {}),
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
                plan = planning.keep_subagent_info(planning.normalize_steps(tool_args.get("steps")), plan)
                plan_changed = True
                tool_content = planning.summarize_plan(plan)
                write({"type": "plan", "steps": [dict(x) for x in plan]})
                write({"type": "tool_result", "tool": tool_name, "status": "ok", "stdout": tool_content})
            elif tool_name == "find_insights":
                tool_content, status = agent.find_insights(tool_args, input_tables, write)
                write({"type": "tool_result", "tool": tool_name, "status": status, "stdout": tool_content})
            elif tool_name == "save_definition":
                tool_content, status = agent.save_definition(
                    tool_args, state.get("user_question", ""), input_tables)
                write({"type": "tool_result", "tool": tool_name, "status": status, "stdout": tool_content,
                       "term": tool_args.get("term"), "meaning": tool_args.get("meaning")})
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

        after = "agent"
        if plan_changed and self._can_fan_out(state, plan):
            after = "subagents"
        return {"messages": messages, "loaded_skills": loaded, "pending_tool_calls": [],
                "charts": agent._end_payload(), "plan": plan, "after_tools": after}

    # ── parallel sub-agents ───────────────────────────────────────────

    MAX_SUBAGENT_ROUNDS = 3

    def _can_fan_out(self, state: AnalystState, plan: list[dict[str, Any]]) -> bool:
        """Sub-agents are on, and the plan has ≥2 steps that could start now."""
        if getattr(self.agent, "should_stop", None) is not None:
            return False                                   # a sub-agent never fans out again
        if not self.agent.settings.subagents:
            return False
        if int(state.get("subagent_rounds") or 0) >= self.MAX_SUBAGENT_ROUNDS:
            return False
        return len(planning.startable_steps(plan, state.get("dispatched_steps") or [])) >= 2

    def dispatch_step(self, state: AnalystState) -> dict[str, Any]:
        """Start a fan-out round: mark the steps that can start now as queued and
        write one task per step; the ``Send`` edge then gives each its own
        ``subagent`` branch.

        Each branch's result is saved in the checkpoint as soon as it finishes
        (``durability="async"``), so a run interrupted mid-round continues with
        only the unfinished steps.
        """
        agent = self.agent
        settings = agent.settings
        write = _writer()
        run_id = state.get("run_id", "")
        plan = [dict(s) for s in state.get("plan") or []]
        batch = planning.startable_steps(plan, state.get("dispatched_steps") or [])
        batch_ids = {b["id"] for b in batch}
        round_no = int(state.get("subagent_rounds") or 0) + 1
        queued_ms = int(time.time() * 1000)
        for s in plan:
            if s["id"] in batch_ids:
                s["status"] = "in_progress"
                s["subagent"] = {"status": "queued", "run_id": run_id, "round": round_no, "queued_at": queued_ms,
                                 "max_parallel": settings.max_parallel}
        write({"type": "plan", "steps": [dict(x) for x in plan]})
        messages = state.get("messages") or []
        tasks = [{
            "step_id": b["id"], "title": b["title"], "goal": b.get("goal") or b["title"],
            "user_question": state.get("user_question", ""),
            "user_answers": _user_answers(messages),
            "lead_checks": _lead_checks(messages),
            "input_tables": state.get("input_tables") or [],
            "parent_run_id": run_id,
            "max_actions": settings.subagent_max_charts,
            "round": round_no, "queued_at": queued_ms, "max_parallel": settings.max_parallel,
        } for b in batch]
        return {"plan": plan, "subagent_tasks": tasks, "subagent_results": None}

    def subagent_branch(self, inp: dict[str, Any]) -> dict[str, Any]:
        """One plan step, in its own branch (several run at the same time).

        The step can be stopped on its own from the UI (see
        :mod:`.subagent_control`), stops at its time limit, and stops when the
        whole run ends (the user pressed Stop or went away).
        """
        from alidade.analyst.engine import subagent, subagent_control

        agent = self.agent
        task = inp["task"]
        sid = task["step_id"]
        run_id = task.get("parent_run_id", "")
        identity = getattr(agent, "identity_id", None)
        limit = agent.settings.subagent_timeout_s
        write = _writer()
        handle = subagent_control.StepHandle()
        subagent_control.register(identity, run_id, sid, handle)
        done = threading.Event()
        run_ended = getattr(agent, "run_ended", None)
        if run_ended is not None:
            def watch_run() -> None:
                while not done.wait(0.2):
                    if run_ended.is_set():
                        handle.stop(subagent_control.RUN_ENDED)
                        return
            threading.Thread(target=watch_run, daemon=True, name=f"subagent-watch-{sid}").start()
        slots = _run_slots(run_id, agent.settings.max_parallel)
        holding = False
        while not handle.event.is_set():            # queued behind sibling steps
            if slots.acquire(timeout=0.2):
                holding = True
                break
        timer = threading.Timer(limit, handle.stop, args=(subagent_control.TIMEOUT,))
        timer.daemon = True
        started_ms = int(time.time() * 1000)
        write({"type": "subagent", "step_id": sid, "status": "started", "title": task["title"],
               "goal": task["goal"], "run_id": run_id, "started_at": started_ms, "timeout_s": limit,
               "round": task.get("round"), "queued_at": task.get("queued_at"),
               "max_charts": task["max_actions"]})
        timer.start()
        try:
            result = subagent.run_subtask(agent, task, write, handle.event)
        finally:
            timer.cancel()
            done.set()
            if holding:
                slots.release()
            subagent_control.unregister(identity, run_id, sid)
        if handle.stopped and result["status"] == "stopped":
            result["status"] = {subagent_control.USER: "cancelled",
                                subagent_control.TIMEOUT: "timeout"}.get(handle.reason, "stopped")
        result["started_at"] = started_ms
        result["round"], result["queued_at"] = task.get("round"), task.get("queued_at")
        result["max_parallel"] = task.get("max_parallel")
        # announce the end now — siblings may still be running
        write({"type": "subagent", "step_id": sid, **_subagent_record(result, run_id)})
        return {"subagent_results": [result]}

    def merge_step(self, state: AnalystState) -> dict[str, Any]:
        """All branches of the round are back: record what each sub-agent did on
        its plan step and report the findings to the main agent."""
        write = _writer()
        run_id = state.get("run_id", "")
        with _SLOTS_LOCK:
            _SLOTS.pop(run_id, None)
        tasks = list(state.get("subagent_tasks") or [])
        results = {r["step_id"]: r for r in state.get("subagent_results") or []}
        plan = [dict(s) for s in state.get("plan") or []]
        messages = _copy_messages(state.get("messages") or [])
        charts = list(state.get("charts") or [])
        completed_steps = list(state.get("completed_steps") or [])
        sections = []
        for t in tasks:
            r = results.get(t["step_id"])
            if r is None:                    # never happens in practice; keep the round whole
                r = {"step_id": t["step_id"], "status": "failed", "error": "no result", "findings": "",
                     "charts": [], "chart_titles": [], "completed_steps": [], "duration_s": None}
                write({"type": "subagent", "step_id": t["step_id"], **_subagent_record(r, run_id)})
            titles = [x for x in r.get("chart_titles") or [] if x]
            record = _subagent_record(r, run_id)
            ok = r["status"] in ("success", "budget_exhausted") and r.get("chart_titles")
            for s in plan:
                if s["id"] == t["step_id"]:
                    if r["status"] == "cancelled":
                        s["status"] = "skipped"
                    else:
                        s["status"] = "done" if ok or r["status"] == "success" else "pending"
                    s["subagent"] = record
            charts.extend(r.get("charts") or [])
            completed_steps.extend(r.get("completed_steps") or [])
            shown = "; ".join(titles) or "none"
            body = r.get("findings") or "(no findings)"
            if r["status"] == "cancelled":
                body += ("\n(The user stopped this step. Do not redo it; say in the final answer that "
                         "it was skipped at the user's request.)")
            elif r["status"] not in ("success", "budget_exhausted"):
                body += f"\n(This step did not finish: {r['status']}{' — ' + r['error'] if r.get('error') else ''}.)"
            sections.append(f"### {t['step_id']} — {t['title']} [{r['status']}]\nCharts shown: {shown}\n{body}")
        write({"type": "plan", "steps": [dict(x) for x in plan]})

        dispatched = list(state.get("dispatched_steps") or []) + [t["step_id"] for t in tasks]
        messages.append({"role": "user", "content": (
            "[SUBTASK RESULTS] These plan steps were done in parallel by other analysts; their "
            "charts are already shown to the user — do not draw them again.\n\n"
            + "\n\n".join(sections)
            + "\n\nNow do any remaining steps yourself (redo a step that did not finish only if "
            "the answer needs it), then write the final answer combining all findings. Check "
            "that the steps used the same time range, units and filters; if they did not, say "
            "so instead of comparing the numbers directly.\n\n"
            + planning.summarize_plan(plan)
        )})
        update: dict[str, Any] = {
            "messages": messages, "plan": plan, "charts": charts, "completed_steps": completed_steps,
            "dispatched_steps": dispatched, "subagent_rounds": int(state.get("subagent_rounds") or 0) + 1,
            "tool_rounds": 0, "subagent_tasks": [], "subagent_results": None,
        }
        nxt = {**state, **update}
        update["after_tools"] = "subagents" if self._can_fan_out(nxt, plan) else "agent"
        return update

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
        charts_before = len(completed_steps)
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
        if name == "visualize" and len(completed_steps) > charts_before and agent.settings.plan_auto_advance:
            # a chart was shown: advance the plan here instead of spending an
            # LLM round on an update_plan call
            advanced = planning.advance_plan(state.get("plan") or [], (pending.get("args") or {}).get("plan_step"))
            if advanced is not None:
                write({"type": "plan", "steps": advanced})
                update["plan"] = advanced
                if observation is not None:
                    observation = f"{observation}\n\n[PLAN] {planning.summarize_plan(advanced)}"
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
        closed = (planning.close_plan(state.get("plan") or [])
                  if status == "success" and self.agent.settings.plan_auto_advance else None)
        if closed is not None:
            write({"type": "plan", "steps": closed})
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
        return {"plan": closed} if closed is not None else {}


def _subagent_record(r: dict[str, Any], run_id: str) -> dict[str, Any]:
    """What a plan step keeps (and the UI shows) about the sub-agent that did it."""
    usage = r.get("usage") or {}
    return {
        "status": r["status"], "run_id": run_id, "started_at": r.get("started_at"),
        **({"round": r["round"]} if r.get("round") else {}),
        **({"queued_at": r["queued_at"]} if r.get("queued_at") else {}),
        **({"max_parallel": r["max_parallel"]} if r.get("max_parallel") else {}),
        **{k: int(usage[k]) for k in ("calls", "prompt_tokens", "cached_tokens", "completion_tokens",
                                      "tool_calls") if k in usage},
        "duration_s": r.get("duration_s"), "charts": len(r.get("chart_titles") or []),
        "chart_titles": [x for x in r.get("chart_titles") or [] if x], "findings": r.get("findings") or "",
        **({"error": r["error"]} if r.get("error") else {}),
    }


_SLOTS: dict[str, threading.BoundedSemaphore] = {}
_SLOTS_LOCK = threading.Lock()


def _run_slots(run_id: str, limit: int) -> threading.BoundedSemaphore:
    """How many of a run's sub-agents may work at once (``max_parallel``).

    LangGraph's own ``max_concurrency`` is not used: with a limit of one, its
    stream worker holds the only slot and the branches never start.
    """
    with _SLOTS_LOCK:
        slots = _SLOTS.get(run_id)
        if slots is None:
            if len(_SLOTS) > 256:
                _SLOTS.clear()
            slots = _SLOTS[run_id] = threading.BoundedSemaphore(max(1, limit))
        return slots


def _route_after_tools(state: AnalystState) -> str:
    return "dispatch" if state.get("after_tools") == "subagents" else "agent"


def _send_subtasks(state: AnalystState) -> list[Send]:
    """One ``subagent`` branch per task; LangGraph runs them in parallel (at most
    ``max_parallel`` work at once, see :func:`_run_slots`) and saves each branch's result as it lands."""
    return [Send("subagent", {"task": t}) for t in state.get("subagent_tasks") or []]


def _user_answers(messages: list[dict[str, Any]], limit: int = 3) -> list[str]:
    """What the user said after the opening message (answers to ask_user)."""
    answers = []
    for m in messages[2:]:
        content = m.get("content")
        if m.get("role") != "user" or not isinstance(content, str):
            continue
        if content.startswith("[") or not content.strip():
            continue                                        # system notes, skill bodies, results
        answers.append(content.strip()[:300])
    return answers[-limit:]


LEAD_CHECKS_CHARS = 2400       # what a sub-agent gets of the lead's earlier checks
_LEAD_CHECK_TOOLS = ("inspect_source_data", "execute_python_script", "find_insights")


def _lead_checks(messages: list[dict[str, Any]], budget: int = LEAD_CHECKS_CHARS) -> list[str]:
    """Outputs of the data checks the main agent already ran (newest kept
    first, oldest first in the result), so a sub-agent reuses them instead of
    repeating them, and filters / cleaning stay the same across steps."""
    names: dict[str, str] = {}
    for m in messages:
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            names[str(tc.get("id"))] = str(fn.get("name") or "")
    picked: list[str] = []
    left = budget
    for m in reversed(messages):
        if m.get("role") != "tool" or names.get(str(m.get("tool_call_id"))) not in _LEAD_CHECK_TOOLS:
            continue
        text = m.get("content") if isinstance(m.get("content"), str) else ""
        text = text.strip()
        if not text or text.startswith(("[ERROR]", "Error")):
            continue
        piece = text if len(text) <= min(left, 1200) else text[:min(left, 1200)].rstrip() + " …"
        picked.append(piece)
        left -= len(piece)
        if left < 200:
            break
    return list(reversed(picked))


def _route(state: AnalystState) -> str:
    return state.get("route") or "finish"
