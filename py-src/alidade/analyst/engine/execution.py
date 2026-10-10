# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Sandbox execution for the analyst graph.

Two kinds of code run here:

* **Exploration** (``execute_python_script``) runs in ONE persistent
  :class:`~alidade.sandbox.local_sandbox.SandboxSession` for the whole
  run — variables defined in one call are visible to the next, like a notebook
  kernel. When the run pauses for the user (``ask_user``) the session's
  DataFrames and scalars are saved next to the workspace scratch area, keyed by
  ``run_id``, and restored when the run resumes.
* **Visualization** (``visualize``) always runs in a *fresh* namespace. Its code
  is signed and re-executed on its own when the user refreshes data
  (``/refresh-derived-data``), so it must be self-contained and must never
  depend on exploration variables.
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from alidade.agents.agent_utils import ensure_output_variable_in_code
from alidade.datalake.parquet_utils import df_to_safe_records

logger = logging.getLogger(__name__)

MAX_STDOUT_CHARS = 8000


def _sandbox_mode_and_limits() -> tuple[str, int]:
    try:
        from flask import current_app
        cli_args = current_app.config.get("CLI_ARGS", {})
        return cli_args.get("sandbox", "local"), cli_args.get("max_display_rows", 5000)
    except (ImportError, RuntimeError, KeyError):
        return "local", 5000


