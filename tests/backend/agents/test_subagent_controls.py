"""Per-run agent settings (from the UI) and stopping one sub-agent."""
from __future__ import annotations

import threading
import time
from unittest.mock import patch

import flask
import pytest

from alidade.analyst.agent import AnalystAgent
from alidade.analyst.engine import planning, subagent_control
from alidade.analyst.engine.run_settings import RunSettings, describe

from test_analyst_graph import call, cp_path, text, visualize_args, workspace  # noqa: F401
from test_analyst_subagents import RoutingClient, fan_out_plan, plan_step, py, sub_events

pytestmark = [pytest.mark.backend]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("ALIDADE_SUBAGENTS", "ALIDADE_MAX_SUBAGENTS", "ALIDADE_SUBAGENT_TIMEOUT",
                 "ALIDADE_SUBAGENT_MAX_CHARTS", "ALIDADE_PLAN_AUTO_ADVANCE"):
        monkeypatch.delenv(name, raising=False)


def run_with(client, workspace, cp_path, settings=None, **kw):  # noqa: F811
    agent = AnalystAgent(client=client, workspace=workspace, max_iterations=kw.pop("max_iterations", 5),
                         settings=settings, identity_id=kw.pop("identity_id", None))
    return agent, list(agent.run(input_tables=[{"name": "sales"}], user_question=kw.pop("question", "Compare"),
                                 checkpoint_db_path=cp_path, **kw))


# ── settings ──────────────────────────────────────────────────────────────


def test_env_defaults_and_overrides(monkeypatch):
    assert RunSettings.from_env() == RunSettings()
    monkeypatch.setenv("ALIDADE_SUBAGENTS", "1")
    monkeypatch.setenv("ALIDADE_MAX_SUBAGENTS", "2")
    monkeypatch.setenv("ALIDADE_SUBAGENT_TIMEOUT", "5")       # operator value: only a 1 s floor
    base = RunSettings.from_env()
    assert (base.subagents, base.max_parallel, base.subagent_timeout_s) == (True, 2, 5.0)
    s = base.merged({"subagents": False, "max_parallel": 99, "subagent_max_charts": "0",
                     "subagent_timeout_s": 5, "plan_auto_advance": "off", "unknown": 1})
    assert s == RunSettings(subagents=False, max_parallel=4, subagent_max_charts=1,
                            subagent_timeout_s=30.0, plan_auto_advance=False)
    assert base.merged({"subagents": "maybe", "max_parallel": "x"}) == base   # bad values ignored
    assert base.merged(None) == base
    assert describe()["limits"]["max_parallel"] == {"min": 1, "max": 4}


def test_request_setting_turns_subagents_on_without_env(workspace, cp_path):  # noqa: F811
    client = RoutingClient(
        main=[fan_out_plan(), text("A is 40, B is 30.")],
        steps={"s1": [text("A is 40.")], "s2": [text("B is 30.")]},
    )
    _, events = run_with(client, workspace, cp_path, RunSettings(subagents=True))
    assert {e["step_id"] for e in sub_events(events, "started")} == {"s1", "s2"}
    # the plan tool asks for dependencies when sub-agents are on
    assert "depends_on" in str(client.seen["main"][0])


