"""Plan-then-execute, chart self-check and context compression."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from alidade.analyst.engine import chart_check, compression, planning

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


# ── chart self-check rules ────────────────────────────────────────────────


def codes(report):
    return {i.code: i.severity for i in report.issues}


def test_clean_bar_chart_passes():
    df = pd.DataFrame({"region": list("NSEW"), "sales": [1, 2, 3, 4]})
    report = chart_check.check_chart(df, {"chart_type": "Bar Chart", "encodings": {"x": "region", "y": "sales"}})
    assert report.issues == []


def test_too_many_bar_categories_blocks():
    df = pd.DataFrame({"sku": [f"s{i}" for i in range(80)], "units": range(80)})
    report = chart_check.check_chart(df, {"chart_type": "Bar Chart", "encodings": {"x": "sku", "y": "units"}})
    assert codes(report)["chartCheck.tooManyCategories"] == "blocking"


def test_horizontal_bar_detects_category_axis():
    df = pd.DataFrame({"units": range(80), "sku": [f"s{i}" for i in range(80)]})
    report = chart_check.check_chart(df, {"chart_type": "Bar Chart", "encodings": {"x": "units", "y": "sku"}})
    assert "chartCheck.tooManyCategories" in codes(report)


def test_unaggregated_line_blocks_but_color_split_is_fine():
    df = pd.DataFrame({"month": [1, 1, 2, 2], "store": ["a", "b", "a", "b"], "sales": [1, 2, 3, 4]})
    bad = chart_check.check_chart(df, {"chart_type": "Line Chart", "encodings": {"x": "month", "y": "sales"}})
    assert codes(bad)["chartCheck.unaggregatedSeries"] == "blocking"
    ok = chart_check.check_chart(df, {"chart_type": "Line Chart",
                                      "encodings": {"x": "month", "y": "sales", "color": "store"}})
    assert ok.issues == []


def _lines(n_series: int) -> pd.DataFrame:
    return pd.DataFrame([{"year": y, "country": f"c{i}", "twh": i + y}
                         for i in range(n_series) for y in (2000, 2001, 2002)])


@pytest.mark.parametrize("n,expected", [
    (10, None),                                  # 10 lines: fine
    (14, ("chartCheck.lineManySeries", "warning")),
    (20, ("chartCheck.lineManySeries", "warning")),
    (21, ("chartCheck.lineTooManySeries", "blocking")),
])
def test_line_series_limits(n, expected):
    report = chart_check.check_chart(_lines(n), {"chart_type": "Line Chart",
                                                 "encodings": {"x": "year", "y": "twh", "color": "country"}})
    line_codes = {c: s for c, s in codes(report).items() if c.startswith("chartCheck.line")}
    assert line_codes == ({} if expected is None else dict([expected]))
    assert "chartCheck.tooManyColors" not in codes(report)  # no double report


def test_line_series_counted_on_stroke_dash_too():
    report = chart_check.check_chart(_lines(25), {"chart_type": "Line Chart",
                                                  "encodings": {"x": "year", "y": "twh", "strokeDash": "country"}})
    assert codes(report)["chartCheck.lineTooManySeries"] == "blocking"


def test_bar_chart_colors_unaffected_by_line_rule():
    df = pd.DataFrame({"k": [f"k{i}" for i in range(15)], "g": [f"g{i}" for i in range(15)], "v": range(15)})
    report = chart_check.check_chart(df, {"chart_type": "Bar Chart", "encodings": {"x": "k", "y": "v", "color": "g"}})
    assert not any(c.startswith("chartCheck.line") for c in codes(report))


def test_single_point_line_blocks():
    df = pd.DataFrame({"year": [2024], "v": [3]})
    report = chart_check.check_chart(df, {"chart_type": "Line Chart", "encodings": {"x": "year", "y": "v"}})
    assert "chartCheck.singlePointSeries" in codes(report)


def test_pie_rules():
    df = pd.DataFrame({"c": [f"c{i}" for i in range(15)], "v": [1] * 14 + [-1]})
    report = chart_check.check_chart(df, {"chart_type": "Pie Chart", "encodings": {"color": "c", "size": "v"}})
    assert {"chartCheck.tooManySlices", "chartCheck.negativePie"} <= set(codes(report))


def test_warnings_do_not_block():
    df = pd.DataFrame({"region": list("NSEW"), "sales": [1, None, None, 4]})
    report = chart_check.check_chart(df, {"chart_type": "Bar Chart", "encodings": {"x": "region", "y": "sales"}})
    assert codes(report) == {"chartCheck.manyNulls": "warning"}
    assert report.blocking == []


def test_no_numeric_axis_blocks():
    df = pd.DataFrame({"a": list("xyz"), "b": list("pqr")})
    report = chart_check.check_chart(df, {"chart_type": "Bar Chart", "encodings": {"x": "a", "y": "b"}})
    assert "chartCheck.noQuantitativeAxis" in codes(report)


# ── chart self-check inside the graph ─────────────────────────────────────


UNAGG_CODE = (
    "import pandas as pd\n"
    "trend = pd.DataFrame({'month': [1, 1, 2, 2], 'sales': [1, 2, 3, 4]})\n"
)
AGG_CODE = (
    "import pandas as pd\n"
    "trend = pd.DataFrame({'month': [1, 2], 'sales': [3, 7]})\n"
)


def line_args(code):
    return visualize_args(code=code, output_variable="trend",
                          chart={"chart_type": "Line Chart", "encodings": {"x": "month", "y": "sales"}})


def test_bad_chart_is_withheld_then_fixed(workspace, cp_path):
    client = FakeClient([
        call("visualize", line_args(UNAGG_CODE)),
        call("visualize", line_args(AGG_CODE)),
        text("Sales rose."),
    ])
    events = run_agent(client, workspace, cp_path, max_iterations=1)
    checks = [e for e in events if e["type"] == "chart_check"]
    assert [c["status"] for c in checks] == ["retrying", "fixed"]
    assert checks[0]["issues"][0]["code"] == "chartCheck.unaggregatedSeries"
    results = [e for e in events if e["type"] == "result"]
    assert len(results) == 1  # the bad chart was never shown
    assert results[0]["content"]["result"]["chart_check"]["status"] == "fixed"
    # the model was told why, as the failed action's observation
    tool_msgs = [m for m in client.seen[1] if m.get("role") == "tool"]
    assert "CHART CHECK FAILED" in tool_msgs[-1]["content"]
    # the withheld attempt did not use up the 1-action budget
    assert events[-1]["status"] == "max_iterations"
    assert events[-1]["content"]["total_steps"] == 1


def test_chart_shown_with_warnings_after_retry_budget(workspace, cp_path):
    client = FakeClient([
        call("visualize", line_args(UNAGG_CODE)),
        call("visualize", line_args(UNAGG_CODE)),
        call("visualize", line_args(UNAGG_CODE)),
        text("done"),
    ])
    events = run_agent(client, workspace, cp_path)
    statuses = [e["status"] for e in events if e["type"] == "chart_check"]
    assert statuses == ["retrying", "retrying", "warning"]
    assert sum(1 for e in events if e["type"] == "result") == 1
    tool_msgs = [m for m in client.seen[3] if m.get("role") == "tool"]
    assert "[CHART CHECK] The chart was shown" in tool_msgs[-1]["content"]


# ── planning ──────────────────────────────────────────────────────────────


def test_plan_tool_streams_plan_events(workspace, cp_path):
    steps = [{"title": "Sales by region", "status": "in_progress"},
             {"title": "Trend over time", "status": "pending"}]
    done = [{"title": "Sales by region", "status": "done"},
            {"title": "Trend over time", "status": "skipped"}]
    client = FakeClient([
        call("update_plan", {"steps": steps}),
        call("visualize", visualize_args()),
        call("update_plan", {"steps": done}),
        text("All done."),
    ])
    events = run_agent(client, workspace, cp_path)
    plans = [e for e in events if e["type"] == "plan"]
    assert [s["status"] for s in plans[0]["steps"]] == ["in_progress", "pending"]
    assert [s["status"] for s in plans[-1]["steps"]] == ["done", "skipped"]
    assert plans[0]["steps"][0]["id"] == "step-1"
    # update_plan is offered as a tool and explained in the system prompt
    assert "## Planning" in client.seen[0][0]["content"]


def test_plan_survives_pause(workspace, cp_path):
    client = FakeClient([
        call("update_plan", {"steps": [{"title": "Ask metric", "status": "in_progress"},
                                       {"title": "Chart it", "status": "pending"}]}),
        call("ask_user", {"questions": [{"text": "Metric?", "options": ["Sales", "Units"]}]}),
    ])
    events = run_agent(client, workspace, cp_path)
    run_id = events[-1]["run_id"]
    client2 = FakeClient([text("ok")])
    run_agent(client2, workspace, cp_path, question="sales", resume_run_id=run_id)
    from alidade.analyst.agent import AnalystAgent  # noqa: F401
    # the resumed model still sees the plan tool call in its history
    assert any("update_plan" in json.dumps(m) for m in client2.seen[0])


def test_normalize_steps_validates():
    steps = planning.normalize_steps(
        [{"title": " A ", "status": "DONE"}, {"title": "", "status": "done"}, "B", 5,
         {"title": "C", "status": "weird"}] + [{"title": f"x{i}", "status": "pending"} for i in range(10)]
    )
    assert [s["title"] for s in steps[:3]] == ["A", "B", "C"]
    assert [s["status"] for s in steps[:3]] == ["done", "pending", "pending"]
    assert len(steps) == planning.MAX_PLAN_STEPS


# ── context compression ───────────────────────────────────────────────────


def _long_history(n_rounds: int, payload: int = 6000) -> list[dict]:
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "ctx + question"}]
    for i in range(n_rounds):
        msgs.append({"role": "assistant", "content": None, "tool_calls": [{
            "id": f"c{i}", "type": "function",
            "function": {"name": "execute_python_script",
                         "arguments": json.dumps({"purpose": f"step {i}", "code": "print(1)"})}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": f"result {i} " + "x" * payload})
    return msgs


def _valid(msgs: list[dict]) -> bool:
    """Every tool message directly answers a preceding assistant tool call."""
    open_ids: set[str] = set()
    for m in msgs:
        if m.get("role") == "assistant":
            open_ids = {tc["id"] for tc in m.get("tool_calls") or []}
        elif m.get("role") == "tool":
            if m.get("tool_call_id") not in open_ids:
                return False
        else:
            open_ids = set()
    return True


def test_under_budget_is_untouched():
    msgs = _long_history(2, payload=100)
    out, info = compression.compress_if_needed(msgs, None, budget=100_000)
    assert info is None and out is msgs


def test_trim_stage_shortens_old_results_only():
    msgs = _long_history(12)
    out, info = compression.compress_if_needed(msgs, None, budget=12_000)
    assert info["stage"] == "trim"
    assert info["tokens_after"] < info["tokens_before"]
    assert compression.TRIMMED_MARK in out[3]["content"]
    assert compression.TRIMMED_MARK not in out[-1]["content"]  # recent tail kept verbatim
    assert _valid(out)


def test_summary_stage_uses_llm_and_keeps_structure():
    msgs = _long_history(40)
    calls = []

    def complete(m):
        calls.append(m)
        return "- inspected sales; found 4 regions"

    out, info = compression.compress_if_needed(msgs, complete, budget=4_000)
    assert info["stage"] == "summary" and info["messages_summarised"] > 0
    assert calls, "summariser was used"
    note = next(m for m in out if str(m.get("content", "")).startswith("[EARLIER STEPS SUMMARY]"))
    assert "found 4 regions" in note["content"]
    assert out[0]["role"] == "system" and out[1]["content"] == "ctx + question"
    assert _valid(out)


def test_summary_falls_back_to_digest_when_llm_fails():
    msgs = _long_history(40)

    def boom(_m):
        raise RuntimeError("down")

    out, info = compression.compress_if_needed(msgs, boom, budget=4_000)
    note = next(m for m in out if str(m.get("content", "")).startswith("[EARLIER STEPS SUMMARY]"))
    assert "called execute_python_script: step 0" in note["content"]


def test_compression_runs_inside_graph(workspace, cp_path, monkeypatch):
    monkeypatch.setenv("ALIDADE_AGENT_CONTEXT_BUDGET", "4000")
    big = "print('" + "y" * 9000 + "')"
    script = [call("execute_python_script", {"purpose": f"p{i}", "code": big}) for i in range(11)]
    client = FakeClient(script + [text("done")])
    client.plain_replies = ["- earlier exploration summarised"] * 10
    events = run_agent(client, workspace, cp_path)
    compressed = [e for e in events if e["type"] == "context_compressed"]
    assert compressed and compressed[-1]["tokens_after"] < compressed[-1]["tokens_before"]
    assert events[-1]["type"] == "completion"
    assert _valid(client.seen[-1])


def test_prompts_ask_before_exploring_vague_requests():
    from alidade.analyst.engine.prompts import SYSTEM_PROMPT
    assert "before any `visualize` and before `update_plan`" in SYSTEM_PROMPT
    assert "do not recommend what to buy or sell" in SYSTEM_PROMPT
    plan = planning.PLANNING_PROMPT
    assert "analyse our\nsales performance" not in plan  # no longer the example of a request to plan
    assert "clarify with `ask_user` first" in plan