class CodeExecutor:
    """Owns the run's exploration session and runs visualization code."""

    def __init__(self, workspace: Any, run_id: str):
        self.workspace = workspace
        self.run_id = run_id
        self._session = None

    # ── session lifecycle ──────────────────────────────────────────────

    def _ns_dir(self) -> Path | None:
        try:
            return Path(self.workspace.confined_scratch.root) / "_explore_ns" / self.run_id
        except Exception:  # noqa: BLE001 — workspaces without a scratch area
            return None

    def _scratch_parent(self) -> str | None:
        try:
            return str(Path(self.workspace.confined_scratch.root).parent)
        except Exception:  # noqa: BLE001
            return None

    def open(self, restore: bool = False) -> None:
        """Start the exploration session; on resume, restore saved variables."""
        from alidade.sandbox.local_sandbox import SandboxSession

        self._session = SandboxSession()
        ns_dir = self._ns_dir()
        if restore and ns_dir is not None and ns_dir.exists():
            if SandboxSession.restore_namespace(self._session, ns_dir, self._scratch_parent()):
                logger.info("[AnalystGraph] Restored exploration namespace for run %s", self.run_id)
            shutil.rmtree(ns_dir, ignore_errors=True)

    def suspend(self) -> None:
        """Persist exploration variables (run paused) and release the worker."""
        ns_dir = self._ns_dir()
        if self._session is not None and ns_dir is not None:
            try:
                if self._session.save_namespace(ns_dir, self._scratch_parent()):
                    logger.info("[AnalystGraph] Saved exploration namespace for run %s", self.run_id)
            except Exception:  # noqa: BLE001 — best-effort
                logger.warning("[AnalystGraph] Failed to save namespace", exc_info=True)
        self.close()

    def close(self, discard_saved: bool = False) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None
        if discard_saved:
            ns_dir = self._ns_dir()
            if ns_dir is not None and ns_dir.exists():
                shutil.rmtree(ns_dir, ignore_errors=True)

    # ── exploration ────────────────────────────────────────────────────

    def run_explore_code(self, code: str) -> dict[str, Any]:
        """Run exploration code in the persistent session, capturing stdout.

        A failing call does not wipe the namespace: variables assigned before
        the error stay available, and the stdout redirect is always restored.
        """
        capture_code = (
            "import io as _io, sys as _sys\n"
            "_old_stdout = _sys.stdout\n"
            "_sys.stdout = _captured = _io.StringIO()\n"
            "try:\n"
            "    exec(compile(_df_code, '<analysis>', 'exec'), globals())\n"
            "finally:\n"
            "    _sys.stdout = _old_stdout\n"
            "_pack = {'stdout': _captured.getvalue()}\n"
        )
        stdout_on_error = (
            "_pack = {'stdout': _captured.getvalue() if '_captured' in globals() else ''}\n"
        )
        try:
            with self.workspace.local_dir() as local_path:
                workspace_path = os.path.abspath(str(local_path))
                if self._session is None:
                    self.open()
                raw = self._session.execute(
                    capture_code, {"_pack": None, "_df_code": code}, workspace_path,
                )
                partial_stdout = ""
                if raw.get("status") != "ok":
                    # Recover whatever was printed before the failure.
                    salvage = self._session.execute(stdout_on_error, {"_pack": None}, workspace_path)
                    if salvage.get("status") == "ok":
                        pack = (salvage.get("allowed_objects") or {}).get("_pack") or {}
                        partial_stdout = str(pack.get("stdout", ""))
        except Exception as e:  # noqa: BLE001
            logger.error("[AnalystGraph] Sandbox execution error", exc_info=e)
            return {"status": "error", "error": "Code execution failed", "stdout": ""}

        if raw.get("status") == "ok":
            pack = (raw.get("allowed_objects") or {}).get("_pack") or {}
            stdout = pack.get("stdout", "") if isinstance(pack, dict) else ""
            return {"status": "ok", "stdout": _truncate(str(stdout))}

        err = raw.get("error_message", raw.get("content", "Unknown error"))
        logger.warning("[AnalystGraph] explore code failed: %s\n--- code ---\n%s", err, code[:2000])
        return {"status": "error", "error": err, "stdout": _truncate(partial_stdout)}

    # ── visualization ──────────────────────────────────────────────────

    def run_visualize_code(
        self,
        code: str,
        output_variable: str,
        chart_spec: dict,
        field_metadata: dict,
        field_display_names: dict,
        display_instruction: str,
        title: str = "",
        subtitle: str = "",
        dialog: list[dict] | None = None,
    ) -> dict[str, Any]:
        """Run self-contained visualize code in a fresh sandbox and assemble
        the transform result the frontend consumes."""
        from alidade.sandbox import create_sandbox

        sandbox_mode, max_display_rows = _sandbox_mode_and_limits()

        code, was_patched, detected_var = ensure_output_variable_in_code(code, output_variable)
        if was_patched:
            logger.info("[AnalystGraph] patched output_variable: %s = %s", output_variable, detected_var)

        sandbox = create_sandbox(sandbox_mode)
        try:
            execution_result = sandbox.run_python_code(
                code=code, workspace=self.workspace, output_variable=output_variable,
            )
            if execution_result["status"] != "ok":
                error_message = execution_result.get("content", "Unknown error")
                logger.warning(
                    "[AnalystGraph] visualize code failed: %s\n--- code ---\n%s",
                    error_message, code[:2000],
                )
                return {"status": "error", "error_message": str(error_message)}

            full_df = execution_result["content"]
            row_count = len(full_df)
            chart_encodings = (chart_spec or {}).get("encodings", {}) or {}

            missing_fields = [
                f"{channel}: '{field}'"
                for channel, field in chart_encodings.items()
                if _missing_encoding(field, full_df.columns)
            ]
            if missing_fields:
                available = list(full_df.columns)
                return {
                    "status": "error",
                    "error_message": (
                        "Chart encoding fields not found in output DataFrame: "
                        f"{', '.join(missing_fields)}. Available columns: {available}"
                    ),
                    "error_code": "agent.fieldsNotFound",
                    "error_params": {
                        "missing": ", ".join(missing_fields),
                        "available": str(available),
                    },
                }

            if row_count == 0:
                return {
                    "status": "error",
                    "error_message": "Output DataFrame is empty (0 rows). Check filters or data loading.",
                    "error_code": "agent.emptyDataframe",
                }

            output_table_name = self.workspace.get_fresh_name(f"d-{output_variable}")
            self.workspace.write_parquet(full_df, output_table_name)

            query_output = full_df.head(max_display_rows) if row_count > max_display_rows else full_df
            query_output = query_output.loc[:, ~query_output.columns.duplicated()]

            refined_goal = {
                "display_instruction": display_instruction,
                "title": title,
                "subtitle": subtitle,
                "output_variable": output_variable,
                "output_fields": list(query_output.columns),
                "chart": chart_spec,
                "field_metadata": field_metadata,
                "field_display_names": field_display_names or {},
            }
            transform_result = {
                "status": "ok",
                # Backend-minted, run-stable chart id (the frontend adopts it
                # verbatim; same-run reports embed it as chart://<id>).
                "chart_id": f"chart-{uuid.uuid4().hex[:12]}",
                "code": code,
                "content": {
                    "rows": df_to_safe_records(query_output),
                    "virtual": {"table_name": output_table_name, "row_count": row_count},
                },
                "refined_goal": refined_goal,
                "dialog": dialog or [],
                "agent": "AnalystAgent",
            }
            # The full frame stays available host-side for the chart self-check.
            return {"status": "ok", "transform_result": transform_result, "dataframe": full_df}

        except Exception as e:  # noqa: BLE001
            logger.error("[AnalystGraph] Visualize execution error", exc_info=e)
            return {"status": "error", "error_message": "Visualization execution failed"}


def _missing_encoding(field: Any, columns) -> bool:
    """True when an encoding's field is not a usable column of the output.

    Weak models sometimes emit a dict (``{"field": "col"}``), a list or another
    non-string; those become a clean, repairable "not found" instead of a crash.
    """
    if not field:
        return False  # empty / None -> optional channel
    if isinstance(field, dict):
        field = field.get("field")
        if not field:
            return False
    if not isinstance(field, str):
        return True
    return field not in columns


def _truncate(stdout: str) -> str:
    if len(stdout) > MAX_STDOUT_CHARS:
        return stdout[:MAX_STDOUT_CHARS] + "\n... (truncated)"
    return stdout
