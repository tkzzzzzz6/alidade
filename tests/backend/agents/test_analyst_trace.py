"""Run traces: every run leaves a compact, per-user record for the trace viewer."""
from __future__ import annotations

import json
import os

import pytest

from alidade.analyst.engine import trace

from test_analyst_graph import FakeClient, call, cp_path, run_agent, text, visualize_args, workspace  # noqa: F401

pytestmark = [pytest.mark.backend]


def traces_of(workspace):
    return trace.list_traces(workspace.user_home)


def test_a_run_leaves_a_trace(workspace, cp_path):  # noqa: F811
    client = FakeClient([
        call("execute_python_script", {"code": "print(sum([10, 20, 30, 40]))", "purpose": "total"}),
        call("visualize", visualize_args(title="Sales by region")),
        text("Total sales are 100."),
    ])
    run_agent(client, workspace, cp_path, question="How are sales split?")
    [item] = traces_of(workspace)
    assert item["question"] == "How are sales split?" and item["segments"] == 1
    s = item["summary"]
    assert (s["status"], s["llm_calls"], s["tool_calls"], s["charts"]) == ("success", 3, 1, 1)
    data = trace.read_trace(workspace.user_home, item["run_id"])
    kinds = [e["type"] for e in data["segments"][0]["events"]]
    assert kinds.count("llm_call") == 3 and "tool_result" in kinds and "completion" in kinds
    chart = next(e for e in data["segments"][0]["events"] if e["type"] == "result")
    assert chart["title"] == "Sales by region" and chart["rows"] == 4 and "code" in chart
    times = [e["t_ms"] for e in data["segments"][0]["events"]]
    assert times == sorted(times)


def test_a_paused_run_is_one_trace_with_two_segments(workspace, cp_path):  # noqa: F811
    from alidade.analyst.agent import AnalystAgent
    ask = call("ask_user", {"questions": [{"text": "Which metric?", "options": ["Sales", "Units"]}]})
    client = FakeClient([ask, text("By sales, W leads.")])
    first = list(AnalystAgent(client=client, workspace=workspace).run(
        input_tables=[{"name": "sales"}], user_question="Who is best?", checkpoint_db_path=cp_path))
    run_id = first[-1]["run_id"]
    list(AnalystAgent(client=client, workspace=workspace).run(
        input_tables=[{"name": "sales"}], user_question="Sales", resume_run_id=run_id, checkpoint_db_path=cp_path))
    data = trace.read_trace(workspace.user_home, run_id)
    assert [s["kind"] for s in data["segments"]] == ["start", "resume"]
    assert [s["status"] for s in data["segments"]] == ["paused", "success"]
    assert data["question"] == "Who is best?" and data["segments"][1]["input"] == "Sales"
    assert data["summary"]["asks"] == 1 and data["summary"]["llm_calls"] == 2


def test_large_fields_are_cut_and_report_deltas_skipped():
    assert trace.compact({"type": "text_delta", "content": "x"}) is None
    out = trace.compact({"type": "tool_result", "tool": "execute_python_script", "stdout": "y" * 10_000})
    assert len(out["stdout"]) < 2100 and "more characters" in out["stdout"]
    sub = trace.compact({"type": "tool_start", "tool": "visualize", "step_id": "s2"})
    assert sub == {"type": "tool_start", "tool": "visualize", "step_id": "s2"}


def test_traces_can_be_switched_off(workspace, cp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ALIDADE_TRACES", "0")
    run_agent(FakeClient([text("Done.")]), workspace, cp_path)
    assert traces_of(workspace) == []


def test_only_the_newest_traces_are_kept(tmp_path):
    for i in range(5):
        t = trace.RunTrace(tmp_path, f"run{i:04d}", question=str(i))
        t.save("success")
        os.utime(tmp_path / "traces" / f"run{i:04d}.json", (1000 + i, 1000 + i))
    trace.prune(tmp_path / "traces", keep=2)
    assert sorted(p.stem for p in (tmp_path / "traces").glob("*.json")) == ["run0003", "run0004"]


def test_bad_run_ids_are_refused(tmp_path):
    assert trace.read_trace(tmp_path, "../../etc/passwd") is None
    assert trace.RunTrace(tmp_path, "../x", question="q").enabled is False


def test_routes(tmp_path):
    from unittest.mock import patch

    import flask

    from alidade.error_handler import register_error_handlers
    from alidade.routes.traces import traces_bp

    t = trace.RunTrace(tmp_path, "abc123def", question="q?")
    t.observe({"type": "llm_call", "model": "m", "tier": "main", "prompt_tokens": 10, "completion_tokens": 2})
    t.save("success")
    app = flask.Flask(__name__)
    app.register_blueprint(traces_bp)
    register_error_handlers(app)
    with patch("alidade.routes.traces.get_identity_id", return_value="u"), \
         patch("alidade.routes.traces.get_user_home", return_value=tmp_path):
        c = app.test_client()
        listed = c.get("/api/traces").get_json()["data"]["traces"]
        assert [x["run_id"] for x in listed] == ["abc123def"] and listed[0]["summary"]["prompt_tokens"] == 10
        one = c.get("/api/traces/abc123def").get_json()["data"]
        assert one["segments"][0]["events"][0]["type"] == "llm_call"
        assert c.get("/api/traces/nope").get_json()["status"] != "success"   # errors use the JSON envelope
    assert json.loads((tmp_path / "traces" / "abc123def.json").read_text(encoding="utf-8"))["question"] == "q?"
