# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Per-run agent settings: what the user chose in the UI, or the server default.

The defaults come from environment variables (so the eval harness and the MCP
server behave as before); a request may override any of them with
``agent_settings`` (see ``routes/agents.py``). Values from a request are
clamped to the ranges below; env values only to a minimum (the operator's
choice, e.g. a 1 s timeout in tests). The settings are saved in the run's checkpoint, so a run that pauses
on ``ask_user`` resumes with the settings it started with.

=====================  ===========================  =========  =========
setting                env default                  default    range
=====================  ===========================  =========  =========
subagents              ALIDADE_SUBAGENTS            off        on / off
max_parallel           ALIDADE_MAX_SUBAGENTS        3          1–4
subagent_max_charts    ALIDADE_SUBAGENT_MAX_CHARTS  2          1–3
subagent_timeout_s     ALIDADE_SUBAGENT_TIMEOUT     180        30–600
plan_auto_advance      ALIDADE_PLAN_AUTO_ADVANCE    on         on / off
=====================  ===========================  =========  =========
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, replace
from typing import Any

LIMITS: dict[str, tuple[float, float]] = {
    "max_parallel": (1, 4),
    "subagent_max_charts": (1, 3),
    "subagent_timeout_s": (30, 600),
}
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    return default


def _env_seconds(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return max(1.0, value) if value == value else default


def _clamp(key: str, value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    lo, hi = LIMITS[key]
    return min(hi, max(lo, number))


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _TRUE:
            return True
        if v in _FALSE:
            return False
    return None


@dataclass(frozen=True)
class RunSettings:
    subagents: bool = False
    max_parallel: int = 3
    subagent_max_charts: int = 2
    subagent_timeout_s: float = 180.0
    plan_auto_advance: bool = True

    @classmethod
    def from_env(cls) -> "RunSettings":
        base = cls()
        return cls(
            subagents=_env_bool("ALIDADE_SUBAGENTS", False),
            max_parallel=int(_clamp("max_parallel", os.environ.get("ALIDADE_MAX_SUBAGENTS"), base.max_parallel)),
            subagent_max_charts=int(_clamp("subagent_max_charts", os.environ.get("ALIDADE_SUBAGENT_MAX_CHARTS"),
                                           base.subagent_max_charts)),
            # the operator's own value: only a floor of 1 s (the UI range is for requests)
            subagent_timeout_s=_env_seconds("ALIDADE_SUBAGENT_TIMEOUT", base.subagent_timeout_s),
            plan_auto_advance=_env_bool("ALIDADE_PLAN_AUTO_ADVANCE", True),
        )

    def merged(self, overrides: Any) -> "RunSettings":
        """These settings with ``overrides`` applied (unknown keys and bad values ignored)."""
        if not isinstance(overrides, dict):
            return self
        changes: dict[str, Any] = {}
        for key in ("subagents", "plan_auto_advance"):
            flag = _as_bool(overrides.get(key))
            if flag is not None:
                changes[key] = flag
        for key in ("max_parallel", "subagent_max_charts"):
            if overrides.get(key) is not None:
                changes[key] = int(_clamp(key, overrides[key], getattr(self, key)))
        if overrides.get("subagent_timeout_s") is not None:
            changes["subagent_timeout_s"] = _clamp("subagent_timeout_s", overrides["subagent_timeout_s"],
                                                   self.subagent_timeout_s)
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def describe() -> dict[str, Any]:
    """The server defaults and allowed ranges (for the settings UI)."""
    return {
        "defaults": RunSettings.from_env().to_dict(),
        "limits": {k: {"min": lo, "max": hi} for k, (lo, hi) in LIMITS.items()},
    }
