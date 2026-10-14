#!/usr/bin/env python
"""Summarise evaluation runs written by ``run_eval.py``.

    python eval/report.py eval/results/new-deepseek
    python eval/report.py eval/results/old-deepseek eval/results/new-deepseek --labels 改造前 改造后

One directory → a summary; two → a side-by-side comparison with deltas.
Markdown goes to stdout (and to ``--out`` if given).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

CATEGORY_NAMES = {
    "lookup": "查找结论",
    "trend": "趋势图",
    "multistep": "多步计算",
    "trap": "易错图表",
    "ambiguous": "模糊问题",
    "clarify": "必须先澄清",
    "cleaning": "数据清洗",
    "join": "多表关联",
    "multiangle": "多角度分析",
    "grounding": "数字溯源",
    "planned": "三步分析",
    "memory": "口径记忆",
    "insights": "自动发现洞察",
}
VAGUE = {"ambiguous", "clarify"}
MULTI = {"multistep", "multiangle", "planned"}   # where parallel sub-agents should pay off


def load_runs(path: str | Path, rescore: bool = True, recheck_numbers: bool = False) -> list[dict[str, Any]]:
    path = Path(path)
    file = path / "runs.jsonl" if path.is_dir() else path
    with open(file, encoding="utf-8") as fh:
        runs = [json.loads(line) for line in fh if line.strip()]
    if recheck_numbers:
        import run_eval
        n = run_eval.recheck_numbers(file.parent, runs)
        print(f"[number check] re-checked {n} of {len(runs)} runs in {file.parent}", file=sys.stderr)
    return rescore_runs(runs) if rescore else runs


def rescore_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Re-check answers with the current rules and questions.yaml.

    Keeps old result files comparable after a grading fix (e.g. "2 月" vs
    "2月") without re-running the models.
    """
    try:
        import run_eval
        _, questions = run_eval.load_questions(run_eval.DEFAULT_QUESTIONS)
    except Exception:
        return runs
    by_id = {q["id"]: q for q in questions}
    for r in runs:
        q = by_id.get(r.get("id"))
        if not q or "summary" not in r:
            continue
        r.update(run_eval.grade(q, r))
        if q.get("category"):
            r["category"] = q["category"]
    return runs


# ── prices (for the model-routing cost estimate) ───────────────────────────

def load_prices(text: str | None = None) -> dict[str, tuple[float, float, float]]:
    """``EVAL_PRICES`` → {model: (input, cached input, output)} per 1M tokens.

    Format: ``deepseek-pro=0.55/0.14/2.19; deepseek-flash=0.14/0.035/0.28``
    (``,`` also works between the three numbers). Read from the environment,
    else from ``eval/.env``. Any currency; the report does not convert.
    """
    import os
    if text is None:
        text = os.environ.get("EVAL_PRICES", "")
        env_file = Path(__file__).resolve().parent / ".env"
        if not text and env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("EVAL_PRICES="):
                    text = line.split("=", 1)[1].strip().strip('"').strip("'")
    prices: dict[str, tuple[float, float, float]] = {}
    for part in (text or "").split(";"):
        if "=" not in part:
            continue
        model, nums = part.split("=", 1)
        try:
            values = [float(x) for x in nums.replace(",", "/").split("/") if x.strip()]
        except ValueError:
            continue
        if len(values) == 3:
            prices[model.strip()] = (values[0], values[1], values[2])
    return prices


def run_cost(run: dict[str, Any], prices: dict[str, tuple[float, float, float]]) -> float | None:
    """Estimated cost of one run from its per-model tokens (None if a price is missing)."""
    by_model = run.get("by_model")
    if not by_model:
        return None
    total = 0.0
    for model, u in by_model.items():
        if model not in prices:
            return None
        p_in, p_cached, p_out = prices[model]
        cached = u.get("cached", 0)
        total += ((u.get("prompt", 0) - cached) * p_in + cached * p_cached + u.get("completion", 0) * p_out) / 1e6
    return total


