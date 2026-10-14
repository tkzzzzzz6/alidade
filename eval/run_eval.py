#!/usr/bin/env python
"""Headless evaluation of the analyst agent against a real model.

Runs every question in ``eval/questions.yaml`` through the agent (no browser,
no Flask), each in its own subprocess with a fresh workspace, and writes one
JSON record per question to ``<out>/runs.jsonl``. ``eval/report.py`` turns that
into a summary, or compares two runs.

The same script drives the current engine (package ``alidade``) and the
pre-rewrite upstream engine (package ``data_formulator``, from a git worktree),
so both are measured the same way:

* tokens are counted by wrapping ``litellm.completion`` — the single call both
  versions make;
* the final charts of both are judged by the same rule-based chart checker
  (``alidade/analyst/engine/chart_check.py``, loaded by file path);
* questions the agent asks back are answered automatically with the first
  (recommended) option, the way a hurried user would.

Examples (PowerShell; see eval/README.md):

    python eval/run_eval.py --endpoint openai --model deepseek-chat `
        --api-base https://api.deepseek.com --api-key $env:DEEPSEEK_API_KEY `
        --out eval/results/new-deepseek

    python eval/run_eval.py --package data_formulator --src ..\\alidade-legacy\\py-src `
        --endpoint openai --model deepseek-chat --api-base https://api.deepseek.com `
        --api-key $env:DEEPSEEK_API_KEY --out eval/results/old-deepseek
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import importlib
import importlib.util
import inspect
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Callable

EVAL_DIR = Path(__file__).resolve().parent
ROOT = EVAL_DIR.parent
DEFAULT_QUESTIONS = EVAL_DIR / "questions.yaml"
DEFAULT_DEMOS = ROOT / "public" / "demos"
DEFAULT_ENV_FILE = EVAL_DIR / ".env"
CHART_CHECK_FILE = ROOT / "py-src" / "alidade" / "analyst" / "engine" / "chart_check.py"
NUMBER_CHECK_FILE = ROOT / "py-src" / "alidade" / "analyst" / "engine" / "number_check.py"
DATA_QUALITY_FILE = ROOT / "py-src" / "alidade" / "agents" / "data_quality.py"

FALLBACK_ANSWER = "按你推荐的方向继续，不用再问我。"


# ═══════════════════════════════════════════════════════════════════════════
# Questions & data
# ═══════════════════════════════════════════════════════════════════════════

def load_questions(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    import yaml
    with open(path, encoding="utf-8") as fh:
        spec = yaml.safe_load(fh)
    return spec["datasets"], spec["questions"]


def load_table(dataset: dict[str, Any], demos_dir: Path):
    """A bundled demo table (``zip`` + ``member``) or a file under eval/ (``file``).

    ``raw: true`` keeps a CSV exactly as typed — every column a string, "" and
    "N/A" left as text — the way a messy export arrives.
    """
    import pandas as pd
    if "file" in dataset:
        path = EVAL_DIR / dataset["file"]
        if path.suffix == ".parquet":
            return pd.read_parquet(path)
        if dataset.get("raw"):
            return pd.read_csv(path, dtype=str, keep_default_na=False)
        return pd.read_csv(path)
    with zipfile.ZipFile(demos_dir / dataset["zip"]) as zf:
        return pd.read_parquet(io.BytesIO(zf.read(dataset["member"])))


def dataset_keys(q: dict[str, Any]) -> list[str]:
    keys = q["dataset"]
    return list(keys) if isinstance(keys, (list, tuple)) else [keys]


# ═══════════════════════════════════════════════════════════════════════════
# Token meter — wraps litellm.completion (both engines call it)
# ═══════════════════════════════════════════════════════════════════════════

class UsageMeter:
    def __init__(self) -> None:
        self.enabled = True
        self.reset()

    def reset(self) -> None:
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.estimated_calls = 0
        self.llm_seconds = 0.0
        self.cached_tokens = 0          # prompt tokens the provider served from its prefix cache
        self.cache_reported_calls = 0   # calls whose usage said anything about caching
        self.by_model: dict[str, dict[str, int]] = {}   # model -> calls / prompt / cached / completion

    def to_dict(self) -> dict[str, Any]:
        return {
            "llm_calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "cached_prompt_tokens": self.cached_tokens if self.cache_reported_calls else None,
            "cache_reported_calls": self.cache_reported_calls,
            "estimated_calls": self.estimated_calls,
            "llm_seconds": round(self.llm_seconds, 2),
            "by_model": {k: dict(v) for k, v in self.by_model.items()},
        }

    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


METER = UsageMeter()


def _field(obj: Any, key: str) -> Any:
    if obj is None:
        return None
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def _cached_tokens(usage: Any) -> int | None:
    """Prompt tokens served from the provider's prefix cache; None if not reported.

    OpenAI-style ``prompt_tokens_details.cached_tokens`` (LiteLLM normalizes
    most providers to it), DeepSeek's ``prompt_cache_hit_tokens``, Anthropic's
    ``cache_read_input_tokens``.
    """
    for value in (_field(_field(usage, "prompt_tokens_details"), "cached_tokens"),
                  _field(usage, "prompt_cache_hit_tokens"),
                  _field(usage, "cache_read_input_tokens")):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
    return None


def _usage_numbers(usage: Any) -> tuple[int, int, int | None] | None:
    if usage is None:
        return None
    p, c = _field(usage, "prompt_tokens"), _field(usage, "completion_tokens")
    if not isinstance(p, (int, float)) and not isinstance(c, (int, float)):
        return None
    return int(p or 0), int(c or 0), _cached_tokens(usage)


def install_usage_meter() -> None:
    import litellm

    original = litellm.completion
    if getattr(original, "_alidade_eval_wrapped", False):
        return

    def _estimate(model: str, messages: Any, text: str) -> tuple[int, int, None]:
        try:
            p = litellm.token_counter(model=model, messages=messages)
        except Exception:
            p = len(json.dumps(messages, ensure_ascii=False)) // 3
        try:
            c = litellm.token_counter(model=model, text=text) if text else 0
        except Exception:
            c = len(text) // 3
        return p, c, None

    def _record(numbers: tuple[int, int, int | None] | None, model: str, messages: Any, text: str, seconds: float) -> None:
        if not METER.enabled:
            return
        METER.calls += 1
        METER.llm_seconds += seconds
        if numbers is None:
            numbers = _estimate(model, messages, text)
            METER.estimated_calls += 1
        METER.prompt_tokens += numbers[0]
        METER.completion_tokens += numbers[1]
        if numbers[2] is not None:
            METER.cached_tokens += numbers[2]
            METER.cache_reported_calls += 1
        name = str(model).split("/", 1)[-1] if str(model).startswith(("openai/", "anthropic/", "gemini/", "ollama/")) else str(model)
        slot = METER.by_model.setdefault(name, {"calls": 0, "prompt": 0, "cached": 0, "completion": 0})
        slot["calls"] += 1
        slot["prompt"] += numbers[0]
        slot["cached"] += numbers[2] or 0
        slot["completion"] += numbers[1]

    def _metered_stream(stream: Any, model: str, messages: Any, t0: float):
        usage, parts = None, []
        try:
            for chunk in stream:
                u = getattr(chunk, "usage", None)
                if u is not None and _usage_numbers(u):
                    usage = u
                try:
                    delta = chunk.choices[0].delta
                    if getattr(delta, "content", None):
                        parts.append(delta.content)
                    for tc in getattr(delta, "tool_calls", None) or []:
                        fn = getattr(tc, "function", None)
                        if fn is not None and getattr(fn, "arguments", None):
                            parts.append(fn.arguments)
                except (AttributeError, IndexError, TypeError):
                    pass
                yield chunk
        finally:
            _record(_usage_numbers(usage), model, messages, "".join(parts), time.time() - t0)

    def completion(*args: Any, **kwargs: Any):
        model = kwargs.get("model", args[0] if args else "")
        messages = kwargs.get("messages", args[1] if len(args) > 1 else [])
        if kwargs.get("stream"):
            kwargs.setdefault("stream_options", {"include_usage": True})
        t0 = time.time()
        resp = original(*args, **kwargs)
        if kwargs.get("stream"):
            return _metered_stream(resp, model, messages, t0)
        text = ""
        try:
            msg = resp.choices[0].message
            text = (msg.content or "") + "".join(
                (tc.function.arguments or "") for tc in (getattr(msg, "tool_calls", None) or []))
        except (AttributeError, IndexError, TypeError):
            pass
        _record(_usage_numbers(getattr(resp, "usage", None)), model, messages, text, time.time() - t0)
        return resp

    completion._alidade_eval_wrapped = True  # type: ignore[attr-defined]
    litellm.completion = completion


# ═══════════════════════════════════════════════════════════════════════════
# Chart judge — the current rule set, applied to both engines' final charts
# ═══════════════════════════════════════════════════════════════════════════

_CHART_CHECK = None


def chart_checker():
    global _CHART_CHECK
    if _CHART_CHECK is None:
        spec = importlib.util.spec_from_file_location("_eval_chart_check", CHART_CHECK_FILE)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = mod  # dataclasses need the module registered
        spec.loader.exec_module(mod)
        _CHART_CHECK = mod
    return _CHART_CHECK


_NUMBER_CHECK = None


def number_checker():
    """The current number-provenance check, loaded by path so it also works
    when the engine under test is the pre-change package."""
    global _NUMBER_CHECK
    if _NUMBER_CHECK is None:
        spec = importlib.util.spec_from_file_location("_eval_number_check", NUMBER_CHECK_FILE)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        _NUMBER_CHECK = mod
    return _NUMBER_CHECK


_DATA_QUALITY = None


def data_quality_notes(df: Any) -> list[str]:
    """The table notes the current engine shows the agent (loaded by path)."""
    global _DATA_QUALITY
    if _DATA_QUALITY is None:
        spec = importlib.util.spec_from_file_location("_eval_data_quality", DATA_QUALITY_FILE)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        _DATA_QUALITY = mod
    try:
        return _DATA_QUALITY.data_quality_notes(df)
    except Exception:  # noqa: BLE001
        return []


RAW_VALUE_CAP = 300_000


def table_numbers(frames: list[Any]) -> tuple[list[float], list[float]]:
    """Numbers the agent may legitimately quote from its input tables.

    Returns (cells, column statistics). Cells include numbers written inside
    text columns (``"$1,234.50"``) so a raw export counts too.
    """
    import pandas as pd
    nc = number_checker()
    cells: list[float] = []
    stats: list[float] = []
    for df in frames:
        for col in df.columns:
            s = df[col]
            if pd.api.types.is_bool_dtype(s):
                continue
            if pd.api.types.is_numeric_dtype(s):
                v = pd.to_numeric(s, errors="coerce").dropna()
                cells.extend(v.abs().tolist()[:RAW_VALUE_CAP])
                if len(v):
                    stats.extend(abs(float(x)) for x in (v.min(), v.max(), v.mean(), v.median(), v.sum()))
            elif s.dtype == object or pd.api.types.is_string_dtype(s):
                uniq = s.dropna().astype(str).unique()[:20_000]
                cells.extend(nc.numbers_in("\n".join(uniq))[:RAW_VALUE_CAP])
        stats.append(float(len(df)))
        # numbers in the data notes the agent saw ("87 rows are exact duplicates")
        stats.extend(nc.numbers_in("\n".join(data_quality_notes(df))))
    return cells[:RAW_VALUE_CAP * 2], stats


def collect_evidence(events: list[dict[str, Any]]) -> dict[str, list[str]]:
    """What the run produced: printed tool output and the data behind each chart."""
    printed = [str(e.get("stdout") or "") for e in events
               if e.get("type") == "tool_result"
               and e.get("tool") in ("execute_python_script", "inspect_source_data", "find_insights")]
    charts = []
    for e in events:
        if e.get("type") == "result" and e.get("status") == "success":
            res = (e.get("content") or {}).get("result") or {}
            rows = (res.get("content") or {}).get("rows") or []
            charts.append(json.dumps(rows[:5000], ensure_ascii=False, default=str))
    return {"printed": printed, "charts": charts}


def check_numbers(summary: str, evidence: dict[str, list[str]], raw: list[float],
                  given: list[float]) -> dict[str, Any]:
    nc = number_checker()
    printed = [v for t in evidence.get("printed", []) for v in nc.numbers_in(t)]
    charted = [v for t in evidence.get("charts", []) for v in nc.numbers_in(t)]
    ev = nc.Evidence(computed=charted, printed=printed, raw=raw, given=given)
    return nc.check(summary, ev).to_dict()


def recheck_numbers(run_dir: Path, runs: list[dict[str, Any]]) -> int:
    """Re-run the number check on saved runs (after changing number_check.py).

    Uses ``logs/<id>_<n>.evidence.json`` written during the run, plus the
    input tables re-loaded from questions.yaml. Returns how many were updated.
    """
    datasets, questions = load_questions(DEFAULT_QUESTIONS)
    by_id = {q["id"]: q for q in questions}
    demos = Path(DEFAULT_DEMOS)
    raw_cache: dict[str, tuple[list[float], list[float]]] = {}
    seen: dict[str, int] = {}
    updated = 0
    for r in runs:
        q = by_id.get(r.get("id"))
        seen[r.get("id")] = seen.get(r.get("id"), 0) + 1
        if not q:
            continue
        tag = f"{r['id']}_{seen[r['id']]}"
        files = [run_dir / "logs" / f"{tag}.evidence.json", run_dir / "logs" / f"{r['id']}.evidence.json"]
        ev_file = next((f for f in files if f.exists()), None)
        if ev_file is None:
            continue
        ev = json.loads(ev_file.read_text(encoding="utf-8"))
        key = "+".join(dataset_keys(q))
        if key not in raw_cache:
            raw_cache[key] = table_numbers([load_table(datasets[k], demos) for k in dataset_keys(q)])
        raw, stats = raw_cache[key]
        given = [*(ev.get("given") or []), *stats]
        r["number_check"] = check_numbers(ev.get("summary") or r.get("summary") or "", ev, raw, given)
        r.update(grade(q, r))
        updated += 1
    return updated


def judge_chart(result_event: dict[str, Any]) -> dict[str, Any]:
    import pandas as pd
    res = (result_event.get("content") or {}).get("result") or {}
    goal = res.get("refined_goal") or {}
    rows = (res.get("content") or {}).get("rows") or []
    chart = goal.get("chart") or {}
    info = {
        "title": goal.get("title") or (result_event.get("content") or {}).get("question", ""),
        "chart_type": chart.get("chart_type"),
        "rows": len(rows),
        "blocking": [],
        "warnings": [],
    }
    try:
        report = chart_checker().check_chart(pd.DataFrame(rows), chart)
        info["blocking"] = [i.code for i in report.blocking]
        info["warnings"] = [i.code for i in report.warnings]
    except Exception as exc:  # a chart the checker cannot read is not "broken"
        info["check_error"] = str(exc)[:200]
    return info


# ═══════════════════════════════════════════════════════════════════════════
# One question (runs inside a worker subprocess)
# ═══════════════════════════════════════════════════════════════════════════

def answer_for(interact: dict[str, Any]) -> tuple[str, list[bool]]:
    """Pick the first (recommended) option of every question."""
    answers, has_options = [], []
    for q in interact.get("questions") or []:
        opts = q.get("options") or []
        has_options.append(len(opts) >= 2)
        first = opts[0] if opts else None
        label = first.get("label") if isinstance(first, dict) else first
        answers.append(str(label) if label else FALLBACK_ANSWER)
    if not answers:
        return FALLBACK_ANSWER, has_options
    if len(answers) == 1:
        return answers[0], has_options
    return "; ".join(f"{i + 1}. {a}" for i, a in enumerate(answers)), has_options


USER_SIM_PROMPT = """你在扮演一个使用数据分析助手的用户。你心里真正想知道的是：

