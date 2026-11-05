"""Sandbox workers must be started with ``spawn`` on every platform.

Background
----------
On Linux, Python 3.11/3.12 default to the ``fork`` start method. Forking a
process that already runs other threads (LangGraph executors, the sandbox's own
memory watchdog, Flask request threads) can copy a lock that another thread
holds, so the child blocks forever and ``_await_result`` waits out the full
execution timeout. That is what hung the backend suite in CI at ~2% until the
30-minute job timeout. Windows and macOS already default to ``spawn``, which is
why the suite passed there.
"""
from __future__ import annotations

import pytest

from alidade.sandbox.local_sandbox import _WarmWorkerPool

pytestmark = [pytest.mark.backend, pytest.mark.security]


def test_warm_workers_use_spawn_start_method():
    pool = _WarmWorkerPool(size=1)
    proc, conn = pool._spawn()
    try:
        assert proc._start_method == "spawn"
    finally:
        proc.terminate()
        proc.join(timeout=5)
        conn.close()
