"""Compatibility with installs from before the rename to Alidade.

* Environment variables: ``DATA_FORMULATOR_*`` and ``DF_*`` are still honoured
  as fallbacks for their ``ALIDADE_*`` names (an explicit new name wins).
* Data directory: the default home moved from ``~/.data_formulator`` to
  ``~/.alidade``. The first time the new default is resolved and only the old
  directory exists, its contents are copied over (the old directory is kept as
  a backup, so nothing is lost if anything goes wrong).
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
from pathlib import Path
from typing import MutableMapping

logger = logging.getLogger(__name__)

LEGACY_ENV_PREFIXES = (("DATA_FORMULATOR_", "ALIDADE_"), ("DF_", "ALIDADE_"))
DEFAULT_HOME_NAME = ".alidade"
LEGACY_HOME_NAME = ".data_formulator"

_migrate_lock = threading.Lock()


def apply_legacy_env(environ: MutableMapping[str, str] | None = None) -> list[str]:
    """Copy legacy-named variables to their ``ALIDADE_*`` names when unset.

    Returns the new names that were filled in (for logging / tests).
    """
    env = os.environ if environ is None else environ
    filled: list[str] = []
    for key in list(env.keys()):
        for old, new in LEGACY_ENV_PREFIXES:
            if key.startswith(old):
                new_key = new + key[len(old):]
                if new_key not in env:
                    env[new_key] = env[key]
                    filled.append(new_key)
                break
    return filled


def default_home(home: Path | None = None) -> Path:
    """The default data directory (``~/.alidade``), migrating the old one once."""
    base = Path.home() if home is None else home
    new_home = base / DEFAULT_HOME_NAME
    old_home = base / LEGACY_HOME_NAME
    if new_home.exists() or not old_home.is_dir():
        return new_home
    with _migrate_lock:
        if new_home.exists():
            return new_home
        try:
            shutil.copytree(old_home, new_home, symlinks=True)
            logger.info("Copied data directory %s -> %s (the old directory was kept)", old_home, new_home)
        except Exception:  # pragma: no cover - best effort; fall back to the old data
            logger.exception("Could not copy %s to %s; using the old directory", old_home, new_home)
            shutil.rmtree(new_home, ignore_errors=True)
            return old_home
    return new_home