{intent}

助手在分析前问了你下面的问题。请像真实用户一样用一两句中文回答：
- 如果某个选项正好符合你的想法，就选它（可以直接说选项内容）；
- 如果没有合适的选项，就直接说出你想要的（可以说得具体一些，比如口径、时间段、筛选条件）；
- 只回答被问到的内容，不要替助手做分析，也不要给出答案或数字。

助手的说明：{context}

助手的问题：
{questions}

只输出你的回答。"""


def simulate_user(client: Any, intent: str, interact: dict[str, Any]) -> str:
    """Answer the agent's questions as a user with a hidden goal (same model).

    Not counted in the run's tokens: it stands in for a person.
    """
    lines = []
    for i, q in enumerate(interact.get("questions") or [], 1):
        opts = [o.get("label") if isinstance(o, dict) else o for o in q.get("options") or []]
        lines.append(f"{i}. {q.get('text', '')}" + (f"（选项：{' / '.join(str(o) for o in opts if o)}）" if opts else ""))
    prompt = USER_SIM_PROMPT.format(intent=intent, context=str(interact.get("context") or "（无）")[:800],
                                    questions="\n".join(lines) or "（无）")
    METER.enabled = False
    try:
        resp = client.get_completion([{"role": "user", "content": prompt}], stream=False)
        reply = (resp.choices[0].message.content or "").strip()
        return reply[:400] or FALLBACK_ANSWER
    except Exception:  # noqa: BLE001 — fall back to the recommended option
        return answer_for(interact)[0]
    finally:
        METER.enabled = True


def _normalize(text: str) -> str:
    # "2 月" == "2月", "Warner  Bros" == "WarnerBros": spacing is not part of the answer
    return "".join(text.lower().split())


def mentions_ok(text: str, wanted: list[str] | None) -> bool | None:
    if not wanted:
        return None
    norm = _normalize(text)
    return any(_normalize(w) in norm for w in wanted)


_NUMBER = None


def numbers_in(text: str) -> list[float]:
    import re
    global _NUMBER
    if _NUMBER is None:
        _NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
    out = []
    for m in _NUMBER.findall(text or ""):
        try:
            out.append(float(m.replace(",", "")))
        except ValueError:
            pass
    return out


def answer_ok(q: dict[str, Any], text: str) -> bool | None:
    """All answer checks of a question; None when it has none.

    * ``mentions_any``: one of these strings appears;
    * ``mentions_all``: a list of groups — every group needs one hit;
    * ``answer_number``: ``{value, tolerance}`` — some number in the text is
      within ``tolerance`` (relative, default 1%) of ``value``.
    """
    checks: list[bool] = []
    if q.get("mentions_any"):
        checks.append(bool(mentions_ok(text, q["mentions_any"])))
    for group in q.get("mentions_all") or []:
        checks.append(bool(mentions_ok(text, group)))
    num = q.get("answer_number")
    if num:
        value, tol = float(num["value"]), float(num.get("tolerance", 0.01))
        checks.append(any(abs(n - value) <= abs(value) * tol for n in numbers_in(text)))
    return all(checks) if checks else None


def grade(q: dict[str, Any], rec: dict[str, Any]) -> dict[str, Any]:
    """Decide success from a (possibly old) record. Shared with report.py."""
    text = "\n".join([rec.get("summary") or ""] + [c.get("title") or "" for c in rec.get("charts") or []])
    answer = answer_ok(q, text)
    clarified = None
    if q.get("expect_ask"):
        # must ask before drawing anything — the point is not to guess
        clarified = bool(rec.get("asked")) and rec.get("charts_before_ask", 0) == 0
    intent_ok = None
    if rec.get("user_sim") and q.get("intent"):
        # the user had a goal of their own: the answer must meet that goal
        intent_ok = answer_ok(q["intent"], text)
    grounded = None
    limit = q.get("max_unsupported")
    unsupported = (rec.get("number_check") or {}).get("unsupported")
    if limit is not None and isinstance(unsupported, int):
        # every number in the answer must come from the run (or the data)
        grounded = unsupported <= int(limit)
    if intent_ok is not None:
        # what counts is meeting the user's goal; questions that must be
        # clarified first still have to ask before charting
        success = bool(rec.get("completed")) and intent_ok is True and clarified is not False
        if rec.get("followup_ok") is not None:
            success = success and rec["followup_ok"] is True
    else:
        success = (bool(rec.get("completed")) and bool(rec.get("chart_ok", True))
                   and answer is not False and clarified is not False and grounded is not False)
        success = success and rec.get("followup_ok") is not False
    return {"mentions_ok": answer, "clarified_first": clarified, "grounded": grounded,
            "intent_ok": intent_ok, "success": success}


def make_agent(pkg: str, client: Any, workspace: Any, args: argparse.Namespace):
    agent_mod = importlib.import_module(f"{pkg}.analyst.agent")
    kwargs: dict[str, Any] = {"client": client, "workspace": workspace, "max_iterations": args.max_iterations}
    try:
        lang = importlib.import_module(f"{pkg}.agents.agent_language")
        kwargs["language_instruction"] = lang.build_language_instruction(args.language, mode="full")
    except Exception:
        pass
    return agent_mod.AnalystAgent(**kwargs)


def run_question(q: dict[str, Any], datasets: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    pkg = args.package
    client_mod = importlib.import_module(f"{pkg}.agents.client_utils")
    ws_mod = importlib.import_module(f"{pkg}.datalake.workspace")
    client = client_mod.Client(args.endpoint, args.model, args.api_key or None, args.api_base or None,
                               args.api_version or None)

    tmp = Path(tempfile.mkdtemp(prefix="alidade-eval-"))
    # per-user files (saved definitions, run traces, knowledge rules) go to a
    # fresh home, not the real ~/.alidade — the eval must not read or leave state
    os.environ["ALIDADE_HOME"] = str(tmp / "home")
    os.environ["DATA_FORMULATOR_HOME"] = str(tmp / "home")   # the legacy engine's name for it
    workspace = ws_mod.Workspace("eval-user", root_dir=tmp / "ws")
    frames = []
    for ds in datasets:
        df = load_table(ds, Path(args.demos))
        frames.append(df)
        workspace.write_parquet(df, ds["table"])

    base_kwargs: dict[str, Any] = {"input_tables": [{"name": ds["table"]} for ds in datasets]}
    run_params = inspect.signature(make_agent(pkg, client, workspace, args).run).parameters
    if "checkpoint_db_path" in run_params:
        base_kwargs["checkpoint_db_path"] = tmp / "checkpoints.sqlite"

    METER.reset()
    intent_text = (q.get("intent") or {}).get("text") if args.user_sim else None

    def converse(question: str, events: list, asks: list) -> int | None:
        """One user question, answering the agent's questions until it finishes.
        Returns the tokens spent before the agent first asked (None if it never did)."""
        tokens_before_ask = None   # spent before the user had any say in the direction
        kwargs = {**base_kwargs, "user_question": question}
        while True:
            agent = make_agent(pkg, client, workspace, args)  # a fresh request per turn
            last = None
            for ev in agent.run(**kwargs):
                events.append(ev)
                last = ev
                if ev.get("type") in ("completion", "interact"):
                    break
            if last and last.get("type") == "interact" and tokens_before_ask is None:
                tokens_before_ask = METER.total()
            if not last or last.get("type") != "interact" or len(asks) >= args.max_answers:
                return tokens_before_ask
            answer, has_options = answer_for(last)
            if intent_text:
                answer = simulate_user(client, intent_text, last)
            asks.append({"questions": [x.get("text") for x in last.get("questions") or []],
                         "has_options": has_options, "context": bool(last.get("context")),
                         "answer": answer})
            if last.get("run_id"):
                kwargs = {**base_kwargs, "user_question": answer, "resume_run_id": last["run_id"]}
            elif last.get("trajectory") is not None:
                kwargs = {**base_kwargs, "user_question": answer,
                          "trajectory": list(last["trajectory"]) + [{"role": "user", "content": answer}],
                          "completed_step_count": last.get("completed_step_count", 0)}
            else:
                return tokens_before_ask

    events: list[dict[str, Any]] = []
    asks: list[dict[str, Any]] = []
    tokens_before_ask = None
    t0 = time.time()
    error = None
    try:
        tokens_before_ask = converse(q["question"], events, asks)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
    duration = time.time() - t0
    usage = METER.to_dict()
    usage["tokens_before_ask"] = tokens_before_ask
    followup = run_followup(q, converse, error) if q.get("followup") else None

    record = score(q, events, asks, usage, duration, error, client, args)
    try:
        raw, stats = table_numbers(frames)
        nc = number_checker()
        given = [*stats, *nc.numbers_in(q["question"]), *[v for a in asks for v in nc.numbers_in(a["answer"])]]
        evidence = collect_evidence(events)
        record["number_check"] = check_numbers(record.get("summary_full") or record["summary"], evidence, raw, given)
        # written next to the log (not to runs.jsonl) so the check can be re-run later
        record["_evidence"] = {**evidence, "given": given, "summary": record.get("summary_full") or ""}
    except Exception as exc:  # noqa: BLE001 — measurement must never fail a run
        record["number_check"] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    record.pop("summary_full", None)
    if followup is not None:
        record.update(followup)
    record.update(grade(q, record))
    return record


def tier_tokens(events: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Calls and tokens per routing tier, from the run's ``llm_call`` events."""
    out: dict[str, dict[str, int]] = {}
    for e in events:
        if e.get("type") != "llm_call":
            continue
        slot = out.setdefault(str(e.get("tier") or "main"), {"calls": 0, "prompt": 0, "cached": 0, "completion": 0})
        slot["calls"] += 1
        slot["prompt"] += int(e.get("prompt_tokens") or 0)
        slot["cached"] += int(e.get("cached_tokens") or 0)
        slot["completion"] += int(e.get("completion_tokens") or 0)
    return out


