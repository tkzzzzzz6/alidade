# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Run traces: what one analyst run did, step by step, for the trace viewer.

Every event the run streams to the user (tool calls, actions, charts, chart
checks, sub-agents, plan changes) plus one ``llm_call`` event per model call
(model, routing tier and why, latency, tokens) is recorded with its time
offset. A run that pauses on ``ask_user`` and resumes later is one trace with
several *segments* (one per request).

Traces are per user, under ``<user home>/traces/<run_id>.json``; the newest
:data:`MAX_TRACES` are kept. Large fields (code, printed output, the final
answer) are cut to a few KB — the trace explains the run, it is not a
transcript. ``ALIDADE_TRACES=0`` turns recording off.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TRACE_DIR = "traces"
MAX_TRACES = 100
RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")

_SKIPPED = frozenset({"text_delta"})            # report streaming: thousands of tiny deltas
_LIMITS = {"code": 3000, "stdout": 2000, "error": 1000, "summary": 4000, "content": 1500}


def traces_enabled() -> bool:
    return os.environ.get("ALIDADE_TRACES", "1").strip().lower() not in ("0", "false", "no", "off")


def trace_dir(user_home: Path | str) -> Path:
    return Path(user_home) / TRACE_DIR


def _cut(value: Any, limit: int) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"… [{len(value) - limit} more characters]"
    return value


