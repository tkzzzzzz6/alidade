"""The evaluation harness's scoring helpers (eval/run_eval.py, eval/report.py)."""

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

EVAL_DIR = Path(__file__).resolve().parents[3] / "eval"
sys.path.insert(0, str(EVAL_DIR))

import report  # noqa: E402
import run_eval  # noqa: E402


def test_answer_picks_the_recommended_first_option():
    answer, has_options = run_eval.answer_for({"questions": [
        {"text": "Which metric?", "options": [{"label": "Sales"}, {"label": "Units"}]},
    ]})
    assert answer == "Sales" and has_options == [True]


def test_answer_without_options_falls_back_and_is_flagged():
    answer, has_options = run_eval.answer_for({"questions": [{"text": "Anything else?"}]})
    assert answer == run_eval.FALLBACK_ANSWER and has_options == [False]


def test_multiple_questions_are_numbered():
    answer, _ = run_eval.answer_for({"questions": [
        {"text": "a", "options": ["x", "y"]}, {"text": "b", "options": ["p", "q"]},
    ]})
    assert answer == "1. x; 2. p"


@pytest.mark.parametrize("text,wanted,expected", [
    ("Brazil leads with 85%", ["Brazil", "巴西"], True),
    ("巴西最高", ["Brazil", "巴西"], True),
    ("Canada leads", ["Brazil"], False),
    ("anything", None, None),
])
def test_mentions(text, wanted, expected):
    assert run_eval.mentions_ok(text, wanted) is expected


def test_chart_judge_flags_unaggregated_line():
    event = {"type": "result", "content": {"question": "q", "result": {
        "refined_goal": {"title": "t", "chart": {"chart_type": "Line Chart", "encodings": {"x": "k", "y": "v"}}},
        "content": {"rows": [{"k": "a", "v": 1}, {"k": "a", "v": 2}, {"k": "b", "v": 3}]},
    }}}
    assert "chartCheck.unaggregatedSeries" in run_eval.judge_chart(event)["blocking"]


def _run(**over):
    base = {"id": "q", "category": "lookup", "engine": "alidade", "success": True, "completed": True,
            "status": "success", "mentions_ok": True, "charts_shown": 2, "charts_blocking_final": 0,
            "self_check_triggered": 1, "self_check_fixed": 1, "actions": 3, "tool_calls": 2, "llm_calls": 4,
            "prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100, "duration_s": 10,
            "plan_used": True, "asks": [], "asked": 0, "compressions": []}
    return {**base, **over}


def test_metrics_and_compare_render():
    new = [_run(), _run(id="r", category="ambiguous", success=False, completed=False, status="error",
                        asks=[{"has_options": [True]}], asked=1)]
    old = [_run(engine="data_formulator", self_check_triggered=0, self_check_fixed=0, charts_blocking_final=1),
           _run(id="r", engine="data_formulator", self_check_triggered=0, self_check_fixed=0)]
    m = report.metrics(new)
    assert m["success_rate"] == 0.5
    assert m["first_pass_chart_error_rate"] == 0.5  # 2 self-check triggers / 4 charts
    assert m["self_check_repair_rate"] == 1.0
    assert m["ask_options_rate"] == 1.0
    assert report.metrics(old)["first_pass_chart_error_rate"] == 0.25  # judged after the fact
    text = report.render_compare(old, new, ("before", "after"))
    assert "| 任务成功率 | 100% | 50% | -50 pp ⚠️ |" in text


