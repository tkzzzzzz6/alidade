"""Keep agent tests out of the developer's real ALIDADE_HOME.

The analyst writes per-user files (run traces, saved definitions) under
``ALIDADE_HOME/users/<id>/``; point that at a temporary directory.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _tmp_alidade_home(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("ALIDADE_HOME", str(tmp_path_factory.mktemp("alidade-home")))
