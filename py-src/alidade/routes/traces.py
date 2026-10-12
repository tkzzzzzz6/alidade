# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Run traces of the current user's analyst runs (see ``analyst/engine/trace.py``).

Routes:
  GET /api/traces?limit=50     — newest first, totals only
  GET /api/traces/<run_id>     — one run with all its recorded steps
"""

from __future__ import annotations

from flask import Blueprint, request

from alidade.analyst.engine import trace
from alidade.auth.identity import get_identity_id
from alidade.datalake.workspace import get_user_home
from alidade.error_handler import json_ok
from alidade.errors import AppError, ErrorCode

traces_bp = Blueprint("traces", __name__, url_prefix="/api/traces")


def _user_home():
    identity_id = get_identity_id()
    if not identity_id:
        raise AppError(ErrorCode.AUTH_REQUIRED, "Identity ID required")
    return get_user_home(identity_id)


@traces_bp.route("", methods=["GET"])
def list_traces():
    try:
        limit = max(1, min(int(request.args.get("limit", 50)), trace.MAX_TRACES))
    except (TypeError, ValueError):
        limit = 50
    return json_ok({"traces": trace.list_traces(_user_home(), limit)})


@traces_bp.route("/<run_id>", methods=["GET"])
def get_trace(run_id: str):
    data = trace.read_trace(_user_home(), run_id)
    if data is None:
        raise AppError(ErrorCode.INVALID_REQUEST, "No trace for this run")
    return json_ok(data)
