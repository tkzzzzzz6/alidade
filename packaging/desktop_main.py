# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""PyInstaller entry point for the Alidade desktop build.

This is a thin launcher: it forces desktop mode (native pywebview window) and
hands off to the normal ``alidade`` app. Build it with
``pyinstaller packaging/alidade.spec`` — see docs/desktop.md.
"""

from __future__ import annotations

import os
import sys


def main() -> None:
    # Force the native-window code path (app.py reads both the flag and the env).
    os.environ.setdefault("ALIDADE_DESKTOP", "1")
    if "--desktop" not in sys.argv:
        sys.argv.append("--desktop")

    from alidade import run_app
    run_app()


if __name__ == "__main__":
    main()