def run_followup(q: dict[str, Any], converse: Callable, error: str | None) -> dict[str, Any]:
    """A second question in the same workspace and user home, after the first
    finished: does the agent remember what the user meant (``memory`` questions)?"""
    fu = q["followup"]
    if error:
        return {"followup_ok": False, "followup_error": "first question failed"}
    events: list[dict[str, Any]] = []
    asks: list[dict[str, Any]] = []
    tokens0, calls0, t0 = METER.total(), METER.calls, time.time()
    try:
        converse(fu["question"], events, asks)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        return {"followup_ok": False, "followup_error": f"{type(exc).__name__}: {exc}"[:300]}
    completion = next((e for e in reversed(events) if e.get("type") == "completion"), None)
    summary = ((completion or {}).get("content") or {}).get("summary") or ""
    titles = [((e.get("content") or {}).get("result") or {}).get("refined_goal", {}).get("title") or ""
              for e in events if e.get("type") == "result" and e.get("status") == "success"]
    return {
        "followup_question": fu["question"],
        "followup_asked": len(asks),
        "followup_asks": asks,
        "followup_tokens": METER.total() - tokens0,
        "followup_llm_calls": METER.calls - calls0,
        "followup_duration_s": round(time.time() - t0, 1),
        "followup_definitions_applied": any(e.get("type") == "definitions_applied" for e in events),
        "followup_summary": summary[:2000],
        "followup_ok": bool(completion) and completion.get("status") == "success"
                       and answer_ok(fu, "\n".join([summary, *titles])) is not False,
    }