# ── metrics ────────────────────────────────────────────────────────────────

def _avg_of(items: list[dict[str, Any]], key: str) -> float | None:
    """Mean of ``key`` over the items that have it (None when none do)."""
    values = [float(i[key]) for i in items if isinstance(i.get(key), (int, float))]
    return sum(values) / len(values) if values else None


def _rate(num: float, den: float) -> float | None:
    return None if not den else num / den


def _mean(values: list[float]) -> float | None:
    values = [v for v in values if v is not None]
    return statistics.fmean(values) if values else None


def metrics(runs: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(runs)
    ran = [r for r in runs if r.get("status") not in ("timeout", "crashed")]
    shown = sum(r.get("charts_shown", 0) for r in ran)
    has_self_check = any(r.get("engine") == "alidade" for r in runs)
    first_fail = sum(
        (r.get("self_check_triggered", 0) if has_self_check else r.get("charts_blocking_final", 0)) for r in ran)
    triggered = sum(r.get("self_check_triggered", 0) for r in ran)
    fixed = sum(r.get("self_check_fixed", 0) for r in ran)
    asks = [a for r in ran for a in r.get("asks", [])]
    asks_with_options = sum(1 for a in asks if a.get("has_options") and all(a["has_options"]))
    ambiguous = [r for r in ran if r.get("category") in VAGUE]
    clarify = [r for r in ran if r.get("expect_ask")]
    compressions = [c for r in ran for c in r.get("compressions", [])]
    judged = [r["judge"]["score"] for r in ran if isinstance(r.get("judge"), dict) and r["judge"].get("score")]

    # provider-reported prefix-cache hits (absent in records from before it was tracked)
    cache_runs = [r for r in ran if r.get("cached_prompt_tokens") is not None]
    cache_prompt = sum(r.get("prompt_tokens", 0) for r in cache_runs)

    # tokens spent before the user had a say: up to the first question, or the
    # whole run when the agent never asked. Needs ``tokens_before_ask``.
    before_direction = (None if any(r.get("asked") and r.get("tokens_before_ask") is None for r in ambiguous)
                        else _mean([r.get("tokens_before_ask") if r.get("asked") else r.get("total_tokens")
                                    for r in ambiguous]))

    multi = [r for r in ran if r.get("category") in MULTI]
    multiangle = [r for r in ran if r.get("category") == "multiangle"]
    subagents = [s for r in ran for s in r.get("subagents") or []]

    sim = [r for r in ran if r.get("user_sim")]
    memory = [r for r in ran if r.get("followup_question")]
    routed = [r for r in ran if set(r.get("llm_tiers") or {}) & {"fast", "strong"}]
    tiers: dict[str, int] = defaultdict(int)
    for r in routed:
        for k, v in (r.get("llm_tiers") or {}).items():
            tiers[k] += v
    def per_call_output(tier: str) -> float | None:
        calls = sum((r.get("by_tier") or {}).get(tier, {}).get("calls", 0) for r in routed)
        out = sum((r.get("by_tier") or {}).get(tier, {}).get("completion", 0) for r in routed)
        return out / calls if calls else None
    prices = load_prices()
    costs = [run_cost(r, prices) for r in ran]
    checked_runs = [r for r in ran if isinstance((r.get("number_check") or {}).get("checked"), int)]
    n_checked = sum(r["number_check"]["checked"] for r in checked_runs)
    n_unsupported = sum(r["number_check"]["unsupported"] for r in checked_runs)

    by_cat: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in runs:
        by_cat[r.get("category", "?")].append(r)

    return {
        "n": n,
        "success_rate": _rate(sum(1 for r in runs if r.get("success")), n),
        "completion_rate": _rate(sum(1 for r in runs if r.get("completed")), n),
        "answer_correct_rate": _rate(sum(1 for r in runs if r.get("mentions_ok") is True),
                                     sum(1 for r in runs if r.get("mentions_ok") is not None)),
        "judge_score": _mean(judged),
        "charts_shown": shown,
        "first_pass_chart_error_rate": _rate(first_fail, shown),
        "final_chart_error_rate": _rate(sum(r.get("charts_blocking_final", 0) for r in ran), shown),
        "final_chart_warning_rate": _rate(sum(1 for r in ran for c in r.get("charts") or [] if c.get("warnings")), shown),
        "self_check_triggered": triggered,
        "self_check_repair_rate": _rate(fixed, triggered),
        "avg_actions": _mean([r.get("actions") for r in ran]),
        "avg_tool_calls": _mean([r.get("tool_calls") for r in ran]),
        "avg_llm_calls": _mean([r.get("llm_calls") for r in ran]),
        "avg_prompt_tokens": _mean([r.get("prompt_tokens") for r in ran]),
        "avg_completion_tokens": _mean([r.get("completion_tokens") for r in ran]),
        "avg_total_tokens": _mean([r.get("total_tokens") for r in ran]),
        "median_total_tokens": statistics.median([r.get("total_tokens", 0) for r in ran]) if ran else None,
        "cache_hit_rate": _rate(sum(r["cached_prompt_tokens"] for r in cache_runs), cache_prompt) if cache_prompt else None,
        "avg_uncached_prompt_tokens": (_mean([r.get("prompt_tokens", 0) - r["cached_prompt_tokens"] for r in cache_runs])
                                       if cache_runs else None),
        "avg_duration_s": _mean([r.get("duration_s") for r in ran]),
        "plan_rate": _rate(sum(1 for r in ran if r.get("plan_used")), len(ran)),
        "avg_plan_updates": (_mean([r.get("plan_updates") for r in ran])
                             if all("plan_updates" in r for r in ran) and ran else None),
        "calls_dropped": (sum(r.get("calls_dropped", 0) for r in ran)
                          if all("calls_dropped" in r for r in ran) and ran else None),
        "ask_rate_ambiguous": _rate(sum(1 for r in ambiguous if r.get("asked")), len(ambiguous)),
        # needs the event order, recorded since the clarify questions were added
        "ask_first_rate_ambiguous": (None if any("charts_before_ask" not in r for r in ambiguous) else
                                     _rate(sum(1 for r in ambiguous if r.get("asked") and r.get("charts_before_ask") == 0),
                                           len(ambiguous))),
        "clarify_success_rate": _rate(sum(1 for r in clarify if r.get("clarified_first")), len(clarify)),
        "avg_tokens_ambiguous": _mean([r.get("total_tokens") for r in ambiguous]),
        "avg_tokens_before_direction": before_direction,
        "avg_charts_ambiguous": _mean([r.get("charts_shown") for r in ambiguous]),
        "avg_tokens_clear": _mean([r.get("total_tokens") for r in ran if r.get("category") not in VAGUE]),
        "ask_rate_clear": _rate(sum(1 for r in ran if r.get("category") not in VAGUE and r.get("asked")),
                                sum(1 for r in ran if r.get("category") not in VAGUE)),
        "asks_total": len(asks),
        "ask_options_rate": _rate(asks_with_options, len(asks)),
        "avg_duration_multi": _mean([r.get("duration_s") for r in multi]),
        "avg_duration_multiangle": _mean([r.get("duration_s") for r in multiangle]),
        "avg_llm_calls_multiangle": _mean([r.get("llm_calls") for r in multiangle]),
        "parallel_plan_rate_multiangle": (_rate(sum(1 for r in multiangle if (r.get("plan_parallel_width") or 0) >= 2),
                                                len(multiangle))
                                          if multiangle and all("plan_parallel_width" in r for r in multiangle) else None),
        "subagent_run_rate": (_rate(sum(1 for r in ran if r.get("subagents")), len(ran))
                              if any("subagents" in r for r in ran) else None),
        "subagents_total": len(subagents),
        "subagent_fail_rate": _rate(sum(1 for s in subagents if s.get("status") != "success"), len(subagents)),
        # per sub-agent (older runs did not record these: None)
        "avg_subagent_seconds": _avg_of(subagents, "duration_s"),
        "avg_subagent_calls": _avg_of(subagents, "calls"),
        "avg_subagent_output_tokens": _avg_of(subagents, "completion_tokens"),
        "avg_subagent_uncached_input": _avg_of(
            [{"v": s["prompt_tokens"] - (s.get("cached_tokens") or 0)} for s in subagents
             if s.get("prompt_tokens") is not None], "v"),
        "intent_ok_rate": (_rate(sum(1 for r in sim if r.get("intent_ok")), len(sim)) if sim else None),
        "intent_ok_rate_asked": (_rate(sum(1 for r in sim if r.get("intent_ok") and r.get("asked")),
                                       sum(1 for r in sim if r.get("asked"))) if sim else None),
        "intent_ok_rate_not_asked": (_rate(sum(1 for r in sim if r.get("intent_ok") and not r.get("asked")),
                                           sum(1 for r in sim if not r.get("asked"))) if sim else None),
        "numbers_checked": n_checked if checked_runs else None,
        "unsupported_number_rate": _rate(n_unsupported, n_checked) if checked_runs else None,
        "runs_with_unsupported_rate": (_rate(sum(1 for r in checked_runs if r["number_check"]["unsupported"]),
                                             len(checked_runs)) if checked_runs else None),
        "definitions_saved_rate": (_rate(sum(1 for r in memory if r.get("definitions_saved")), len(memory))
                                   if memory else None),
        "followup_ask_rate": _rate(sum(1 for r in memory if r.get("followup_asked")), len(memory)) if memory else None,
        "followup_ok_rate": _rate(sum(1 for r in memory if r.get("followup_ok")), len(memory)) if memory else None,
        "followup_applied_rate": (_rate(sum(1 for r in memory if r.get("followup_definitions_applied")), len(memory))
                                  if memory else None),
        "avg_followup_tokens": _mean([r.get("followup_tokens") for r in memory]) if memory else None,
        "avg_followup_duration": _mean([r.get("followup_duration_s") for r in memory]) if memory else None,
        "strong_call_share": (_rate(tiers["strong"], tiers["strong"] + tiers["fast"]) if routed else None),
        "escalated_runs": (sum(1 for r in routed if r.get("escalations")) if routed else None),
        "strong_output_per_call": per_call_output("strong") if routed else None,
        "fast_output_per_call": per_call_output("fast") if routed else None,
        "avg_cost": (_mean(costs) if ran and all(c is not None for c in costs) else None),
        "compressed_runs": sum(1 for r in ran if r.get("compressions")),
        "compression_avg_before": _mean([c.get("tokens_before") for c in compressions]),
        "compression_avg_after": _mean([c.get("tokens_after") for c in compressions]),
        "estimated_token_calls": sum(r.get("estimated_calls", 0) for r in ran),
        "timeouts": sum(1 for r in runs if r.get("status") == "timeout"),
        "by_category": {c: _rate(sum(1 for r in rs if r.get("success")), len(rs)) for c, rs in sorted(by_cat.items())},
        "by_category_n": {c: len(rs) for c, rs in by_cat.items()},
    }


# ── formatting ─────────────────────────────────────────────────────────────

def pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.0f}%"


