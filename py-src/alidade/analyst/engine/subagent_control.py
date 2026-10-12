# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Live sub-agents of running analyst runs, so the user can stop one of them.

The dispatcher in ``graph.py`` registers a :class:`StepHandle` for every plan
step it hands to a sub-agent and removes it when the step returns. The
``/api/agent/subagent-stop`` route looks the handle up by (identity, run id,
step id) and stops it; the sub-agent ends at its next model turn and the main
agent is told the user stopped that step (it is not redone).

The registry is in-process. That matches how the backend is served (one
process; runs stream from a thread), and a stop request for a run this process
does not know simply returns ``False``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

USER = "user"        # stopped from the UI
TIMEOUT = "timeout"  # ran past the time limit
RUN_ENDED = "run_ended"  # the whole run was stopped / the client went away


@dataclass
class StepHandle:
    event: threading.Event = field(default_factory=threading.Event)
    reason: str = ""

    def stop(self, reason: str) -> None:
        if not self.reason:
            self.reason = reason
        self.event.set()

    @property
    def stopped(self) -> bool:
        return self.event.is_set()


_lock = threading.Lock()
_live: dict[tuple[str, str], dict[str, StepHandle]] = {}


def _key(identity: str | None, run_id: str) -> tuple[str, str]:
    return (str(identity or ""), str(run_id or ""))


def register(identity: str | None, run_id: str, step_id: str, handle: StepHandle) -> None:
    with _lock:
        _live.setdefault(_key(identity, run_id), {})[str(step_id)] = handle


def unregister(identity: str | None, run_id: str, step_id: str) -> None:
    with _lock:
        steps = _live.get(_key(identity, run_id))
        if steps is None:
            return
        steps.pop(str(step_id), None)
        if not steps:
            _live.pop(_key(identity, run_id), None)


def stop(identity: str | None, run_id: str, step_id: str) -> bool:
    """Stop one running step. False when no such step is running."""
    with _lock:
        handle = (_live.get(_key(identity, run_id)) or {}).get(str(step_id))
    if handle is None or handle.stopped:
        return False
    handle.stop(USER)
    return True


def running(identity: str | None, run_id: str) -> list[str]:
    """Step ids of this run that are being worked on right now."""
    with _lock:
        return sorted(sid for sid, h in (_live.get(_key(identity, run_id)) or {}).items() if not h.stopped)