def plan_parallel_width(events: list[dict[str, Any]]) -> int:
    """Most plan steps that could have run at the same time.

    Counts, for each ``plan`` event, the unfinished steps whose dependencies
    are all finished, and keeps the maximum. Plans without ``depends_on`` (the
    old engine, or sub-agents off) count as sequential: width 1.
    """
    width = 0
    for e in events:
        steps = e.get("steps") if e.get("type") == "plan" else None
        if not steps:
            continue
        # the model filled in the dependency fields (ids it depends on, or goals)
        dependency_aware = any(st.get("depends_on") or st.get("goal") for st in steps)
        if not dependency_aware:
            width = max(width, 1)
            continue
        status = {st.get("id"): st.get("status") for st in steps}
        open_steps = [st for st in steps if st.get("status") not in ("done", "skipped")
                      and all(status.get(d) in ("done", "skipped") for d in st.get("depends_on") or [])]
        width = max(width, len(open_steps))
    return width


def score(q, events, asks, usage, duration, error, client, args) -> dict[str, Any]:
    types = [e.get("type") for e in events]
    final = events[-1] if events else {}
    completion = next((e for e in reversed(events) if e.get("type") == "completion"), None)
    summary = ((completion or {}).get("content") or {}).get("summary") or ""
    status = (completion or {}).get("status") or ("interact" if final.get("type") == "interact" else "no_completion")

    results = [e for e in events if e.get("type") == "result" and e.get("status") == "success"]
    charts = [judge_chart(r) for r in results]
    checks = [e for e in events if e.get("type") == "chart_check"]
    compress = [e for e in events if e.get("type") == "context_compressed"]
    errors = [e.get("message") for e in events if e.get("type") == "error"]

    completed = status == "success" and error is None
    chart_ok = (len(charts) >= int(q.get("min_charts", 1))) if q.get("chart") else True
    subagents = [{k: e.get(k) for k in ("step_id", "status", "duration_s", "round", "calls", "tool_calls",
                                        "prompt_tokens", "cached_tokens", "completion_tokens")}
                 for e in events if e.get("type") == "subagent" and e.get("status") != "started"]
    first_ask = next((i for i, e in enumerate(events) if e.get("type") == "interact"), None)
    charts_before_ask = sum(1 for e in (events if first_ask is None else events[:first_ask])
                            if e.get("type") == "result" and e.get("status") == "success")
    user_sim = bool(getattr(args, "user_sim", False) and q.get("intent"))
    graded = grade(q, {"summary": summary, "charts": charts, "completed": completed, "chart_ok": chart_ok,
                       "asked": len(asks), "charts_before_ask": charts_before_ask, "user_sim": user_sim})
    mention, success = graded["mentions_ok"], graded["success"]

    record = {
        "id": q["id"], "dataset": "+".join(dataset_keys(q)), "category": q["category"],
        "engine": args.package, "model": args.model,
        "success": success, "completed": completed, "status": status,
        "chart_required": bool(q.get("chart")), "min_charts": int(q.get("min_charts", 1)), "chart_ok": chart_ok, "mentions_ok": mention,
        "error": error or (errors[-1] if errors and not completed else None),
        "duration_s": round(duration, 1), **usage,
        "actions": types.count("action"), "tool_calls": types.count("tool_start"),
        "plan_used": "plan" in types,
        "plan_updates": sum(1 for e in events if e.get("type") == "tool_start" and e.get("tool") == "update_plan"),
        "calls_dropped": sum(len(e.get("dropped") or []) for e in events if e.get("type") == "calls_dropped"),
        "plan_parallel_width": plan_parallel_width(events),
        "charts": charts, "charts_shown": len(charts),
        "charts_blocking_final": sum(1 for c in charts if c["blocking"]),
        "self_check_triggered": sum(1 for c in checks if c.get("status") == "retrying" and c.get("attempt") == 1),
        "self_check_retries": sum(1 for c in checks if c.get("status") == "retrying"),
        "self_check_fixed": sum(1 for c in checks if c.get("status") == "fixed"),
        "self_check_gave_up": sum(1 for c in checks if c.get("status") == "warning"),
        "asks": asks, "asked": len(asks),
        "charts_before_ask": charts_before_ask if asks else None,
        "expect_ask": bool(q.get("expect_ask")), "clarified_first": graded["clarified_first"],
        "user_sim": user_sim, "intent_ok": graded["intent_ok"],
        "subagents": subagents,
        "llm_tiers": dict(Counter(e.get("tier") for e in events if e.get("type") == "llm_call")),
        "by_tier": tier_tokens(events),
        "definitions_saved": sum(1 for e in events if e.get("type") == "tool_result"
                                 and e.get("tool") == "save_definition" and e.get("status") == "ok"),
        "escalations": [e.get("reason") for e in events if e.get("type") == "llm_call"
                        and str(e.get("reason") or "").startswith(("final answer", "empty final answer"))],
        "compressions": [{k: c.get(k) for k in ("tokens_before", "tokens_after", "budget", "mode")} for c in compress],
        "summary": summary[:2000],
        "summary_full": summary,
        "question": q["question"],
    }
    if args.judge:
        record["judge"] = llm_judge(q, record, client)
    return record