def num(v: float | None, digits: int = 1) -> str:
    if v is None:
        return "—"
    return f"{v:,.0f}" if abs(v) >= 100 else f"{v:.{digits}f}"


def cnt(v: float | None) -> str:
    return "—" if v is None else f"{int(round(v)):,}"


ROWS: list[tuple[str, str, Callable[[Any], str], int]] = [
    # (label, key, formatter, better: +1 higher is better, -1 lower is better, 0 neutral)
    ("任务成功率", "success_rate", pct, +1),
    ("正常结束率", "completion_rate", pct, +1),
    ("结论正确率（有标准答案的题）", "answer_correct_rate", pct, +1),
    ("LLM 评分（1–5）", "judge_score", lambda v: num(v, 2), +1),
    ("展示的图表数", "charts_shown", cnt, 0),
    ("首次出图的错误率（自检前）", "first_pass_chart_error_rate", pct, -1),
    ("最终展示图表的错误率", "final_chart_error_rate", pct, -1),
    ("最终展示图表带警告的比例", "final_chart_warning_rate", pct, -1),
    ("自检触发次数", "self_check_triggered", cnt, 0),
    ("自检后修正成功率", "self_check_repair_rate", pct, +1),
    ("平均动作数", "avg_actions", num, 0),
    ("平均工具调用数", "avg_tool_calls", num, 0),
    ("平均 LLM 调用数", "avg_llm_calls", num, -1),
    ("平均输入 token", "avg_prompt_tokens", num, -1),
    ("平均输出 token", "avg_completion_tokens", num, -1),
    ("平均总 token", "avg_total_tokens", num, -1),
    ("总 token 中位数", "median_total_tokens", num, -1),
    ("输入 token 的缓存命中率", "cache_hit_rate", pct, +1),
    ("平均未命中缓存的输入 token", "avg_uncached_prompt_tokens", num, -1),
    ("平均耗时（秒）", "avg_duration_s", num, -1),
    ("每题平均费用（按 EVAL_PRICES 估算）", "avg_cost", lambda v: "—" if v is None else f"{v:.4f}", -1),
    ("模型分工：强模型调用占比", "strong_call_share", pct, 0),
    ("模型分工：小模型的结论被强模型重写的题数", "escalated_runs", cnt, 0),
    ("模型分工：关键步骤每次调用的平均输出 token", "strong_output_per_call", num, 0),
    ("模型分工：常规步骤每次调用的平均输出 token", "fast_output_per_call", num, 0),
    ("使用计划（update_plan）的比例", "plan_rate", pct, 0),
    ("平均 update_plan 调用次数", "avg_plan_updates", num, -1),
    ("和动作一起被丢弃的工具调用（总数）", "calls_dropped", cnt, -1),
    ("模糊/需澄清题：反问的比例", "ask_rate_ambiguous", pct, +1),
    ("模糊/需澄清题：出图前先反问的比例", "ask_first_rate_ambiguous", pct, +1),
    ("必须先澄清的题：做到的比例", "clarify_success_rate", pct, +1),
    ("模糊/需澄清题：平均总 token", "avg_tokens_ambiguous", num, -1),
    ("模糊/需澄清题：用户确认方向前的平均 token", "avg_tokens_before_direction", num, -1),
    ("模糊/需澄清题：平均图表数", "avg_charts_ambiguous", lambda v: num(v, 1), -1),
    ("明确问题：平均总 token", "avg_tokens_clear", num, -1),
    ("明确问题中反问的比例", "ask_rate_clear", pct, -1),
    ("反问带 ≥2 个选项的比例", "ask_options_rate", pct, +1),
    ("模拟用户：按用户真实意图答对的比例", "intent_ok_rate", pct, +1),
    ("模拟用户：反问过的题，答对的比例", "intent_ok_rate_asked", pct, +1),
    ("模拟用户：没反问的题，答对的比例", "intent_ok_rate_not_asked", pct, +1),
    ("口径记忆：第一问后保存了口径的比例", "definitions_saved_rate", pct, +1),
    ("口径记忆：第二问用上了已保存口径的比例", "followup_applied_rate", pct, +1),
    ("口径记忆：第二问又反问的比例", "followup_ask_rate", pct, -1),
    ("口径记忆：第二问答对的比例", "followup_ok_rate", pct, +1),
    ("口径记忆：第二问的平均 token", "avg_followup_tokens", num, -1),
    ("口径记忆：第二问的平均耗时（秒）", "avg_followup_duration", num, -1),
    ("多步/多角度题：平均耗时（秒）", "avg_duration_multi", num, -1),
    ("多角度题：平均耗时（秒）", "avg_duration_multiangle", num, -1),
    ("多角度题：平均 LLM 调用数", "avg_llm_calls_multiangle", num, -1),
    ("多角度题：计划里有可并行步骤的比例", "parallel_plan_rate_multiangle", pct, 0),
    ("使用子 Agent 的题目比例", "subagent_run_rate", pct, 0),
    ("子 Agent 总数", "subagents_total", cnt, 0),
    ("子 Agent 失败率", "subagent_fail_rate", pct, -1),
    ("子 Agent 一步的平均耗时（秒）", "avg_subagent_seconds", num, -1),
    ("子 Agent 一步的平均 LLM 调用数", "avg_subagent_calls", num, -1),
    ("子 Agent 一步的平均输出 token", "avg_subagent_output_tokens", num, -1),
    ("子 Agent 一步的平均未命中缓存输入 token", "avg_subagent_uncached_input", num, -1),
    ("结论中检查的数字个数", "numbers_checked", cnt, 0),
    ("结论中找不到出处的数字比例", "unsupported_number_rate", pct, -1),
    ("含找不到出处数字的回答比例", "runs_with_unsupported_rate", pct, -1),
    ("触发上下文压缩的题数", "compressed_runs", cnt, 0),
    ("压缩前平均 token", "compression_avg_before", num, 0),
    ("压缩后平均 token", "compression_avg_after", num, 0),
    ("超时题数", "timeouts", cnt, -1),
]


