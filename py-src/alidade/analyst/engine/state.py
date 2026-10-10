# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""State carried through the analyst graph (and persisted on pause).

Everything here is plain JSON-compatible data so it checkpoints cleanly.
Live objects (LLM client, workspace, sandbox session) live on the runtime,
never in state. Nodes return *replacement* values for the keys they change;
there are no reducers, so a node that edits ``messages`` returns a new list.
"""

from __future__ import annotations

from typing import Any, TypedDict


class PendingAction(TypedDict, total=False):
    action: str            # action name (visualize / ask_user / write_report / ...)
    args: dict[str, Any]   # parsed arguments
    tool_call_id: str
    narration: str         # prose the model wrote alongside the action


class AnalystState(TypedDict, total=False):
    # ── run inputs (set once by the caller) ────────────────────────────
    run_id: str
    user_question: str
    input_tables: list[dict[str, Any]]
    focused_thread: list[dict[str, Any]] | None
    other_threads: list[dict[str, Any]] | None
    primary_tables: list[str] | None
    attached_images: list[str] | None
    scratch_files: list[str] | None
    conversation_id: str

    # ── conversation ───────────────────────────────────────────────────
    messages: list[dict[str, Any]]
    loaded_skills: list[str]
    charts: list[dict[str, Any]]           # existing + created-this-run charts
    completed_steps: list[dict[str, Any]]  # one entry per successful visualize

    # ── control ────────────────────────────────────────────────────────
    route: str                  # next hop chosen by the last routing node
    pending_tool_calls: list[dict[str, Any]]
    pending_action: PendingAction | None
    streamed_channels: dict[str, str]
    tool_rounds: int            # inspection rounds since the last action
    cycles: int                 # action cycles (bounded by a hard ceiling)
    actions_committed: int
    max_iterations: int

    # ── planning / self-check / compression (phase 2) ──────────────────
    plan: list[dict[str, Any]]  # [{"id", "title", "status"}]
    chart_retries: dict[str, int]
    compressed_upto: int        # messages[:compressed_upto] already summarised

    # ── outcome ────────────────────────────────────────────────────────
    final_text: str
    status: str                 # success | max_iterations | tool_rounds_exhausted | llm_error
    error_message: str
