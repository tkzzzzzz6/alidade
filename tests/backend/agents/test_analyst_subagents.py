"""Sub-agents: one plan step each, run in parallel (stages 3–4 of the plan)."""
from __future__ import annotations

import json
import re
import threading
from typing import Any

import pytest

from alidade.analyst.agent import AnalystAgent
from alidade.analyst.engine import subagent

from test_analyst_graph import _chunks, call, cp_path, text, visualize_args, workspace  # noqa: F401

pytestmark = [pytest.mark.backend]

STEP_RE = re.compile(r"\[YOUR STEP\] (\S+):")


class RoutingClient:
    """A fake LLM that answers the main agent and each sub-agent from separate
    scripts (picked by the "[YOUR STEP] <id>:" marker), safely across threads."""

    model = "fake-model"

    def __init__(self, main: list[dict], steps: dict[str, list[dict]] | None = None):
        self.scripts: dict[str, list[dict]] = {"main": list(main), **{k: list(v) for k, v in (steps or {}).items()}}
        self.seen: dict[str, list[list[dict]]] = {}
        self.tools_seen: dict[str, list[list[str]]] = {}
        self.lock = threading.Lock()
        self.fail_steps: set[str] = set()

    @staticmethod
    def who(messages) -> str:
        first_user = next((m for m in messages if m.get("role") == "user"), {})
        m = STEP_RE.search(str(first_user.get("content", "")))
        return m.group(1) if m else "main"

    def get_completion_with_tools(self, messages, tools=None, stream=True, **kwargs):
        key = self.who(messages)
        with self.lock:
            self.seen.setdefault(key, []).append(json.loads(json.dumps(messages, default=str)))
            self.tools_seen.setdefault(key, []).append([t["function"]["name"] for t in tools or []])
            if key in self.fail_steps:
                raise RuntimeError("model unavailable")
            script = self.scripts.get(key) or []
            if not script:
                raise AssertionError(f"LLM called more times than scripted for {key}")
            step = script.pop(0)
        return iter(_chunks(step))

    def get_completion(self, messages, stream=False, **kwargs):
        from types import SimpleNamespace
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=""))])


def parent_agent(client, workspace):
    return AnalystAgent(client=client, workspace=workspace)


def task(step_id="s1", **over):
    return {"step_id": step_id, "title": f"Step {step_id}", "goal": f"Goal of {step_id}",
            "user_question": "Compare things", "user_answers": [], "input_tables": [{"name": "sales"}],
            "parent_run_id": "parent", **over}


def py(code):
    return call("execute_python_script", {"code": code, "purpose": "look"})


# ── stage 3: the sub-agent runner ─────────────────────────────────────────

def test_subtask_agent_offers_only_step_tools(workspace):
    client = RoutingClient([])
    agent = subagent.SubtaskAgent(client, workspace)
    names = {t["function"]["name"] for t in agent._current_tools({"core"})}
    assert names == set(subagent.SUBTASK_TOOLS)
    prompt = agent._build_system_prompt()
    assert "You are working on one step of a larger analysis" in prompt
    assert "at most 2 chart(s)" in prompt
    assert agent.max_iterations == 3                       # 2 charts + a turn for the closing text


def test_run_subtask_returns_findings_and_tagged_events(workspace):
    client = RoutingClient([], {"s1": [call("visualize", visualize_args()), text("North leads with 40.")]})
    events: list[dict] = []
    result = subagent.run_subtask(parent_agent(client, workspace), task("s1"), events.append)
    assert result["status"] == "success" and result["findings"] == "North leads with 40."
    assert result["chart_titles"] == ["Sales by region"] and len(result["completed_steps"]) == 1
    assert events and all(e["step_id"] == "s1" for e in events)
    assert not any(e["type"] in ("completion", "interact") for e in events)
    first_user = next(m for m in client.seen["s1"][0] if m["role"] == "user")
    assert "[YOUR STEP] s1: Step s1" in first_user["content"] and "Goal of s1" in first_user["content"]


def test_budget_exhausted_falls_back_to_chart_titles(workspace):
    client = RoutingClient([], {"s1": [call("visualize", visualize_args(title=f"Chart {i}")) for i in range(3)]})
    result = subagent.run_subtask(parent_agent(client, workspace), task("s1"), lambda e: None)
    assert result["status"] == "budget_exhausted"
    assert result["findings"].startswith("Charts shown") and "Chart 0" in result["findings"]


def test_stop_ends_the_step_before_calling_the_model(workspace):
    client = RoutingClient([], {"s1": [text("never")]})
    stop = threading.Event()
    stop.set()
    result = subagent.run_subtask(parent_agent(client, workspace), task("s1"), lambda e: None, stop)
    assert result["status"] == "stopped" and "s1" not in client.seen


