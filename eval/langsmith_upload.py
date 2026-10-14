"""Upload evaluation runs to LangSmith as experiments.

The local results (``runs.jsonl``, ``report.md``) stay the record; this puts
the same runs on LangSmith so several of them can be compared side by side in
its experiment view (per question, per repeat, with the scores as columns).

* Dataset: one example per question of ``questions.yaml`` (default dataset
  ``alidade-eval``). The example's inputs are the question; its reference
  outputs are the checks the question is graded with. Examples are matched by
  question id, so every upload lands on the same examples and experiments line
  up; an example whose question changed in ``questions.yaml`` is updated.
* Experiment: one per results directory (named after it, e.g.
  ``new-sub-on4``), linked to the dataset. Each run in ``runs.jsonl`` becomes
  one run on its question's example (repeats are repetitions of the example),
  with the answer and chart titles as outputs and the grades and costs as
  feedback: ``success``, ``completed``, ``chart_ok``, ``mentions_ok``, …,
  ``duration_s``, ``llm_calls``, ``total_tokens_k``, ``output_tokens_k``,
  ``uncached_input_tokens_k`` (thousands: LangSmith scores stop at 99,999),
  ``cost`` (with ``EVAL_PRICES``), ``subagents``.
* LangSmith only takes runs that started within the last 24 hours, so an
  older results directory is moved forward in time as a whole (durations and
  gaps kept; it ends a minute before the upload). The real times are kept in
  the run metadata (``ran_at``) and on the experiment (``ran_from`` /
  ``ran_to``).
  Grades are re-checked with the current rules first, as ``report.py`` does.
* When the evaluated agent itself traced to LangSmith (``ALIDADE_LANGSMITH=1``),
  each run's metadata carries the agent's ``trace_id``.

Usage::

    python eval/langsmith_upload.py eval/results/new-sub-off3 eval/results/new-sub-on4
    python eval/run_eval.py ... --langsmith        # upload right after the run

Needs ``LANGSMITH_API_KEY`` (read from the environment, ``eval/.env`` or the
repository's ``.env``; ``LANGSMITH_ENDPOINT`` for self-hosted / EU). What is
sent: question text, dataset names, the agent's answer and chart titles,
grades and token counts. Not sent: API keys, tables, prompts.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable

EVAL_DIR = Path(__file__).resolve().parent
ROOT = EVAL_DIR.parent
if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))
DEFAULT_DATASET = "alidade-eval"
RUN_NAME = "alidade eval"

# reference checks copied from questions.yaml into the example's outputs
REFERENCE_KEYS = ("chart", "min_charts", "mentions_any", "mentions_all", "answer_number", "expect_ask",
                  "intent", "followup", "max_unsupported", "note")
# config.json fields worth keeping on the experiment
CONFIG_KEYS = ("package", "model", "endpoint", "repeat", "jobs", "only", "judge", "user_sim", "fast_model",
               "strong_effort", "fast_effort", "max_iterations", "language", "started", "questions", "alidade_env")
_SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD")


# ── what goes up (pure, tested without a network) ──────────────────────────

def _dataset_keys(q: dict[str, Any]) -> list[str]:
    d = q.get("dataset")
    return [d] if isinstance(d, str) else list(d or [])


def example_for(q: dict[str, Any]) -> dict[str, Any]:
    """The dataset example of one question: inputs, reference outputs, metadata."""
    return {
        "inputs": {"id": q["id"], "question": q.get("question", ""), "datasets": _dataset_keys(q)},
        "outputs": {k: q[k] for k in REFERENCE_KEYS if k in q},
        "metadata": {"question_id": q["id"], "category": q.get("category")},
    }


def example_from_run(r: dict[str, Any]) -> dict[str, Any]:
    """For a run whose question is no longer in questions.yaml."""
    datasets = [d for d in str(r.get("dataset") or "").split("+") if d]
    return {
        "inputs": {"id": r["id"], "question": r.get("question", ""), "datasets": datasets},
        "outputs": {},
        "metadata": {"question_id": r["id"], "category": r.get("category"), "removed_from_questions": True},
    }


def safe_config(config: dict[str, Any]) -> dict[str, Any]:
    out = {k: config[k] for k in CONFIG_KEYS if k in config and config[k] not in (None, "", [], {})}
    env = out.get("alidade_env")
    if isinstance(env, dict):
        out["alidade_env"] = {k: v for k, v in env.items() if not any(h in k.upper() for h in _SECRET_HINTS)}
    return out


# LangSmith rejects feedback scores outside this range
SCORE_LIMIT = 99_999.9999


def _k(n: Any) -> float | None:
    """Token counts in thousands (a run's total can pass the score limit)."""
    return None if n is None else round(float(n) / 1000, 2)


def _flag(value: Any) -> float | None:
    return None if value is None else (1.0 if value else 0.0)


def scores_for(r: dict[str, Any], cost: float | None = None) -> list[dict[str, Any]]:
    """Feedback for one run: the grades (1 / 0) and the costs (numbers)."""
    out: list[dict[str, Any]] = []

    def add(key: str, score: float | None, comment: str | None = None) -> None:
        if score is not None and abs(float(score)) <= SCORE_LIMIT:
            out.append({"key": key, "score": score, **({"comment": comment} if comment else {})})

    why = None
    if not r.get("success"):
        try:
            import report
            why = report._why(r)
        except Exception:  # noqa: BLE001 — a comment is optional
            why = r.get("error")
    add("success", _flag(bool(r.get("success"))), why)
    add("completed", _flag(r.get("completed")))
    if r.get("chart_required"):
        add("chart_ok", _flag(r.get("chart_ok")))
    add("mentions_ok", _flag(r.get("mentions_ok")))
    if r.get("expect_ask"):
        add("clarified_first", _flag(r.get("clarified_first")))
    if r.get("user_sim"):
        add("intent_ok", _flag(r.get("intent_ok")))
    judge = r.get("judge") or {}
    if isinstance(judge, dict) and isinstance(judge.get("score"), (int, float)):
        add("judge", float(judge["score"]), str(judge.get("reason") or "")[:500] or None)
    nc = r.get("number_check")
    if isinstance(nc, dict) and isinstance(nc.get("unsupported"), (int, float)):
        add("unsupported_numbers", float(nc["unsupported"]))

    add("duration_s", r.get("duration_s"))
    add("llm_calls", r.get("llm_calls"))
    add("total_tokens_k", _k(r.get("total_tokens")))
    add("output_tokens_k", _k(r.get("completion_tokens")))
    if r.get("cached_prompt_tokens") is not None and r.get("prompt_tokens") is not None:
        add("uncached_input_tokens_k", _k(r["prompt_tokens"] - r["cached_prompt_tokens"]))
    add("charts", r.get("charts_shown"))
    subs = r.get("subagents") or []
    add("subagents", float(len(subs)))
    add("cost", cost)
    return out


def outputs_for(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "answer": r.get("summary") or "",
        "charts": [c.get("title") or "" for c in r.get("charts") or []],
        "status": r.get("status"),
        **({"asks": r["asks"]} if r.get("asks") else {}),
        **({"followup_answer": r["followup_summary"]} if r.get("followup_summary") else {}),
    }


def _utc(ts: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)


def run_times(run_dir: Path, r: dict[str, Any], fallback_end: float) -> tuple[dt.datetime, dt.datetime]:
    """When the run happened: the worker's record file is written when it ends."""
    tag = r["id"] if not r.get("repeat") else f"{r['id']}_{r['repeat']}"
    end = fallback_end
    for name in (f"{tag}.json", f"{r['id']}.json"):
        f = run_dir / "logs" / name
        if f.is_file():
            end = f.stat().st_mtime
            break
    duration = float(r.get("duration_s") or 0)
    return _utc(end - duration), _utc(end)


def _started(config: dict[str, Any], run_dir: Path) -> float:
    try:
        return dt.datetime.strptime(config["started"], "%Y-%m-%d %H:%M:%S").timestamp()
    except (KeyError, TypeError, ValueError):
        return (run_dir / "runs.jsonl").stat().st_mtime


def build_experiment(run_dir: Path, questions: list[dict[str, Any]],
                     prices: dict[str, tuple[float, float, float]] | None = None) -> dict[str, Any]:
    """Everything one results directory uploads: examples needed, runs with times / outputs / scores."""
    import report
    config_file = run_dir / "config.json"
    config = json.loads(config_file.read_text(encoding="utf-8")) if config_file.is_file() else {}
    runs = report.load_runs(run_dir)            # re-graded with the current rules, as in report.md
    by_id = {q["id"]: q for q in questions}
    started = _started(config, run_dir)

    examples: dict[str, dict[str, Any]] = {}
    rows = []
    for i, r in enumerate(runs):
        qid = r["id"]
        if qid not in examples:
            examples[qid] = example_for(by_id[qid]) if qid in by_id else example_from_run(r)
        start, end = run_times(run_dir, r, started + float(r.get("duration_s") or 0) + i)
        cost = report.run_cost(r, prices) if prices else None
        rows.append({
            "question_id": qid,
            "inputs": examples[qid]["inputs"],
            "outputs": outputs_for(r),
            "error": None if r.get("completed") else (r.get("error") or f"status: {r.get('status')}"),
            "start": start, "end": end,
            "metadata": {k: v for k, v in {
                "repeat": r.get("repeat"), "category": r.get("category"), "model": r.get("model"),
                "engine": r.get("engine"), "trace_id": r.get("trace_id"),
            }.items() if v is not None},
            "scores": scores_for(r, cost),
        })
    starts = [row["start"] for row in rows] or [_utc(started)]
    ends = [row["end"] for row in rows] or [_utc(started)]
    repeats = max([int(r.get("repeat") or 1) for r in runs] or [1])
    return {
        "name": run_dir.name, "config": safe_config(config), "examples": examples, "rows": rows,
        "start": min(starts), "end": max(ends), "repetitions": repeats,
    }


# ── talking to LangSmith ───────────────────────────────────────────────────

def load_langsmith_env() -> None:
    """``LANGSMITH_*`` from ``eval/.env``, else the repository ``.env`` (never printed)."""
    if os.environ.get("LANGSMITH_API_KEY"):
        return
    for path in (EVAL_DIR / ".env", ROOT / ".env"):
        if not path.is_file():
            continue
        try:
            from dotenv import dotenv_values
            values = dotenv_values(path, encoding="utf-8-sig")
        except ImportError:
            values = {}
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    values[k.strip()] = v.strip().strip("'\"")
        for k, v in values.items():
            if k.startswith("LANGSMITH_") and v and k not in os.environ:
                os.environ[k] = v
        if os.environ.get("LANGSMITH_API_KEY"):
            return


def make_client():
    load_langsmith_env()
    if not os.environ.get("LANGSMITH_API_KEY"):
        raise RuntimeError("LANGSMITH_API_KEY is not set (environment, eval/.env or .env)")
    from langsmith import Client
    # no background batching: uploads are synchronous so rejections surface here
    return Client(auto_batch_tracing=False)


def sync_dataset(client: Any, name: str, examples: dict[str, dict[str, Any]]) -> tuple[Any, dict[str, Any]]:
    """Get or create the dataset; make sure each question has its example. Returns (dataset, {qid: example_id})."""
    if client.has_dataset(dataset_name=name):
        dataset = client.read_dataset(dataset_name=name)
    else:
        dataset = client.create_dataset(name, description="Alidade evaluation questions (eval/questions.yaml)")
    ids: dict[str, Any] = {}
    for ex in client.list_examples(dataset_id=dataset.id):
        qid = (ex.inputs or {}).get("id")
        if qid in examples and qid not in ids:
            ids[qid] = ex.id
            want = examples[qid]
            changed = (ex.inputs != want["inputs"]) or (want["outputs"] and (ex.outputs or {}) != want["outputs"])
            if changed and not want["metadata"].get("removed_from_questions"):
                client.update_example(ex.id, inputs=want["inputs"], outputs=want["outputs"], metadata=want["metadata"])
    missing = [qid for qid in examples if qid not in ids]
    if missing:
        new_ids = {qid: uuid.uuid4() for qid in missing}
        client.create_examples(dataset_id=dataset.id, examples=[
            {"id": new_ids[qid], **examples[qid]} for qid in missing])
        ids.update(new_ids)
    return dataset, ids


# LangSmith refuses runs that started more than 24 hours ago; stay well inside
MAX_AGE = dt.timedelta(hours=23)


def time_shift(exp: dict[str, Any], now: dt.datetime | None = None) -> dt.timedelta:
    """How far to move an old results directory forward so LangSmith takes it (0 when recent)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if exp["start"] >= now - MAX_AGE and exp["end"] <= now:
        return dt.timedelta(0)
    return (now - dt.timedelta(minutes=1)) - exp["end"]


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def upload_experiment(client: Any, exp: dict[str, Any], *, dataset_name: str = DEFAULT_DATASET,
                      name: str | None = None, replace: bool = False) -> dict[str, Any]:
    """Create the experiment on the dataset and upload its runs and feedback.

    Every call is synchronous, so a rejected run or score raises instead of
    being dropped by a background queue; failures are collected and reported.
    """
    name = name or exp["name"]
    shift = time_shift(exp)
    dataset, example_ids = sync_dataset(client, dataset_name, exp["examples"])
    if client.has_project(name):
        if not replace:
            raise RuntimeError(f"an experiment named {name!r} already exists on LangSmith "
                               "(pass --replace to overwrite it, or --name to upload under another name)")
        client.delete_project(project_name=name)
    project = client.create_project(
        name, reference_dataset_id=dataset.id, description=f"eval/results/{exp['name']}",
        metadata={**exp["config"], "source": "eval/run_eval.py",
                  **({"ran_from": _iso(exp["start"]), "ran_to": _iso(exp["end"]), "times_shifted": True} if shift else {})},
        num_examples=len(exp["examples"]), num_repetitions=exp["repetitions"],
    )
    failures: list[str] = []
    uploaded: list[tuple[uuid.UUID, dict[str, Any]]] = []
    for row in exp["rows"]:
        run_id = uuid.uuid4()
        start, end = row["start"] + shift, row["end"] + shift
        try:
            # no trace_id / dotted_order: the SDK then posts the run directly and raises on a rejection
            client.create_run(
                id=run_id, name=RUN_NAME, run_type="chain", project_name=name,
                inputs=row["inputs"], outputs=row["outputs"], error=row["error"],
                start_time=start, end_time=end,
                reference_example_id=example_ids[row["question_id"]],
                extra={"metadata": {**row["metadata"], **({"ran_at": _iso(row["start"])} if shift else {})}},
            )
            uploaded.append((run_id, {**row, "start": start}))
        except Exception as exc:  # noqa: BLE001
            failures.append(f"run {row['question_id']}#{row['metadata'].get('repeat', 1)}: {exc}")

    def send(job: tuple[uuid.UUID, dict[str, Any], dict[str, Any]]) -> str | None:
        run_id, row, s = job
        try:
            client.create_feedback(run_id, s["key"], score=s["score"], comment=s.get("comment"),
                                   session_id=project.id, start_time=row["start"])
            return None
        except Exception as exc:  # noqa: BLE001
            return f"score {s['key']} of {row['question_id']}: {exc}"

    jobs = [(run_id, row, s) for run_id, row in uploaded for s in row["scores"]]
    with ThreadPoolExecutor(max_workers=8) as pool:
        failures += [f for f in pool.map(send, jobs) if f]
    client.update_project(project.id, end_time=exp["end"] + shift)
    client.flush()
    return {"dataset": dataset, "project": project, "runs": len(uploaded), "scores": len(jobs),
            "failures": failures, "shifted": shift}


def _url(obj: Any) -> str | None:
    try:
        return getattr(obj, "url", None)
    except Exception:  # noqa: BLE001
        return None


def compare_url(dataset: Any, projects: Iterable[Any]) -> str | None:
    base = _url(dataset)
    ids = [str(p.id) for p in projects]
    if not base or len(ids) < 2:
        return None
    return f"{base}/compare?selectedSessions={','.join(ids)}"


def upload_dirs(dirs: list[Path], *, dataset_name: str = DEFAULT_DATASET, names: list[str] | None = None,
                replace: bool = False, client: Any = None, questions_path: Path | None = None) -> list[dict[str, Any]]:
    import report
    import run_eval
    _, questions = run_eval.load_questions(questions_path or run_eval.DEFAULT_QUESTIONS)
    prices = report.load_prices()
    client = client or make_client()
    done = []
    for i, d in enumerate(dirs):
        exp = build_experiment(d, questions, prices)
        name = names[i] if names and i < len(names) else None
        result = upload_experiment(client, exp, dataset_name=dataset_name, name=name, replace=replace)
        shifted = result.get("shifted")
        print(f"[langsmith] {d.name}: {result['runs']}/{len(exp['rows'])} runs, {result.get('scores', 0)} scores"
              f" → experiment {name or exp['name']!r}"
              + (f" (ran {exp['start']:%Y-%m-%d %H:%M} UTC; moved forward {shifted.days} d {shifted.seconds // 3600} h"
                 " because LangSmith only takes runs from the last 24 h — real times are in the metadata)"
                 if shifted else ""))
        for f in result.get("failures", [])[:5]:
            print(f"[langsmith]   ✗ {str(f)[:300]}", file=sys.stderr)
        if len(result.get("failures", [])) > 5:
            print(f"[langsmith]   … {len(result['failures']) - 5} more", file=sys.stderr)
        done.append(result)
    if done:
        url = compare_url(done[0]["dataset"], [r["project"] for r in done])
        print(f"[langsmith] dataset {dataset_name!r}: {_url(done[0]['dataset']) or ''}")
        if url:
            print(f"[langsmith] compare: {url}")
    failed = sum(len(r.get("failures", [])) for r in done)
    if failed:
        raise RuntimeError(f"{failed} upload(s) were rejected (see above); fix and re-run with --replace")
    return done


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(description="Upload eval results directories to LangSmith as experiments.")
    p.add_argument("dirs", nargs="+", help="results directories (each holds runs.jsonl)")
    p.add_argument("--dataset", default=os.environ.get("EVAL_LANGSMITH_DATASET", DEFAULT_DATASET))
    p.add_argument("--name", action="append", default=[], help="experiment name (one per directory; default: the directory name)")
    p.add_argument("--replace", action="store_true", help="overwrite an experiment of the same name")
    args = p.parse_args(argv)
    dirs = [Path(d) for d in args.dirs]
    for d in dirs:
        if not (d / "runs.jsonl").is_file():
            p.error(f"{d} has no runs.jsonl")
    try:
        upload_dirs(dirs, dataset_name=args.dataset, names=args.name, replace=args.replace)
    except Exception as exc:  # noqa: BLE001
        print(f"[langsmith] upload failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