def llm_judge(q: dict[str, Any], record: dict[str, Any], client: Any) -> dict[str, Any]:
    """Score the final answer 1–5 against the ground-truth note (same model)."""
    prompt = (
        "你是数据分析结果的评审。根据问题、参考答案和 Agent 的输出打分（1–5 分）：\n"
        "5=结论正确且图表恰当；4=基本正确有小瑕疵；3=部分正确；2=大体错误；1=没有回答。\n"
        "只输出 JSON：{\"score\": <1-5>, \"reason\": \"<一句话>\"}\n\n"
        f"问题：{q['question']}\n参考答案：{q.get('note', '（无）')}\n"
        f"Agent 结论：{record['summary'] or '（无）'}\n"
        f"Agent 图表：{json.dumps([{k: c[k] for k in ('title', 'chart_type')} for c in record['charts']], ensure_ascii=False)}\n"
    )
    METER.enabled = False
    try:
        resp = client.get_completion([{"role": "user", "content": prompt}], stream=False)
        text = resp.choices[0].message.content or ""
        start, end = text.find("{"), text.rfind("}")
        data = json.loads(text[start:end + 1])
        return {"score": int(data.get("score", 0)), "reason": str(data.get("reason", ""))[:300]}
    except Exception as exc:
        return {"score": None, "reason": f"judge failed: {exc}"[:300]}
    finally:
        METER.enabled = True


