# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
Flask-aware workspace factory.

Reads the workspace backend configuration from Flask's ``current_app.config``
(populated by CLI args / env vars in ``app.py``) and returns the appropriate
:class:`Workspace` subclass.  This keeps the data-layer modules
(``datalake.workspace``, ``datalake.ephemeral_workspace``) free of any
Flask dependency.

Multi-workspace support:
  - Each user has a WorkspaceManager with multiple named workspaces.
  - ``get_workspace()`` returns the active workspace (read from X-Workspace-Id header).
  - ``get_workspace_manager()`` returns the WorkspaceManager for workspace CRUD.
  - Backend is stateless: workspace ID comes from the frontend on every request.
"""

import os
import logging
from pathlib import Path

from alidade.datalake.workspace import Workspace, get_alidade_home, get_user_home
from alidade.datalake.workspace_manager import WorkspaceManager
from alidade.errors import AppError, ErrorCode

logger = logging.getLogger(__name__)


def _get_user_workspaces_root(identity_id: str) -> Path:
    """Return the workspaces root for a user: <home>/users/<safe_id>/workspaces/."""
    return get_user_home(identity_id) / "workspaces"


def _get_backend() -> str:
    """Read workspace backend from Flask config."""
    from flask import current_app
    cfg = current_app.config.get("CLI_ARGS", {})
    return cfg.get("workspace_backend", os.getenv("WORKSPACE_BACKEND", "local"))


def get_workspace_manager(identity_id: str) -> WorkspaceManager:
    """
    Return a :class:`WorkspaceManager` (or ephemeral subclass) for the given user.
    """
    from flask import current_app

    cfg = current_app.config.get("CLI_ARGS", {})
    backend = cfg.get(
        "workspace_backend", os.getenv("WORKSPACE_BACKEND", "local")
    )

    if backend == "ephemeral":
        from alidade.datalake.ephemeral_workspace import EphemeralWorkspaceManager
        return EphemeralWorkspaceManager(identity_id)

    # Default: local filesystem
    root = _get_user_workspaces_root(identity_id)
    return WorkspaceManager(root)


def get_active_workspace_id() -> str | None:
    """Read the active workspace ID from the current Flask request's X-Workspace-Id header."""
    from flask import request
    return request.headers.get("X-Workspace-Id") or None


def get_workspace(identity_id: str) -> Workspace:
    """
    Return the active :class:`Workspace` for *identity_id*.

    All backends use their configured WorkspaceManager with lazy creation.
    Ephemeral workspaces use the same local format with bounded retention.
    """
    ws_id = get_active_workspace_id()
    if not ws_id:
        raise ValueError("No active workspace. X-Workspace-Id header is required.")

    mgr = get_workspace_manager(identity_id)

    # Lazy creation: frontend generates the ID, backend creates on first use
    if not mgr.workspace_exists(ws_id):
        if getattr(mgr, "workspace_was_evicted", lambda _workspace_id: False)(ws_id):
            raise AppError(
                "WORKSPACE_EXPIRED",
                "This temporary workspace has expired.",
            )
        mgr.create_workspace(ws_id)

    return mgr.open_workspace(ws_id, identity_id)