def compact(event: dict[str, Any]) -> dict[str, Any] | None:
    """The part of an event worth keeping in a trace (None = skip it)."""
    kind = event.get("type")
    if kind in _SKIPPED:
        return None
    out: dict[str, Any] = {"type": kind}
    if event.get("step_id"):
        out["step_id"] = event["step_id"]
    if kind == "result":
        res = (event.get("content") or {}).get("result") or {}
        goal = res.get("refined_goal") or {}
        content = res.get("content") or {}
        out.update({
            "status": event.get("status"),
            "title": goal.get("title") or goal.get("display_instruction"),
            "chart_type": (goal.get("chart") or {}).get("chart_type"),
            "rows": len(content.get("rows") or []),
            "code": _cut(res.get("code"), _LIMITS["code"]),
        })
        return out
    if kind == "completion":
        content = event.get("content") or {}
        out.update({"status": event.get("status"),
                    "summary": _cut(content.get("summary") or "", _LIMITS["summary"])})
        return out
    if kind == "interact":
        out["questions"] = [q.get("text") for q in event.get("questions") or [] if isinstance(q, dict)]
        return out
    if kind == "plan":
        out["steps"] = [{k: s.get(k) for k in ("id", "title", "status", "depends_on")}
                        for s in event.get("steps") or [] if isinstance(s, dict)]
        return out
    for key, value in event.items():
        if key in ("type", "step_id"):
            continue
        if isinstance(value, str):
            limit = _LIMITS.get(key, _LIMITS["content"])
            out[key] = _cut(value, limit)
        elif isinstance(value, (int, float, bool)) or value is None:
            out[key] = value
        else:
            try:
                text = json.dumps(value, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                continue
            out[key] = value if len(text) <= 4000 else _cut(text, 4000)
    return out


def summarize(segments: list[dict[str, Any]]) -> dict[str, Any]:
    """Totals over all segments of a run (for the list view and the header)."""
    calls = [e for s in segments for e in s.get("events", []) if e.get("type") == "llm_call"]
    by_model: dict[str, dict[str, int]] = {}
    for c in calls:
        slot = by_model.setdefault(str(c.get("model") or "?"),
                                   {"calls": 0, "prompt": 0, "cached": 0, "completion": 0})
        slot["calls"] += 1
        slot["prompt"] += int(c.get("prompt_tokens") or 0)
        slot["cached"] += int(c.get("cached_tokens") or 0)
        slot["completion"] += int(c.get("completion_tokens") or 0)
    tiers: dict[str, int] = {}
    for c in calls:
        tiers[str(c.get("tier") or "main")] = tiers.get(str(c.get("tier") or "main"), 0) + 1
    events = [e for s in segments for e in s.get("events", [])]
    return {
        "duration_ms": sum(int(s.get("duration_ms") or 0) for s in segments),
        "status": segments[-1].get("status") if segments else None,
        "llm_calls": len(calls),
        "llm_ms": sum(int(c.get("duration_ms") or 0) for c in calls),
        "prompt_tokens": sum(v["prompt"] for v in by_model.values()),
        "cached_tokens": sum(v["cached"] for v in by_model.values()),
        "completion_tokens": sum(v["completion"] for v in by_model.values()),
        "by_model": by_model,
        "tiers": tiers,
        "tool_calls": sum(1 for e in events if e.get("type") == "tool_start"),
        "charts": sum(1 for e in events if e.get("type") == "result" and e.get("status") == "success"),
        "chart_checks": sum(1 for e in events if e.get("type") == "chart_check" and e.get("status") == "retrying"),
        "subagents": sum(1 for e in events if e.get("type") == "subagent" and e.get("status") == "started"),
        "asks": sum(1 for e in events if e.get("type") == "interact"),
        "errors": sum(1 for e in events if e.get("type") == "error"),
    }


class RunTrace:
    """Records one request of a run; :meth:`save` merges it into the run's file."""

    def __init__(self, user_home: Path | str | None, run_id: str, *, question: str,
                 model: str = "", fast_model: str = "", resumed: bool = False):
        self.enabled = bool(user_home) and traces_enabled() and bool(RUN_ID_RE.match(run_id or ""))
        self.dir = trace_dir(user_home) if user_home else None
        self.run_id = run_id
        self.t0 = time.time()
        self._lock = threading.Lock()
        self.segment: dict[str, Any] = {
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.t0)),
            "kind": "resume" if resumed else "start",
            "input": _cut(question or "", 2000),
            "model": model,
            "fast_model": fast_model,
            "events": [],
        }

    def observe(self, event: dict[str, Any]) -> None:
        if not self.enabled or not isinstance(event, dict):
            return
        try:
            item = compact(event)
        except Exception:  # noqa: BLE001 — a trace must never break a run
            return
        if item is None:
            return
        item["t_ms"] = int((time.time() - self.t0) * 1000)
        with self._lock:
            self.segment["events"].append(item)

    def save(self, status: str) -> Path | None:
        if not self.enabled or self.dir is None:
            return None
        try:
            self.segment["status"] = status
            self.segment["duration_ms"] = int((time.time() - self.t0) * 1000)
            self.dir.mkdir(parents=True, exist_ok=True)
            path = self.dir / f"{self.run_id}.json"
            data: dict[str, Any] = {"run_id": self.run_id, "segments": []}
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    pass
            data.setdefault("segments", []).append(self.segment)
            first = data["segments"][0]
            data.update({
                "run_id": self.run_id,
                "question": first.get("input", ""),
                "started_at": first.get("started_at"),
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "model": first.get("model", ""),
                "fast_model": first.get("fast_model", ""),
                "summary": summarize(data["segments"]),
            })
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
            os.replace(tmp, path)
            prune(self.dir)
            return path
        except Exception:  # noqa: BLE001
            logger.warning("Could not save the run trace", exc_info=True)
            return None


def prune(directory: Path, keep: int = MAX_TRACES) -> None:
    files = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


def list_traces(user_home: Path | str, limit: int = 50) -> list[dict[str, Any]]:
    """Newest first: id, question, times, model(s) and totals — no events."""
    directory = trace_dir(user_home)
    if not directory.exists():
        return []
    files = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:max(1, limit)]
    out = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({k: data.get(k) for k in ("run_id", "question", "started_at", "updated_at",
                                              "model", "fast_model", "summary")}
                   | {"segments": len(data.get("segments") or [])})
    return out


def read_trace(user_home: Path | str, run_id: str) -> dict[str, Any] | None:
    if not RUN_ID_RE.match(run_id or ""):
        return None
    path = trace_dir(user_home) / f"{run_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