def _header(runs: list[dict[str, Any]]) -> str:
    engines = sorted({r.get("engine", "?") for r in runs})
    models = sorted({r.get("model", "?") for r in runs})
    return f"engine: {', '.join(engines)} · model: {', '.join(models)} · runs: {len(runs)}"


def render_single(runs: list[dict[str, Any]]) -> str:
    m = metrics(runs)
    lines = [f"# 评测结果\n\n{_header(runs)}\n", "| 指标 | 数值 |", "|---|---|"]
    # rows with no data for this run (e.g. sub-agent rows when they were off) are left out
    lines += [f"| {label} | {fmt(m[key])} |" for label, key, fmt, _ in ROWS if m[key] is not None]
    lines += ["", "## 分类成功率", "", "| 类别 | 题数 | 成功率 |", "|---|---|---|"]
    lines += [f"| {CATEGORY_NAMES.get(c, c)} | {m['by_category_n'][c]} | {pct(v)} |" for c, v in m["by_category"].items()]
    failures = [r for r in runs if not r.get("success")]
    if failures:
        lines += ["", "## 未通过的题", "", "| 题目 | 原因 |", "|---|---|"]
        for r in failures:
            lines.append(f"| {r['id']} | {_why(r)} |")
    unsupported = [(r["id"], item) for r in runs
                   for item in (r.get("number_check") or {}).get("unsupported_items", [])]
    if unsupported:
        lines += ["", "## 找不到出处的数字（人工抽查用）", "",
                  "是否真的是编造，还是检查规则漏掉了合理的写法，需要对照 `logs/` 里的原文判断。", "",
                  "| 题目 | 数字 | 上下文 |", "|---|---|---|"]
        for qid, item in unsupported[:30]:
            ctx = str(item.get("context", "")).replace("|", "/")
            lines.append(f"| {qid} | {item.get('text')} | {ctx} |")
        if len(unsupported) > 30:
            lines.append(f"| … | 共 {len(unsupported)} 个 | |")
    if m["estimated_token_calls"]:
        lines += ["", f"> 注：{m['estimated_token_calls']} 次调用的服务商没有返回 usage，token 为估算值。"]
    return "\n".join(lines)