# ═══════════════════════════════════════════════════════════════════════════
# CLI: the parent process fans out one subprocess per question
# ═══════════════════════════════════════════════════════════════════════════

def load_env_file(argv: list[str] | None = None) -> Path | None:
    """Load ``eval/.env`` (or ``--env-file``) into the environment.

    Variables already set in the environment win, so a one-off
    ``$env:EVAL_MODEL = ...`` still overrides the file.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    path = DEFAULT_ENV_FILE
    if "--env-file" in argv:
        i = argv.index("--env-file")
        if i + 1 < len(argv):
            path = Path(argv[i + 1])
    if not path.is_file():
        return None
    try:
        from dotenv import dotenv_values
        values = dotenv_values(path, encoding="utf-8-sig")
    except ImportError:  # python-dotenv is a project dependency, but stay usable without it
        values = {}
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("'\"")
    for key, value in values.items():
        if value is not None and key not in os.environ:
            os.environ[key] = value
    return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    load_env_file(argv)
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--endpoint", default=os.environ.get("EVAL_ENDPOINT", "openai"),
                   help="openai (any OpenAI-compatible API) | anthropic | azure | gemini | ollama")
    p.add_argument("--model", default=os.environ.get("EVAL_MODEL"), help="e.g. deepseek-chat, qwen-plus, gpt-4.1")
    p.add_argument("--api-base", default=os.environ.get("EVAL_API_BASE", ""))
    p.add_argument("--api-key", default=os.environ.get("EVAL_API_KEY", ""))
    p.add_argument("--api-version", default="")
    p.add_argument("--package", default="alidade", help="alidade (current) or data_formulator (legacy worktree)")
    p.add_argument("--src", default=str(ROOT / "py-src"), help="py-src directory that holds --package")
    p.add_argument("--questions", default=str(DEFAULT_QUESTIONS))
    p.add_argument("--demos", default=str(DEFAULT_DEMOS))
    p.add_argument("--only", default="", help="comma-separated question ids or categories")
    p.add_argument("--repeat", type=int, default=int(os.environ.get("EVAL_REPEAT") or 1),
                   help="run every question N times")
    p.add_argument("--jobs", type=int, default=int(os.environ.get("EVAL_JOBS") or 2),
                   help="questions run in parallel")
    p.add_argument("--timeout", type=int, default=int(os.environ.get("EVAL_TIMEOUT") or 600),
                   help="seconds per question")
    p.add_argument("--env-file", default=str(DEFAULT_ENV_FILE),
                   help="KEY=VALUE file with EVAL_API_KEY / EVAL_MODEL / ... (default: eval/.env)")
    p.add_argument("--max-iterations", type=int, default=10)
    p.add_argument("--max-answers", type=int, default=2, help="auto-answers to ask_user per question")
    p.add_argument("--language", default="zh")
    p.add_argument("--judge", action="store_true", help="also score each answer 1-5 with the same model")
    p.add_argument("--user-sim", action="store_true",
                   help="questions with an `intent`: an LLM plays the user (answers ask_user from the hidden "
                        "goal) and the answer is graded against that goal")
    p.add_argument("--strong-effort", default=os.environ.get("EVAL_STRONG_EFFORT", ""),
                   help="model routing by reasoning effort: effort for the strong tier (e.g. high; "
                        "sets ALIDADE_STRONG_EFFORT)")
    p.add_argument("--fast-effort", default=os.environ.get("EVAL_FAST_EFFORT", ""),
                   help="effort for routine steps (e.g. low; sets ALIDADE_FAST_EFFORT)")
    p.add_argument("--fast-model", default=os.environ.get("EVAL_FAST_MODEL", ""),
                   help="model routing: a cheaper model of the same provider for routine steps "
                        "(sets ALIDADE_FAST_MODEL; the --model is the strong one)")
    p.add_argument("--out", default="")
    p.add_argument("--worker", default="", help=argparse.SUPPRESS)
    p.add_argument("--worker-out", default="", help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if not args.model:
        p.error("--model is required (or set EVAL_MODEL in eval/.env)")
    if args.api_key == "sk-your-key-here":
        p.error("EVAL_API_KEY is still the placeholder from .env.example — put your real key in eval/.env")
    return args


def worker_main(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(Path(args.src).resolve()))
    install_usage_meter()
    datasets, questions = load_questions(Path(args.questions))
    q = next(x for x in questions if x["id"] == args.worker)
    record = run_question(q, [datasets[k] for k in dataset_keys(q)], args)
    evidence = record.pop("_evidence", None)
    if evidence is not None:   # kept for re-checking numbers later without re-running
        Path(args.worker_out).with_suffix(".evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, default=str), encoding="utf-8")
    Path(args.worker_out).write_text(json.dumps(record, ensure_ascii=False, default=str), encoding="utf-8")
    return 0


def _worker_cmd(args: argparse.Namespace, qid: str, out_file: Path) -> list[str]:
    cmd = [sys.executable, str(Path(__file__).resolve()), "--worker", qid, "--worker-out", str(out_file)]
    for name in ("endpoint", "model", "api_base", "api_version", "package", "src", "questions", "demos",
                 "max_iterations", "max_answers", "language"):
        value = getattr(args, name)
        if value not in ("", None):
            cmd += [f"--{name.replace('_', '-')}", str(value)]
    if args.judge:
        cmd.append("--judge")
    if getattr(args, "user_sim", False):
        cmd.append("--user-sim")
    return cmd


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # ✓/✗ and Chinese on a GBK console
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = parse_args(argv)
    if args.worker:
        return worker_main(args)

    datasets, questions = load_questions(Path(args.questions))
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        questions = [q for q in questions if q["id"] in wanted or q["category"] in wanted
                     or wanted & set(dataset_keys(q))]
    out = Path(args.out or EVAL_DIR / "results" / f"{args.package}-{args.model.replace('/', '_')}-{time.strftime('%Y%m%d-%H%M%S')}")
    out.mkdir(parents=True, exist_ok=True)
    logs = out / "logs"
    logs.mkdir(exist_ok=True)
    (out / "config.json").write_text(json.dumps({
        k: v for k, v in vars(args).items() if k not in ("api_key", "worker", "worker_out")
    } | {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "questions": len(questions)},
        ensure_ascii=False, indent=2), encoding="utf-8")

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if args.api_key:
        env["EVAL_API_KEY"] = args.api_key  # passed to workers via env, never on the command line
    if args.fast_model:
        env["ALIDADE_FAST_MODEL"] = args.fast_model
    if args.strong_effort:
        env["ALIDADE_STRONG_EFFORT"] = args.strong_effort
    if args.fast_effort:
        env["ALIDADE_FAST_EFFORT"] = args.fast_effort

    sys.path.insert(0, str(EVAL_DIR))
    report = importlib.import_module("report")

    jobs = [(q, r) for r in range(args.repeat) for q in questions]
    print(f"Running {len(jobs)} question(s) with {args.package} / {args.model} → {out}")

    def run_one(job):
        q, rep = job
        tag = q["id"] if args.repeat == 1 else f"{q['id']}#{rep + 1}"
        rec_file = logs / f"{tag.replace('#', '_')}.json"
        log_file = logs / f"{tag.replace('#', '_')}.log"
        t0 = time.time()
        try:
            with open(log_file, "w", encoding="utf-8") as log:
                subprocess.run(_worker_cmd(args, q["id"], rec_file), stdout=log, stderr=subprocess.STDOUT,
                               env=env, timeout=args.timeout, cwd=str(ROOT))
            record = json.loads(rec_file.read_text(encoding="utf-8"))
        except subprocess.TimeoutExpired:
            record = {"id": q["id"], "dataset": "+".join(dataset_keys(q)), "category": q["category"], "engine": args.package,
                      "model": args.model, "success": False, "completed": False, "status": "timeout",
                      "error": f"timeout after {args.timeout}s", "duration_s": round(time.time() - t0, 1)}
        except Exception as exc:
            record = {"id": q["id"], "dataset": "+".join(dataset_keys(q)), "category": q["category"], "engine": args.package,
                      "model": args.model, "success": False, "completed": False, "status": "crashed",
                      "error": f"worker failed: {exc} (see {log_file.name})", "duration_s": round(time.time() - t0, 1)}
        record["repeat"] = rep + 1
        return tag, record

    done = 0
    with open(out / "runs.jsonl", "w", encoding="utf-8") as sink, \
            futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        for tag, record in pool.map(run_one, jobs):
            done += 1
            sink.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            sink.flush()
            mark = "✓" if record.get("success") else "✗"
            extra = report._why(record)
            print(f"[{done}/{len(jobs)}] {mark} {tag:<32} {record.get('duration_s', 0):>6}s "
                  f"tokens={record.get('total_tokens', '-')} charts={record.get('charts_shown', '-')}"
                  + ("" if record.get("success") else f"  ({extra})"))

    text = report.render_single(report.load_runs(out))
    (out / "report.md").write_text(text, encoding="utf-8")
    print("\n" + text)
    print(f"\nWrote {out / 'runs.jsonl'} and {out / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