def test_model_failure_is_contained(workspace):
    client = RoutingClient([], {"s1": []})
    client.fail_steps.add("s1")
    result = subagent.run_subtask(parent_agent(client, workspace), task("s1"), lambda e: None)
    assert result["status"] == "failed" and result.get("error")


def test_hallucinated_ask_user_does_not_pause(workspace):
    client = RoutingClient([], {"s1": [
        call("ask_user", {"questions": [{"text": "Which?", "options": ["a", "b"]}]}),
        text("Assumed a."),
    ]})
    result = subagent.run_subtask(parent_agent(client, workspace), task("s1"), lambda e: None)
    assert result["status"] == "success" and result["findings"] == "Assumed a."


def test_parallel_subtasks_have_separate_python_sessions(workspace):
    client = RoutingClient([], {
        "s1": [py("secret = 41\nprint('s1 set', secret)"), text("s1 done")],
        "s2": [py("print('s2 sees secret:', 'secret' in globals())"), text("s2 done")],
    })
    parent = parent_agent(client, workspace)
    results: dict[str, Any] = {}
    threads = [threading.Thread(target=lambda s=s: results.__setitem__(s, subagent.run_subtask(
        parent, task(s), lambda e: None))) for s in ("s1", "s2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert results["s1"]["status"] == results["s2"]["status"] == "success"
    s2_tool_output = [m["content"] for m in client.seen["s2"][1] if m.get("role") == "tool"]
    assert any("s2 sees secret: False" in o for o in s2_tool_output)


# ── stage 4: dispatch from the main graph ─────────────────────────────────

def plan_step(id_, deps=(), status="pending"):
    return {"id": id_, "title": f"Step {id_}", "status": status, "depends_on": list(deps), "goal": f"Goal of {id_}"}


def run_main(client, workspace, cp_path, **kw):
    agent = AnalystAgent(client=client, workspace=workspace, max_iterations=kw.pop("max_iterations", 5))
    return list(agent.run(input_tables=[{"name": "sales"}], user_question="Compare two things",
                          checkpoint_db_path=cp_path, **kw))


def sub_events(events, status=None):
    return [e for e in events if e["type"] == "subagent" and (status is None or e["status"] == status)]


@pytest.fixture
def subagents_on(monkeypatch):
    monkeypatch.setenv("ALIDADE_SUBAGENTS", "1")
    monkeypatch.delenv("ALIDADE_MAX_SUBAGENTS", raising=False)
    monkeypatch.delenv("ALIDADE_SUBAGENT_TIMEOUT", raising=False)


def fan_out_plan():
    return call("update_plan", {"steps": [plan_step("s1", status="in_progress"), plan_step("s2"),
                                          plan_step("s3", ["s1", "s2"])]})


def test_independent_steps_run_as_subagents(workspace, cp_path, subagents_on):  # noqa: F811
    client = RoutingClient(
        main=[fan_out_plan(), call("visualize", visualize_args(title="Relation")), text("Both compared.")],
        steps={"s1": [call("visualize", visualize_args(title="A")), text("A is 40.")],
               "s2": [call("visualize", visualize_args(title="B")), text("B is 30.")]},
    )
    events = run_main(client, workspace, cp_path)
    assert {e["step_id"] for e in sub_events(events, "started")} == {"s1", "s2"}
    assert {e["step_id"]: e["status"] for e in sub_events(events) if e["status"] != "started"} == {
        "s1": "success", "s2": "success"}
    tagged = [e for e in events if e["type"] == "result" and e.get("step_id")]
    assert sorted(e["step_id"] for e in tagged) == ["s1", "s2"]
    plans = [[(s["id"], s["status"]) for s in e["steps"]] for e in events if e["type"] == "plan"]
    assert ("s1", "in_progress") in plans[1] and ("s2", "in_progress") in plans[1]
    # after the sub-agents (plan updates while they start are in between)
    assert [("s1", "done"), ("s2", "done"), ("s3", "pending")] in plans
    assert plans[-1] == [("s1", "done"), ("s2", "done"), ("s3", "done")]
    report = next(m["content"] for m in client.seen["main"][1] if "[SUBTASK RESULTS]" in str(m.get("content")))
    assert "A is 40." in report and "B is 30." in report and "Charts shown: A" in report
    assert events[-1]["type"] == "completion" and events[-1]["content"]["total_steps"] == 3
    assert len(client.seen["main"]) == 3                  # plan, own chart, answer


def test_subagents_off_keeps_one_agent(workspace, cp_path, monkeypatch):  # noqa: F811
    monkeypatch.delenv("ALIDADE_SUBAGENTS", raising=False)
    client = RoutingClient(main=[fan_out_plan(), call("visualize", visualize_args()), text("Done.")])
    events = run_main(client, workspace, cp_path)
    assert sub_events(events) == [] and set(client.seen) == {"main"}


def test_linear_plan_is_not_split(workspace, cp_path, subagents_on):  # noqa: F811
    client = RoutingClient(main=[
        call("update_plan", {"steps": [plan_step("s1", status="in_progress"), plan_step("s2", ["s1"]),
                                       plan_step("s3", ["s2"])]}),
        text("Done."),
    ])
    events = run_main(client, workspace, cp_path)
    assert sub_events(events) == []


def test_a_failed_step_does_not_fail_the_run(workspace, cp_path, subagents_on):  # noqa: F811
    client = RoutingClient(
        main=[fan_out_plan(), text("Only A worked.")],
        steps={"s1": [call("visualize", visualize_args(title="A")), text("A is 40.")], "s2": []},
    )
    client.fail_steps.add("s2")
    events = run_main(client, workspace, cp_path)
    finals = {e["step_id"]: e["status"] for e in sub_events(events) if e["status"] != "started"}
    assert finals == {"s1": "success", "s2": "failed"}
    plan = next(e for e in reversed(events) if e["type"] == "plan")["steps"]
    assert [s["status"] for s in plan][:2] == ["done", "pending"]
    report = next(m["content"] for m in client.seen["main"][1] if "[SUBTASK RESULTS]" in str(m.get("content")))
    assert "did not finish: failed" in report
    assert events[-1]["type"] == "completion" and events[-1]["status"] == "success"


def test_slow_step_times_out(workspace, cp_path, subagents_on, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ALIDADE_SUBAGENT_TIMEOUT", "1")
    client = RoutingClient(
        main=[fan_out_plan(), text("Partial.")],
        steps={"s1": [py("import time\ntime.sleep(2)\nprint('slow')"), text("never")],
               "s2": [text("B is 30.")]},
    )
    events = run_main(client, workspace, cp_path)
    finals = {e["step_id"]: e["status"] for e in sub_events(events) if e["status"] != "started"}
    assert finals == {"s1": "timeout", "s2": "success"}


def test_concurrency_limit_of_one_still_finishes(workspace, cp_path, subagents_on, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ALIDADE_MAX_SUBAGENTS", "1")
    client = RoutingClient(
        main=[fan_out_plan(), text("Done.")],
        steps={"s1": [text("A.")], "s2": [text("B.")]},
    )
    events = run_main(client, workspace, cp_path)
    assert len(sub_events(events, "success")) == 2


def test_second_round_once_dependencies_are_done(workspace, cp_path, subagents_on):  # noqa: F811
    client = RoutingClient(
        main=[call("update_plan", {"steps": [plan_step("s1"), plan_step("s2"),
                                             plan_step("s3", ["s1"]), plan_step("s4", ["s2"])]}),
              text("All four.")],
        steps={k: [text(f"{k} found.")] for k in ("s1", "s2", "s3", "s4")},
    )
    events = run_main(client, workspace, cp_path)
    order = [e["step_id"] for e in sub_events(events, "success")]
    assert sorted(order[:2]) == ["s1", "s2"] and sorted(order[2:]) == ["s3", "s4"]
    reports = [m for m in client.seen["main"][1] if "[SUBTASK RESULTS]" in str(m.get("content"))]
    assert len(reports) == 2


def test_sub_agents_get_the_users_answers(workspace, cp_path, subagents_on):  # noqa: F811
    from alidade.analyst.engine.graph import _user_answers
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "tables + question"},
            {"role": "user", "content": "[SYSTEM] note"}, {"role": "user", "content": "Use 2025 only"}]
    assert _user_answers(msgs) == ["Use 2025 only"]


def test_stopping_the_run_stops_running_subagents(workspace, cp_path, subagents_on):  # noqa: F811
    """The user's Stop closes the event stream; sub-agents must not keep calling the model."""
    import time
    client = RoutingClient(
        main=[fan_out_plan(), text("unused")],
        steps={s: [py("import time\ntime.sleep(1)\nprint('x')"), py("print('more')"), text("never")]
               for s in ("s1", "s2")},
    )
    agent = AnalystAgent(client=client, workspace=workspace, max_iterations=5)
    stream = agent.run(input_tables=[{"name": "sales"}], user_question="Compare two things",
                       checkpoint_db_path=cp_path)
    for event in stream:
        if event["type"] == "subagent" and event["status"] == "started":
            break
    stream.close()                                     # what a client disconnect does
    assert agent.run_ended.is_set()
    time.sleep(2.5)                                    # let the first tool call finish
    with client.lock:
        calls = {k: len(v) for k, v in client.seen.items() if k != "main"}
    # stopped at (or before) their next model call: none went past its first
    assert all(n <= 1 for n in calls.values()), calls
