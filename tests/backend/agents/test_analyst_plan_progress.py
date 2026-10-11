"""The plan advances by itself as charts are shown (no update_plan round trips)."""
from __future__ import annotations

import json

import pytest

from alidade.analyst.engine import planning

from test_analyst_graph import (  # noqa: F401 — fixtures
    FakeClient,
    call,
    cp_path,
    run_agent,
    text,
    types,
    visualize_args,
    workspace,
)

pytestmark = [pytest.mark.backend]


def step(id_, status="pending", deps=()):
    return {"id": id_, "title": f"Step {id_}", "status": status, "depends_on": list(deps)}


def statuses(plan):
    return [s["status"] for s in plan]


# ── advance_plan / close_plan ──────────────────────────────────────────────

def test_chart_finishes_the_step_in_progress_and_starts_the_next():
    plan = [step("s1", "in_progress"), step("s2"), step("s3")]
    assert statuses(planning.advance_plan(plan)) == ["done", "in_progress", "pending"]
    assert statuses(plan) == ["in_progress", "pending", "pending"]      # input untouched


def test_plan_step_names_the_step_a_chart_belongs_to():
    plan = [step("s1", "in_progress"), step("s2"), step("s3")]
    out = planning.advance_plan(plan, "s3")
    assert statuses(out) == ["in_progress", "pending", "done"]          # s1 still being worked on


def test_without_a_step_in_progress_the_first_ready_step_is_used():
    plan = [step("s1"), step("s2")]
    assert statuses(planning.advance_plan(plan)) == ["done", "in_progress"]


def test_next_step_respects_dependencies():
    plan = [step("s1", "in_progress"), step("s3", deps=["s2"]), step("s2")]
    assert statuses(planning.advance_plan(plan)) == ["done", "pending", "in_progress"]


@pytest.mark.parametrize("plan,step_id", [
    ([], None),
    ([step("s1", "done")], None),
    ([step("s1", "done"), step("s2", "in_progress")], "s1"),            # already done
])
def test_nothing_to_advance(plan, step_id):
    assert planning.advance_plan(plan, step_id) is None


def test_close_plan():
    plan = [step("s1", "done"), step("s2", "in_progress"), step("s3")]
    assert statuses(planning.close_plan(plan)) == ["done", "done", "pending"]
    assert planning.close_plan([step("s1", "done")]) is None
    assert planning.close_plan([]) is None


# ── in the running graph ──────────────────────────────────────────────────

def plan_events(events):
    return [statuses(e["steps"]) for e in events if e["type"] == "plan"]


def three_steps():
    return {"steps": [step("s1", "in_progress"), step("s2"), step("s3")]}


def test_plan_advances_without_update_plan_calls(workspace, cp_path):
    client = FakeClient([
        call("update_plan", three_steps()),
        call("visualize", visualize_args()),
        call("visualize", visualize_args(title="Second")),
        text("Done."),
    ])
    events = run_agent(client, workspace, cp_path)
    assert plan_events(events) == [
        ["in_progress", "pending", "pending"],
        ["done", "in_progress", "pending"],          # after chart 1
        ["done", "done", "in_progress"],             # after chart 2
        ["done", "done", "done"],                    # closing answer
    ]
    assert len(client.seen) == 4                     # plan, chart, chart, answer — no status rounds
    observations = [m["content"] for m in client.seen[2] if m.get("role") == "tool"]
    assert any("[PLAN]" in str(o) for o in observations)


def test_failed_chart_does_not_advance(workspace, cp_path):
    client = FakeClient([
        call("update_plan", three_steps()),
        call("visualize", visualize_args(code="raise ValueError('boom')")),
        text("Could not chart it."),
    ])
    events = run_agent(client, workspace, cp_path)
    assert plan_events(events)[1:] == [["done", "pending", "pending"]]   # only the closing answer


def test_plan_change_rides_along_with_an_action(workspace, cp_path):
    client = FakeClient([
        {"content": "", "tool_calls": [("update_plan", three_steps()), ("visualize", visualize_args())]},
        text("Done."),
    ])
    events = run_agent(client, workspace, cp_path)
    assert plan_events(events)[:2] == [["in_progress", "pending", "pending"], ["done", "in_progress", "pending"]]
    assert any(e["type"] == "result" for e in events)
    second = client.seen[1]
    assert not any("commits exactly one action" in str(m.get("content")) for m in second)
    tool_ids = {m["tool_call_id"] for m in second if m.get("role") == "tool"}
    assert len(tool_ids) == 2                         # both calls answered


