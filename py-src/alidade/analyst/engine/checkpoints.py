# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Checkpoint storage for paused analyst runs.

A run is checkpointed only when it stops (``durability="exit"``): when it pauses
for the user (``ask_user``) the full graph state is saved, and the client resumes
it later with just the ``run_id``. Completed runs are deleted right away, and
paused runs nobody came back to are pruned after ``RETENTION_SECONDS``.

Storage: ``ALIDADE_HOME/agent-checkpoints/<identity>.sqlite`` — one
small SQLite file per user, so users never share a database and a file can be
removed without affecting anyone else.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

RETENTION_SECONDS = 7 * 24 * 3600
_PRUNE_INTERVAL_SECONDS = 3600

_last_prune: dict[str, float] = {}


def _checkpoint_dir() -> Path:
    from alidade.auth.vault import get_alidade_home
    path = get_alidade_home() / "agent-checkpoints"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_identity(identity_id: str | None) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", identity_id or "anonymous")
    return safe[:120] or "anonymous"


def db_path_for(identity_id: str | None) -> Path:
    return _checkpoint_dir() / f"{_safe_identity(identity_id)}.sqlite"


@contextmanager
def open_checkpointer(identity_id: str | None, db_path: Path | None = None) -> Iterator:
    """Yield a LangGraph ``SqliteSaver`` for this user (and keep the run index).

    The connection is opened per run and closed afterwards; concurrent runs of
    the same user are serialised by SQLite's own locking (30 s busy timeout).
    """
    from langgraph.checkpoint.sqlite import SqliteSaver

    path = db_path or db_path_for(identity_id)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS df_run_index ("
            " run_id TEXT PRIMARY KEY, updated_at REAL NOT NULL)"
        )
        conn.commit()
        saver = SqliteSaver(conn)
        saver.setup()
        _maybe_prune(saver, conn, path)
        yield saver
    finally:
        conn.close()


def touch_run(saver, run_id: str) -> None:
    conn = saver.conn
    conn.execute(
        "INSERT INTO df_run_index (run_id, updated_at) VALUES (?, ?) "
        "ON CONFLICT(run_id) DO UPDATE SET updated_at = excluded.updated_at",
        (run_id, time.time()),
    )
    conn.commit()


def forget_run(saver, run_id: str) -> None:
    """Drop a finished run's checkpoints."""
    try:
        saver.delete_thread(run_id)
        saver.conn.execute("DELETE FROM df_run_index WHERE run_id = ?", (run_id,))
        saver.conn.commit()
    except Exception:  # noqa: BLE001 — cleanup is best-effort
        logger.warning("[AnalystGraph] Failed to delete checkpoints for %s", run_id, exc_info=True)


def has_run(saver, run_id: str) -> bool:
    try:
        return saver.get_tuple({"configurable": {"thread_id": run_id}}) is not None
    except Exception:  # noqa: BLE001
        return False


def _maybe_prune(saver, conn: sqlite3.Connection, path: Path) -> None:
    now = time.time()
    if now - _last_prune.get(str(path), 0) < _PRUNE_INTERVAL_SECONDS:
        return
    _last_prune[str(path)] = now
    try:
        stale = [
            row[0] for row in conn.execute(
                "SELECT run_id FROM df_run_index WHERE updated_at < ?",
                (now - RETENTION_SECONDS,),
            )
        ]
        for run_id in stale:
            forget_run(saver, run_id)
        if stale:
            conn.execute("VACUUM")
            logger.info("[AnalystGraph] Pruned %d expired paused run(s) from %s", len(stale), path.name)
    except Exception:  # noqa: BLE001
        logger.warning("[AnalystGraph] Checkpoint pruning failed", exc_info=True)