def test_env_file_is_loaded_but_terminal_values_win(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_bytes(
        "﻿# comment\r\nEVAL_API_KEY=\"sk-from-file\"\r\nEVAL_MODEL=file-model\r\nEVAL_API_BASE=\r\n".encode("utf-8"))
    for key in ("EVAL_API_KEY", "EVAL_API_BASE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("EVAL_MODEL", "terminal-model")
    args = run_eval.parse_args(["--env-file", str(env_file)])
    assert args.api_key == "sk-from-file"      # BOM and quotes handled
    assert args.model == "terminal-model"      # terminal value wins over the file
    assert args.api_base == ""                 # explicit empty value kept


def test_placeholder_key_is_rejected(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("EVAL_API_KEY=sk-your-key-here\nEVAL_MODEL=m\n", encoding="utf-8")
    monkeypatch.delenv("EVAL_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        run_eval.parse_args(["--env-file", str(env_file)])


def test_spacing_does_not_break_answer_matching():
    assert run_eval.mentions_ok("最高的月份是 2 月", ["2月"]) is True
    assert run_eval.mentions_ok("Warner  Bros. leads", ["Warner Bros"]) is True


def test_report_rescores_old_results():
    runs = [{"id": "unemp-construction-season", "category": "multistep", "completed": True, "chart_ok": True,
             "success": False, "mentions_ok": False, "summary": "最高的月份是 2 月", "charts": []}]
    assert report.rescore_runs(runs)[0]["success"] is True


# ── harder questions: numeric answers, multi-part answers, clarify-first ──

@pytest.mark.parametrize("text,expected", [
    ("平均每单 511.54 元", True),
    ("average order value is $511.5", True),
    ("平均 461.88（把缺失当 0）", False),       # missing counted as 0
    ("平均 510.63", False),                       # duplicates not removed
    ("1,511.54", False),
])
def test_answer_number_tolerance(text, expected):
    q = {"answer_number": {"value": 511.54, "tolerance": 0.001}}
    assert run_eval.answer_ok(q, text) is expected


def test_numbers_in_handles_thousands_and_negatives():
    assert run_eval.numbers_in("North 69,834.2, change -3.5%") == [69834.2, -3.5]


def test_mentions_all_needs_every_group():
    q = {"mentions_all": [["Musical", "音乐剧"], ["样本", "sample"]]}
    assert run_eval.answer_ok(q, "音乐剧回报率最高，但样本只有 3 部") is True
    assert run_eval.answer_ok(q, "Musical has the best ROI") is False


def test_answer_ok_combines_checks_and_is_none_without_any():
    q = {"mentions_any": ["North"], "answer_number": {"value": 69834}}
    assert run_eval.answer_ok(q, "North: 69,834") is True
    assert run_eval.answer_ok(q, "North: 80,000") is False
    assert run_eval.answer_ok({}, "whatever") is None


def _rec(**over):
    return {"completed": True, "chart_ok": True, "summary": "", "charts": [], "asked": 0, **over}


def test_expect_ask_requires_asking_before_any_chart():
    q = {"expect_ask": True}
    assert run_eval.grade(q, _rec(asked=1, charts_before_ask=0)) == {
        "mentions_ok": None, "clarified_first": True, "grounded": None, "success": True}
    late = run_eval.grade(q, _rec(asked=1, charts_before_ask=2))
    assert late["clarified_first"] is False and late["success"] is False
    never = run_eval.grade(q, _rec(asked=0))
    assert never["clarified_first"] is False and never["success"] is False


def test_grade_without_expect_ask_ignores_asking():
    q = {"mentions_any": ["Brazil"]}
    g = run_eval.grade(q, _rec(summary="Brazil leads", asked=1, charts_before_ask=3))
    assert g["clarified_first"] is None and g["success"] is True


def test_grade_reads_chart_titles_too():
    q = {"mentions_any": ["March", "3月"]}
    assert run_eval.grade(q, _rec(charts=[{"title": "Sales peak in March"}]))["success"] is True


def test_dataset_keys_accepts_one_or_many():
    assert run_eval.dataset_keys({"dataset": "stocks"}) == ["stocks"]
    assert run_eval.dataset_keys({"dataset": ["stocks", "stock_info"]}) == ["stocks", "stock_info"]


def test_raw_csv_keeps_dirty_values_as_text():
    df = run_eval.load_table({"file": "data/messy_sales.csv", "raw": True}, Path("."))
    assert set(df.columns) == {"order_id", "order_date", "region", "amount"}
    assert all(pd.api.types.is_string_dtype(t) for t in df.dtypes)
    assert {"", "N/A"} <= set(df["amount"])
    assert df["amount"].str.startswith("$").any()
    assert {"North", "north", " NORTH "} <= set(df["region"])


def test_question_file_is_consistent():
    datasets, questions = run_eval.load_questions(EVAL_DIR / "questions.yaml")
    ids = [q["id"] for q in questions]
    assert len(ids) == len(set(ids))
    for q in questions:
        for key in run_eval.dataset_keys(q):
            assert key in datasets, (q["id"], key)
        if q.get("expect_ask"):
            assert q["category"] == "clarify"
    for d in datasets.values():
        if "file" in d:
            assert (EVAL_DIR / d["file"]).exists(), d["file"]


# ── token accounting: provider cache hits, tokens before the first question ──

@pytest.mark.parametrize("usage,expected", [
    ({"prompt_tokens": 100, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 64}}, (100, 5, 64)),
    ({"prompt_tokens": 100, "completion_tokens": 5, "prompt_cache_hit_tokens": 80}, (100, 5, 80)),
    ({"prompt_tokens": 100, "completion_tokens": 5, "cache_read_input_tokens": 0}, (100, 5, 0)),
    ({"prompt_tokens": 100, "completion_tokens": 5}, (100, 5, None)),
    ({"prompt_tokens": 100, "completion_tokens": 5, "prompt_tokens_details": None}, (100, 5, None)),
    ({}, None),
    (None, None),
])
def test_usage_numbers_reads_cache_fields(usage, expected):
    assert run_eval._usage_numbers(usage) == expected


def test_usage_numbers_reads_objects_too():
    class Details:
        cached_tokens = 30

    class Usage:
        prompt_tokens, completion_tokens, prompt_tokens_details = 50, 2, Details()

    assert run_eval._usage_numbers(Usage()) == (50, 2, 30)


def test_cache_and_before_direction_metrics():
    runs = [
        _run(category="ambiguous", asked=1, tokens_before_ask=8000, total_tokens=90000,
             prompt_tokens=80000, cached_prompt_tokens=60000),
        _run(category="clarify", expect_ask=True, asked=0, total_tokens=50000,
             prompt_tokens=40000, cached_prompt_tokens=20000),
        _run(category="lookup", prompt_tokens=10000, cached_prompt_tokens=None),
    ]
    m = report.metrics(runs)
    assert m["avg_tokens_before_direction"] == (8000 + 50000) / 2   # never asked → the whole run
    assert m["cache_hit_rate"] == pytest.approx(80000 / 120000)
    assert m["avg_uncached_prompt_tokens"] == (20000 + 20000) / 2


def test_metrics_unknown_for_old_records():
    m = report.metrics([_run(category="ambiguous", asked=1, total_tokens=90000)])
    assert m["avg_tokens_before_direction"] is None
    assert m["cache_hit_rate"] is None and m["avg_uncached_prompt_tokens"] is None


@pytest.mark.parametrize("over,expected", [
    ({"clarified_first": False, "asked": 0}, "没有先反问就直接分析"),
    ({"clarified_first": False, "asked": 1, "charts_before_ask": 2}, "先出了 2 张图才反问"),
    ({"mentions_ok": False}, "结论与标准答案不符"),
])
def test_failure_reason_names_the_cause(over, expected):
    assert report._why(_run(success=False, chart_ok=True, **over)) == expected


# ── multi-angle questions (stage 0 of the parallel sub-agent plan) ──

def _questions():
    return {q["id"]: q for q in run_eval.load_questions(EVAL_DIR / "questions.yaml")[1]}


def test_every_reference_note_passes_its_own_checks():
    # guards against typos in mentions_* / answer_number
    for q in _questions().values():
        assert run_eval.answer_ok(q, q.get("note", "")) is not False, q["id"]


def test_multiangle_questions_check_every_angle():
    multi = [q for q in _questions().values() if q["category"] == "multiangle"]
    assert len(multi) >= 4
    for q in multi:
        assert q.get("chart") and q.get("min_charts", 1) >= 2, q["id"]
        assert len(q.get("mentions_all") or []) + bool(q.get("answer_number")) >= 2, q["id"]


def test_multiangle_answer_with_one_angle_fails():
    q = _questions()["multi-stocks"]
    assert run_eval.answer_ok(q, "GOOGL 收益率最高，NVDA 成交量最大") is True
    assert run_eval.answer_ok(q, "GOOGL 收益率最高") is False
    movies = _questions()["multi-movies"]
    assert run_eval.answer_ok(movies, "Adventure 票房最高；R 级平均 6.43 分") is True
    assert run_eval.answer_ok(movies, "Adventure 票房最高；PG-13 平均 6.05 分") is False


def test_too_few_charts_reason():
    r = _run(success=False, chart_ok=False, min_charts=2, charts_shown=1)
    assert report._why(r) == "只出了 1 张图（要求至少 2 张）"
    assert report._why(_run(success=False, chart_ok=False, charts_shown=0)) == "没有生成图表"


def test_multi_and_subagent_metrics():
    runs = [
        _run(category="multiangle", duration_s=40, llm_calls=8,
             subagents=[{"step_id": "s1", "status": "success"}, {"step_id": "s2", "status": "timeout"}]),
        _run(category="multistep", duration_s=20, subagents=[]),
        _run(category="lookup", duration_s=5, subagents=[]),
    ]
    m = report.metrics(runs)
    assert m["avg_duration_multi"] == 30 and m["avg_duration_multiangle"] == 40
    assert m["avg_llm_calls_multiangle"] == 8
    assert m["subagent_run_rate"] == pytest.approx(1 / 3)
    assert m["subagents_total"] == 2 and m["subagent_fail_rate"] == 0.5
    old = report.metrics([_run(category="multistep")])          # records from before sub-agents
    assert old["subagent_run_rate"] is None and old["subagent_fail_rate"] is None


def test_plan_parallel_width():
    def plan(*steps):
        return {"type": "plan", "steps": list(steps)}

    def st(i, status="pending", deps=(), goal=None):
        d = {"id": i, "title": i, "status": status, "depends_on": list(deps)}
        return {**d, "goal": goal} if goal else d

    old_style = [plan({"id": "step-1", "title": "a", "status": "pending"},
                      {"id": "step-2", "title": "b", "status": "pending"})]
    assert run_eval.plan_parallel_width(old_style) == 1
    flag_off = [plan(st("step-1"), st("step-2"))]                  # depends_on always [], no goals
    assert run_eval.plan_parallel_width(flag_off) == 1
    fan_out = [plan(st("s1", goal="x"), st("s2", goal="y"), st("s3", deps=["s1", "s2"]))]
    assert run_eval.plan_parallel_width(fan_out) == 2
    later = fan_out + [plan(st("s1", "done"), st("s2", "done"), st("s3", deps=["s1", "s2"]))]
    assert run_eval.plan_parallel_width(later) == 2               # the maximum over the run
    chain = [plan(st("a"), st("b", deps=["a"]), st("c", deps=["b"]))]
    assert run_eval.plan_parallel_width(chain) == 1
    assert run_eval.plan_parallel_width([{"type": "result"}]) == 0


def test_parallel_plan_rate():
    runs = [_run(category="multiangle", plan_parallel_width=2),
            _run(category="multiangle", plan_parallel_width=1)]
    assert report.metrics(runs)["parallel_plan_rate_multiangle"] == 0.5
    assert report.metrics([_run(category="multiangle")])["parallel_plan_rate_multiangle"] is None


# ── number provenance in the harness ──

def test_grounding_questions_require_traceable_numbers():
    qs = _questions()
    grounding = [q for q in qs.values() if q["category"] == "grounding"]
    assert len(grounding) >= 5 and all(q.get("max_unsupported") == 0 for q in grounding)
    q = qs["grounding-avatar-roi"]
    ok = _rec(summary="约 11.68 倍", number_check={"checked": 1, "unsupported": 0})
    made_up = _rec(summary="约 11.68 倍，重映后 12.3 倍", number_check={"checked": 2, "unsupported": 1})
    assert run_eval.grade(q, ok)["success"] is True
    g = run_eval.grade(q, made_up)
    assert g["grounded"] is False and g["success"] is False
    # old records without a number check are not failed for it
    assert run_eval.grade(q, _rec(summary="约 11.68 倍"))["grounded"] is None


def test_evidence_collection_and_check():
    events = [
        {"type": "tool_result", "tool": "execute_python_script", "stdout": "gross 2767891499\nbudget 237000000"},
        {"type": "tool_result", "tool": "load_skill", "stdout": "999.99"},          # not data
        {"type": "result", "status": "success",
         "content": {"result": {"content": {"rows": [{"genre": "Adventure", "gross": 241171386}]}}}},
    ]
    ev = run_eval.collect_evidence(events)
    assert len(ev["printed"]) == 1 and len(ev["charts"]) == 1
    result = run_eval.check_numbers("约 11.68 倍；冒险片均值 2.41 亿；另有 999.99", ev, raw=[], given=[])
    assert result["checked"] == 3 and result["unsupported"] == 1
    assert result["unsupported_items"][0]["text"] == "999.99"


def test_table_numbers_reads_cells_text_and_stats():
    df = pd.DataFrame({"amount": ["$1,234.50", "N/A"] * 3 + ["$1,234.50"], "n": [10, 30, 20, 20, 20, 20, 20]})
    cells, stats = run_eval.table_numbers([df])
    assert 1234.5 in cells and 10 in cells                            # cells, numbers inside text
    assert 20.0 in stats and 140.0 in stats and 7.0 in stats          # mean, sum, row count
    # the data notes the agent saw count too ("3 rows are exact duplicates …")
    assert run_eval.data_quality_notes(df) and 3.0 in stats


def test_recheck_numbers_from_saved_evidence(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "grounding-avatar-roi.evidence.json").write_text(json.dumps({
        "printed": ["gross 2767891499\nbudget 237000000"], "charts": [], "given": [],
        "summary": "约 11.68 倍",
    }), encoding="utf-8")
    runs = [_run(id="grounding-avatar-roi", category="grounding", summary="约 11.68 倍", charts=[],
                 chart_ok=True, number_check={"checked": 1, "unsupported": 1})]
    assert run_eval.recheck_numbers(tmp_path, runs) == 1
    assert runs[0]["number_check"]["unsupported"] == 0 and runs[0]["success"] is True


def test_number_metrics_and_reason():
    runs = [_run(number_check={"checked": 4, "unsupported": 1,
                               "unsupported_items": [{"text": "9.2 倍", "context": "约 9.2 倍"}]}),
            _run(number_check={"checked": 6, "unsupported": 0}),
            _run()]                                                    # an old record
    m = report.metrics(runs)
    assert m["numbers_checked"] == 10 and m["unsupported_number_rate"] == 0.1
    assert m["runs_with_unsupported_rate"] == 0.5
    assert report.metrics([_run()])["unsupported_number_rate"] is None
    why = report._why(_run(success=False, chart_ok=True, grounded=False,
                           number_check={"unsupported": 1, "unsupported_items": [{"text": "9.2 倍"}]}))
    assert why == "结论里有 1 个找不到出处的数字（9.2 倍）"
    assert "找不到出处的数字（人工抽查用）" in report.render_single(runs)


def test_plan_round_metrics():
    runs = [_run(plan_updates=3, calls_dropped=1), _run(plan_updates=1, calls_dropped=0)]
    m = report.metrics(runs)
    assert m["avg_plan_updates"] == 2 and m["calls_dropped"] == 1
    assert report.metrics([_run()])["avg_plan_updates"] is None      # records from before