def test_other_inspection_next_to_an_action_is_still_dropped(workspace, cp_path):
    client = FakeClient([
        {"content": "", "tool_calls": [("update_plan", three_steps()),
                                       ("execute_python_script", {"code": "print(1)", "purpose": "x"}),
                                       ("visualize", visualize_args())]},
        text("Done."),
    ])
    events = run_agent(client, workspace, cp_path)
    notes = [str(m.get("content")) for m in client.seen[1] if m.get("role") == "user"]
    note = next(n for n in notes if "commits exactly one action" in n)
    assert "execute_python_script" in note and "update_plan" not in note
    assert plan_events(events)[0] == ["in_progress", "pending", "pending"]


def test_unfinished_run_keeps_the_plan_as_is(workspace, cp_path):
    client = FakeClient([
        call("update_plan", three_steps()),
        call("visualize", visualize_args()),
    ])
    events = run_agent(client, workspace, cp_path, max_iterations=1)
    assert events[-1]["type"] == "completion" and events[-1]["status"] == "max_iterations"
    assert plan_events(events)[-1] == ["done", "in_progress", "pending"]   # not closed


# ── prompt ────────────────────────────────────────────────────────────────

def test_prompt_plans_from_three_steps_and_says_statuses_advance(monkeypatch):
    monkeypatch.delenv("ALIDADE_SUBAGENTS", raising=False)
    text_ = planning.planning_prompt()
    assert "three or more distinct analyses" in text_
    assert "You do not need to update step statuses" in text_
    assert "plan_step" in text_
    tool = json.dumps(planning.plan_tool())
    assert "advance by themselves" in tool
    monkeypatch.setenv("ALIDADE_SUBAGENTS", "1")
    on = planning.planning_prompt()
    assert "**two or more distinct analyses that do not need each other's results**" in on
    # no leftover rule telling the model to skip plans for two charts (it overrode the flag)
    assert "one or two charts" not in on and "three or more distinct analyses or charts" not in on
    assert "two or more analyses that do not need" in json.dumps(planning.plan_tool())
    assert "{" not in on.replace("{max", "")


def test_dropped_calls_are_reported(workspace, cp_path):
    client = FakeClient([
        {"content": "", "tool_calls": [("execute_python_script", {"code": "print(1)", "purpose": "x"}),
                                       ("visualize", visualize_args())]},
        text("Done."),
    ])
    events = run_agent(client, workspace, cp_path)
    dropped = [e for e in events if e["type"] == "calls_dropped"]
    assert dropped == [{"type": "calls_dropped", "kept": "visualize", "dropped": ["execute_python_script"]}]


def test_planning_examples_do_not_reuse_evaluation_questions(monkeypatch):
    """Prompt examples must not mirror the eval set, or the eval measures memorised hints."""
    import yaml
    from pathlib import Path
    questions = yaml.safe_load((Path(__file__).resolve().parents[3] / "eval" / "questions.yaml").read_text(encoding="utf-8"))
    monkeypatch.setenv("ALIDADE_SUBAGENTS", "1")
    text_ = planning.planning_prompt().lower() + json.dumps(planning.plan_tool()).lower()
    for word in ("renewable", "co₂", "co2", "stock", "volume", "gasoline", "diesel", "unemployment", "box office"):
        assert word not in text_, word
    assert len(questions["questions"]) > 40


def test_core_skill_answers_partly_missing_data_without_asking():
    from alidade.analyst.skills import build_registry
    body = build_registry().load_body("core")
    assert "*Partly outside the data*" in body and "do not stop to ask" in body
    assert body.index("*Partly outside the data*") < body.index("*Missing data*")
    assert "never as the recommended one" in body


def test_auto_advance_can_be_switched_off_for_ab_tests(workspace, cp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ALIDADE_PLAN_AUTO_ADVANCE", "0")
    prompt = planning.planning_prompt()
    assert "keep it current: mark a step `in_progress`" in prompt
    assert "You do not need to update step statuses" not in prompt and "{status_rule}" not in prompt
    client = FakeClient([
        call("update_plan", three_steps()),
        call("visualize", visualize_args()),
        text("Done."),
    ])
    events = run_agent(client, workspace, cp_path)
    assert plan_events(events) == [["in_progress", "pending", "pending"]]   # untouched by code
