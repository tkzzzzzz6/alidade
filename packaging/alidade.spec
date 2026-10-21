# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the Alidade desktop build.
#
#   pyinstaller packaging/alidade.spec
#
# Prereqs (see docs/desktop.md):
#   * build the frontend first:  yarn install && yarn build
#     (it must exist at py-src/alidade/dist so it can be bundled)
#   * install build deps:        uv pip install -e ".[desktop]" pyinstaller
#
# This is a best-effort spec for a heavy dependency tree. The first build on a
# new machine or OS may still surface a missing module or data file — add it to
# `extra_hiddenimports` or `extra_datas` below rather than editing the body.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

# packaging/alidade.spec  ->  repo root is the parent of this file's dir.
ROOT = Path(SPECPATH).resolve().parent
PY_SRC = ROOT / "py-src"
FRONTEND_DIST = PY_SRC / "alidade" / "dist"

if not FRONTEND_DIST.is_dir():
    raise SystemExit(
        f"Frontend build not found at {FRONTEND_DIST}.\n"
        "Run `yarn install && yarn build` before packaging (see docs/desktop.md)."
    )

datas = []
binaries = []
hiddenimports = []


def _bundle(pkg: str) -> None:
    """collect_all a package, tolerating ones that aren't installed."""
    try:
        d, b, h = collect_all(pkg)
    except Exception as exc:  # noqa: BLE001 - packaging convenience
        print(f"[alidade.spec] skip collect_all({pkg!r}): {exc}", file=sys.stderr)
        return
    datas.extend(d)
    binaries.extend(b)
    hiddenimports.extend(h)


# Packages that ship native libraries, data files, or lazy submodules that
# PyInstaller's static analysis misses.
for pkg in (
    "webview",        # pywebview + its platform backends
    "litellm",        # model-cost JSON, tokenizers
    "duckdb",         # native engine
    "vl_convert",     # bundled Vega->image binary
    "pyarrow",        # native Arrow libs
    "sklearn",        # find_insights
    "scipy",          # sklearn dependency
    "langgraph",
    "langgraph_checkpoint",
    "langgraph_checkpoint_sqlite",
    "yfinance",
):
    _bundle(pkg)

# The alidade package's own non-Python resources (SKILL.md, tools.json, guides)
# declared in pyproject [tool.setuptools.package-data].
datas += collect_data_files("alidade", include_py_files=False)

# The built frontend the Flask server serves from alidade/dist.
datas.append((str(FRONTEND_DIST), "alidade/dist"))

# Flask extensions and connectors imported indirectly.
hiddenimports += collect_submodules("flask_limiter")
hiddenimports += [
    "flask_cors",
    "flask_session",
    "cachelib.file",
    "engineio.async_drivers.threading",
]

# Keep the artifact to a sane size: these optional data connectors are imported
# behind try/except in the data loader, so dropping them does not break startup.
# Re-include any you need in the desktop build by removing it from this list.
excludes = [
    "azure", "azure_cosmos", "azure_storage_blob", "azure_identity",
    "azure_kusto_data", "azure_keyvault_secrets",
    "boto3", "botocore",
    "google", "google_cloud_bigquery", "google_auth", "db_dtypes",
    "databricks", "databricks_sql_connector",
    "clickhouse_connect", "pymongo", "mssql_python",
    "psycopg2", "pymysql",
    "playwright",
    "pytest", "matplotlib", "tkinter",
]


a = Analysis(
    [str(ROOT / "packaging" / "desktop_main.py")],
    pathex=[str(PY_SRC)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Alidade",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # Windowed app (no console). Flip to True when debugging a build so you can
    # see the server's stdout/stderr.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Alidade",
)