def test_request_setting_turns_subagents_off_despite_env(workspace, cp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ALIDADE_SUBAGENTS", "1")
    client = RoutingClient(main=[fan_out_plan(), text("Did both myself.")])
    _, events = run_with(client, workspace, cp_path, RunSettings(subagents=False))
    assert sub_events(events) == []


def test_charts_per_subagent_and_timeout_come_from_settings(workspace, cp_path):  # noqa: F811
    client = RoutingClient(
        main=[fan_out_plan(), text("Done.")],
        steps={"s1": [call("visualize", visualize_args(title="A")), text("A.")], "s2": [text("B.")]},
    )
    _, events = run_with(client, workspace, cp_path,
                         RunSettings(subagents=True, subagent_max_charts=1, subagent_timeout_s=45))
    started = sub_events(events, "started")[0]
    assert started["max_charts"] == 1 and started["timeout_s"] == 45
    s1_prompt = client.seen["s1"][0][0]["content"]
    assert "at most 1 chart(s)" in s1_prompt


def test_settings_survive_a_pause(workspace, cp_path):  # noqa: F811
    ask = call("ask_user", {"questions": [{"text": "Which metric?", "options": ["Sales", "Units"]}]})
    first = RoutingClient(main=[ask])
    _, events = run_with(first, workspace, cp_path, RunSettings(subagents=True, max_parallel=2))
    run_id = events[-1]["run_id"]
    second = RoutingClient(main=[fan_out_plan(), text("Done.")],
                           steps={"s1": [text("A.")], "s2": [text("B.")]})
    # the resuming request did not send settings: the run keeps its own
    agent, events = run_with(second, workspace, cp_path, None, question="Sales", resume_run_id=run_id)
    assert agent.settings.subagents is True and agent.settings.max_parallel == 2
    assert len(sub_events(events, "started")) == 2


# ── what each sub-agent did is kept on its plan step ──────────────────────


def test_plan_steps_record_subagent_findings(workspace, cp_path):  # noqa: F811
    client = RoutingClient(
        main=[fan_out_plan(), text("A is 40, B is 30.")],
        steps={"s1": [call("visualize", visualize_args(title="A")), text("A is 40.")],
               "s2": [text("B is 30.")]},
    )
    _, events = run_with(client, workspace, cp_path, RunSettings(subagents=True))
    plans = [e["steps"] for e in events if e["type"] == "plan"]
    queued = next(p for p in plans if any(s.get("subagent") for s in p))
    assert queued[0]["subagent"]["status"] == "queued" and queued[0]["subagent"]["run_id"]
    assert queued[0]["subagent"]["round"] == 1 and queued[0]["subagent"]["queued_at"] > 0
    assert queued[0]["subagent"]["max_parallel"] == RunSettings(subagents=True).max_parallel
    # each start is announced with what the checklist needs (clock, limit, run)
    started = sub_events(events, "started")
    assert {e["step_id"] for e in started} == {"s1", "s2"}
    assert all(e["started_at"] > 0 and e["timeout_s"] > 0 and e["run_id"] for e in started)
    final = {s["id"]: s for s in plans[-1]}
    rec = final["s1"]["subagent"]
    assert rec["status"] == "success" and rec["findings"] == "A is 40." and rec["chart_titles"] == ["A"]
    assert rec["duration_s"] is not None and rec["started_at"] > 0
    # what the dispatch panel shows per step: its round, when it was queued, its own model calls
    assert rec["round"] == 1 and rec["queued_at"] <= rec["started_at"]
    assert rec["calls"] == 2 and final["s2"]["subagent"]["calls"] == 1
    assert {"prompt_tokens", "completion_tokens", "cached_tokens", "tool_calls"} <= set(rec)
    ended = [e for e in sub_events(events) if e["status"] != "started"]
    assert {e["step_id"]: e["findings"] for e in ended} == {"s1": "A is 40.", "s2": "B is 30."}


def test_model_rewriting_the_plan_keeps_subagent_records():
    old = [{**plan_step("s1"), "subagent": {"status": "success", "findings": "A"}}, plan_step("s2")]
    new = planning.normalize_steps([plan_step("s1", status="done"), plan_step("s2"), plan_step("s3")])
    kept = planning.keep_subagent_info(new, old)
    assert kept[0]["subagent"]["findings"] == "A" and "subagent" not in kept[1]
    assert planning.keep_subagent_info(new, None) == new


# ── stopping one sub-agent ────────────────────────────────────────────────


def _stop_when_running(step_id: str, identity: str | None, stopped: list[bool]) -> threading.Thread:
    def worker():
        deadline = time.time() + 20
        while time.time() < deadline:
            for (ident, run_id), steps in list(subagent_control._live.items()):
                if ident == (identity or "") and step_id in steps:
                    stopped.append(subagent_control.stop(identity, run_id, step_id))
                    return
            time.sleep(0.02)
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t


def test_user_can_stop_one_subagent(workspace, cp_path):  # noqa: F811
    client = RoutingClient(
        main=[fan_out_plan(), text("Only B: 30. A was skipped.")],
        steps={"s1": [py("import time\ntime.sleep(1.0)\nprint('slow')"), text("never")],
               "s2": [text("B is 30.")]},
    )
    stopped: list[bool] = []
    watcher = _stop_when_running("s1", "user-1", stopped)
    _, events = run_with(client, workspace, cp_path, RunSettings(subagents=True), identity_id="user-1")
    watcher.join(5)
    assert stopped == [True]
    finals = {e["step_id"]: e["status"] for e in sub_events(events) if e["status"] != "started"}
    assert finals == {"s1": "cancelled", "s2": "success"}
    plan = {s["id"]: s for s in next(e for e in reversed(events) if e["type"] == "plan")["steps"]}
    assert plan["s1"]["status"] == "skipped" and plan["s1"]["subagent"]["status"] == "cancelled"
    report = next(m["content"] for m in client.seen["main"][1] if "[SUBTASK RESULTS]" in str(m.get("content")))
    assert "The user stopped this step. Do not redo it" in report
    assert len(client.seen.get("s1", [])) <= 1    # stopped at (or before) its next model turn
    assert subagent_control._live == {}         # nothing left registered
    assert events[-1]["type"] == "completion"


def test_stop_is_scoped_to_the_identity_and_running_steps():
    handle = subagent_control.StepHandle()
    subagent_control.register("alice", "run1", "s1", handle)
    try:
        assert subagent_control.stop("bob", "run1", "s1") is False      # someone else's run
        assert subagent_control.running("alice", "run1") == ["s1"]
        assert subagent_control.stop("alice", "run1", "s1") is True
        assert handle.reason == subagent_control.USER
        assert subagent_control.stop("alice", "run1", "s1") is False    # already stopped
        handle.stop(subagent_control.TIMEOUT)
        assert handle.reason == subagent_control.USER                   # first reason wins
    finally:
        subagent_control.unregister("alice", "run1", "s1")
    assert subagent_control.stop("alice", "run1", "s1") is False


# ── routes ────────────────────────────────────────────────────────────────


@pytest.fixture()
def agents_client():
    from alidade.error_handler import register_error_handlers
    from alidade.routes.agents import agent_bp

    app = flask.Flask(__name__)
    app.config["TESTING"] = True
    app.config["CLI_ARGS"] = {}
    app.register_blueprint(agent_bp)
    register_error_handlers(app)
    return app.test_client()


def test_settings_route(agents_client, monkeypatch):
    monkeypatch.setenv("ALIDADE_SUBAGENTS", "1")
    body = agents_client.get("/api/agent/run-settings").get_json()
    assert body["status"] == "success"
    assert body["data"]["defaults"]["subagents"] is True
    assert body["data"]["limits"]["subagent_timeout_s"] == {"min": 30, "max": 600}


def test_stop_route(agents_client):
    handle = subagent_control.StepHandle()
    subagent_control.register("user-1", "run9", "s2", handle)
    try:
        with patch("alidade.routes.agents.get_identity_id", return_value="user-1"):
            body = agents_client.post("/api/agent/subagent-stop", json={"run_id": "run9", "step_id": "s2"}).get_json()
            assert body["data"] == {"stopped": True, "running": []}
            again = agents_client.post("/api/agent/subagent-stop", json={"run_id": "run9", "step_id": "s2"}).get_json()
            assert again["data"]["stopped"] is False
            bad = agents_client.post("/api/agent/subagent-stop", json={"run_id": "run9"}).get_json()
            assert bad["status"] == "error"
    finally:
        subagent_control.unregister("user-1", "run9", "s2")


def test_streaming_route_passes_agent_settings(agents_client):
    from unittest.mock import MagicMock

    agent_instance = MagicMock()
    agent_instance.run.return_value = [{"type": "completion", "content": {"summary": "done"}}]
    with (
        patch("alidade.routes.agents.get_identity_id", return_value="user-1"),
        patch("alidade.routes.agents.get_client", return_value=object()),
        patch("alidade.routes.agents.get_workspace", return_value=object()),
        patch("alidade.routes.agents.AnalystAgent", return_value=agent_instance) as cls,
    ):
        resp = agents_client.post("/api/agent/analyst-streaming", json={
            "model": {}, "input_tables": [], "user_question": "q",
            "agent_settings": {"subagents": True, "max_parallel": 2},
        })
        resp.get_data()
    settings = cls.call_args.kwargs["settings"]
    assert settings.subagents is True and settings.max_parallel == 2
