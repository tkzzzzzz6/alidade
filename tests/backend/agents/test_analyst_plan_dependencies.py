"""Plan steps with dependencies (stage 2 of the parallel sub-agent plan)."""
from __future__ import annotations

import json

import pytest

from alidade.analyst.engine import planning

from test_analyst_graph import FakeClient, call, cp_path, run_agent, text, workspace  # noqa: F401

pytestmark = [pytest.mark.backend]


def step(id_, deps=(), status="pending", **extra):
    return {"id": id_, "title": f"Step {id_}", "status": status, "depends_on": list(deps), **extra}


def norm(*items):
    return planning.normalize_steps(list(items))


def test_independent_steps_are_ready_together():
    plan = norm(step("s1"), step("s2"), step("s3", ["s1", "s2"]))
    assert [s["id"] for s in planning.ready_steps(plan)] == ["s1", "s2"]


def test_dependent_step_waits_until_dependencies_finish():
    plan = norm(step("s1", status="done"), step("s2", status="in_progress"), step("s3", ["s1", "s2"]))
    assert planning.ready_steps(plan) == []
    plan = norm(step("s1", status="done"), step("s2", status="skipped"), step("s3", ["s1", "s2"]))
    assert [s["id"] for s in planning.ready_steps(plan)] == ["s3"]


def test_linear_chain_runs_one_at_a_time():
    plan = norm(step("a"), step("b", ["a"]), step("c", ["b"]))
    assert [s["id"] for s in planning.ready_steps(plan)] == ["a"]


def test_dependencies_by_position_and_step_names():
    plan = planning.normalize_steps([
        {"title": "one", "status": "pending"},
        {"title": "two", "status": "pending", "depends_on": ["1"]},
        {"title": "three", "status": "pending", "depends_on": "step-2"},
    ])
    assert [s["id"] for s in plan] == ["step-1", "step-2", "step-3"]
    assert plan[1]["depends_on"] == ["step-1"] and plan[2]["depends_on"] == ["step-2"]


def test_unknown_and_self_references_are_dropped():
    plan = norm(step("s1", ["nope", "s1"]), step("s2", ["s1", "s1", 7]))
    assert plan[0]["depends_on"] == [] and plan[1]["depends_on"] == ["s1"]


def test_cycle_falls_back_to_running_in_order():
    plan = norm(step("s1", ["s3"]), step("s2", ["s1"]), step("s3", ["s2"]))
    assert [s["depends_on"] for s in plan] == [[], ["s1"], ["s2"]]
    assert [s["id"] for s in planning.ready_steps(plan)] == ["s1"]


def test_bad_or_duplicate_ids_get_positional_ids():
    plan = norm(step("has space"), step("s2"), step("s2"))
    assert [s["id"] for s in plan] == ["step-1", "s2", "step-3"]


def test_goal_is_kept_and_trimmed():
    plan = norm(step("s1", goal="  compute the share  "), step("s2", goal="x" * 2000))
    assert plan[0]["goal"] == "compute the share"
    assert len(plan[1]["goal"]) == planning.MAX_GOAL_CHARS
    assert "goal" not in norm(step("s3"))[0]


def test_old_style_plans_still_work():
    plan = planning.normalize_steps([{"title": "A", "status": "done"}, "B"])
    assert [(s["id"], s["status"], s["depends_on"]) for s in plan] == [
        ("step-1", "done", []), ("step-2", "pending", [])]
    assert planning.summarize_plan(plan) == "Plan updated (2 steps): [x] A; [ ] B"


def test_summary_shows_dependencies_when_present():
    plan = norm(step("s1", status="done"), step("s2"), step("s3", ["s1", "s2"]))
    assert planning.summarize_plan(plan) == (
        "Plan updated (3 steps): [x] s1 Step s1; [ ] s2 Step s2; [ ] s3 Step s3 (after s1, s2)")


def test_schema_and_prompt_follow_the_flag(monkeypatch):
    monkeypatch.delenv("ALIDADE_SUBAGENTS", raising=False)
    assert planning.subagents_enabled() is False
    off = planning.plan_tool()["function"]["parameters"]["properties"]["steps"]["items"]
    assert "depends_on" not in off["properties"]
    assert "depends_on" not in planning.planning_prompt()

    monkeypatch.setenv("ALIDADE_SUBAGENTS", "1")
    on = planning.plan_tool()["function"]["parameters"]["properties"]["steps"]["items"]
    assert {"id", "depends_on", "goal"} <= set(on["properties"])
    assert "depends_on" in on["required"]
    assert "Mark which steps depend on which" in planning.planning_prompt()
    # the default schema object is never mutated
    assert "depends_on" not in planning.PLAN_TOOL["function"]["parameters"]["properties"]["steps"]["items"]["properties"]


def test_agent_streams_plan_with_dependencies(workspace, cp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ALIDADE_SUBAGENTS", "1")
    client = FakeClient([
        call("update_plan", {"steps": [step("s1", goal="share"), step("s2"), step("s3", ["s1", "s2"])]}),
        text("Done."),
    ])
    events = run_agent(client, workspace, cp_path)
    plan = next(e for e in events if e["type"] == "plan")["steps"]
    assert [s["depends_on"] for s in plan] == [[], [], ["s1", "s2"]]
    assert plan[0]["goal"] == "share"
    sent_tools = json.dumps(client.seen[0])          # the model was told about dependencies
    assert "Mark which steps depend on which" in sent_tools