def _why(r: dict[str, Any]) -> str:
    if r.get("error"):
        return str(r["error"]).replace("|", "/")[:160]
    if not r.get("completed"):
        return f"未正常结束（{r.get('status')}）"
    if not r.get("chart_ok"):
        need = r.get("min_charts") or 1
        if need > 1 and r.get("charts_shown"):
            return f"只出了 {r.get('charts_shown')} 张图（要求至少 {need} 张）"
        return "没有生成图表"
    if r.get("followup_ok") is False:
        return ("第二问没答对" + ("，而且又反问了" if r.get("followup_asked") else "")
                + (f"（{r['followup_error']}）" if r.get("followup_error") else ""))
    if r.get("intent_ok") is False:
        return "没有答到用户真正想问的（模拟用户）" + ("" if r.get("asked") else "，且没有反问")
    if r.get("mentions_ok") is False:
        return "结论与标准答案不符"
    nc = r.get("number_check") or {}
    if r.get("grounded") is False:
        items = "、".join(i.get("text", "") for i in nc.get("unsupported_items", [])[:3])
        return f"结论里有 {nc.get('unsupported')} 个找不到出处的数字（{items}）"
    if r.get("clarified_first") is False:
        if not r.get("asked"):
            return "没有先反问就直接分析"
        return f"先出了 {r.get('charts_before_ask') or 0} 张图才反问"
    return r.get("status", "?")


