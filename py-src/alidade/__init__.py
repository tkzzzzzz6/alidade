# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

from alidade.legacy import apply_legacy_env as _apply_legacy_env

# Honour pre-rename environment variable names (see alidade.legacy).
_apply_legacy_env()


def run_app():
    """Launch the Alidade Flask application."""
    # Import app only when actually running to avoid heavy imports at package load
    from alidade.app import run_app as _run_app
    return _run_app()

__all__ = [
    "run_app",
]