def _delta(a: Any, b: Any, fmt: Callable[[Any], str], better: int) -> str:
    if a is None or b is None:
        return ""
    if fmt is pct:
        d, text = (b - a) * 100, f"{(b - a) * 100:+.0f} pp"
    elif fmt is cnt:
        d, text = b - a, f"{b - a:+.0f}"
    else:
        d = b - a
        text = f"{d / a * 100:+.0f}%" if a else f"{d:+.1f}"
    if abs(d) < 1e-9:
        return "="
    good = (d > 0) == (better > 0)
    return f"{text} {'✅' if better and good else '⚠️' if better else ''}".strip()


def render_compare(a_runs, b_runs, labels=("A", "B")) -> str:
    a, b = metrics(a_runs), metrics(b_runs)
    la, lb = labels
    lines = [f"# 评测对比：{la} vs {lb}\n", f"- {la}: {_header(a_runs)}", f"- {lb}: {_header(b_runs)}", "",
             f"| 指标 | {la} | {lb} | 变化 |", "|---|---|---|---|"]
    for label, key, fmt, better in ROWS:
        if a[key] is None and b[key] is None:
            continue
        lines.append(f"| {label} | {fmt(a[key])} | {fmt(b[key])} | {_delta(a[key], b[key], fmt, better)} |")
    lines += ["", "## 分类成功率", "", f"| 类别 | {la} | {lb} |", "|---|---|---|"]
    for c in sorted(set(a["by_category"]) | set(b["by_category"])):
        lines.append(f"| {CATEGORY_NAMES.get(c, c)} | {pct(a['by_category'].get(c))} | {pct(b['by_category'].get(c))} |")
    a_by, b_by = {r["id"]: r for r in a_runs}, {r["id"]: r for r in b_runs}
    flips = [(qid, a_by[qid].get("success"), b_by[qid].get("success")) for qid in a_by if qid in b_by
             and bool(a_by[qid].get("success")) != bool(b_by[qid].get("success"))]
    if flips:
        lines += ["", "## 结果不同的题", "", f"| 题目 | {la} | {lb} |", "|---|---|---|"]
        lines += [f"| {qid} | {'✓' if x else '✗'} | {'✓' if y else '✗'} |" for qid, x, y in flips]
    return "\n".join(lines)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("runs", nargs="+", help="one or two result directories (or runs.jsonl files)")
    p.add_argument("--labels", nargs=2, default=None)
    p.add_argument("--out", default="")
    p.add_argument("--recheck-numbers", action="store_true",
                   help="re-run the number-provenance check on saved runs (after changing number_check.py)")
    args = p.parse_args()
    load = lambda path: load_runs(path, recheck_numbers=args.recheck_numbers)  # noqa: E731
    if len(args.runs) == 1:
        text = render_single(load(args.runs[0]))
    elif len(args.runs) == 2:
        labels = args.labels or (Path(args.runs[0]).name, Path(args.runs[1]).name)
        text = render_compare(load(args.runs[0]), load(args.runs[1]), labels)
    else:
        p.error("give one or two result directories")
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
