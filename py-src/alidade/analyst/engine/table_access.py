# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Reading large tables without loading them whole.

A workspace table is a file under ``data/`` (parquet / csv / …). The analyst
needs two things from it that used to force a full ``pandas`` load:

* the **summary** shown to the model — row count, schema, a few sample values
  and rows;
* the **analysis** itself — the code the model writes.

For a table of a few thousand rows, loading it into pandas is fine and nothing
here changes. For millions of rows it is slow and memory-hungry, while the
answer is almost always an aggregate whose result is tiny. So above a row
threshold (``ALIDADE_BIGDATA``, on by default; ``ALIDADE_BIGDATA_ROWS``,
default 200k):

* the summary is built from the file's metadata (row count), its schema, and a
  bounded sample read with DuckDB — not the whole table;
* the model is told the table is large and to aggregate it with DuckDB SQL over
  the file (``duckdb.sql("… FROM 'data/<file>' …")``) rather than read the whole
  thing into a DataFrame.

The summary's sample values and rows then come from the first
``ALIDADE_BIGDATA_SAMPLE`` rows (default 50k), which is enough to show what the
columns look like; the row count and the "this is a sample" note keep it honest.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_LARGE_ROWS = 200_000
DEFAULT_SAMPLE_ROWS = 50_000


def _int_env(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, "").strip() or default)
        return v if v > 0 else default
    except ValueError:
        return default


def enabled() -> bool:
    """Whether the large-table path is on (``ALIDADE_BIGDATA``, default yes)."""
    return os.environ.get("ALIDADE_BIGDATA", "1").strip().lower() not in ("0", "false", "no", "off")


def large_threshold() -> int:
    return _int_env("ALIDADE_BIGDATA_ROWS", DEFAULT_LARGE_ROWS)


def sample_rows() -> int:
    return _int_env("ALIDADE_BIGDATA_SAMPLE", DEFAULT_SAMPLE_ROWS)


# ── file location ────────────────────────────────────────────────────────────

def _file(workspace: Any, table_name: str) -> tuple[Path, str] | None:
    """(absolute path, file_type) of a table's data file, or None."""
    try:
        meta = workspace.get_table_metadata(table_name)
        if meta is None:
            return None
        path = workspace.get_file_path(meta.filename)
        if not path.exists():
            return None
        return path, (meta.file_type or path.suffix.lstrip(".")).lower()
    except Exception:  # noqa: BLE001 — a missing / odd table just falls back to pandas
        return None


def _duckdb_source(path: Path, file_type: str) -> str | None:
    """A DuckDB table function reading this file, with the path as a literal."""
    p = str(path).replace("'", "''")
    if file_type == "parquet":
        return f"read_parquet('{p}')"
    if file_type in ("csv", "txt"):
        sep = "\\t" if file_type == "txt" else ","
        return f"read_csv_auto('{p}', sep='{sep}', sample_size=-1)"
    if file_type == "json":
        return f"read_json_auto('{p}')"
    return None


# ── row count (cheap) ────────────────────────────────────────────────────────

def true_row_count(workspace: Any, table_name: str) -> int | None:
    """The table's row count without loading it: metadata, else Parquet footer,
    else a DuckDB ``COUNT(*)`` (still far cheaper than a full pandas read)."""
    try:
        meta = workspace.get_table_metadata(table_name)
        if meta is not None and getattr(meta, "row_count", None):
            return int(meta.row_count)
    except Exception:  # noqa: BLE001
        pass
    located = _file(workspace, table_name)
    if located is None:
        return None
    path, file_type = located
    if file_type == "parquet":
        try:
            import pyarrow.parquet as pq
            return pq.ParquetFile(path).metadata.num_rows
        except Exception:  # noqa: BLE001
            pass
    src = _duckdb_source(path, file_type)
    if src is None:
        return None
    try:
        import duckdb
        return int(duckdb.sql(f"SELECT COUNT(*) FROM {src}").fetchone()[0])
    except Exception:  # noqa: BLE001
        logger.info("[bigdata] COUNT(*) failed for %s", table_name, exc_info=True)
        return None


def is_large(workspace: Any, table_name: str) -> bool:
    if not enabled():
        return False
    n = true_row_count(workspace, table_name)
    return n is not None and n >= large_threshold()


# ── bounded read for the summary ─────────────────────────────────────────────

def read_for_summary(workspace: Any, table_name: str):
    """Return ``(df, total_rows, sampled)`` for building a table's summary.

    Small table: the whole frame, as before. Large table: only the first
    ``sample_rows()`` rows (read with DuckDB), plus the true total so the
    header and the "sampled" note are correct.
    """
    total = true_row_count(workspace, table_name)
    if not enabled() or total is None or total < large_threshold():
        return workspace.read_data_as_df(table_name), total, False

    located = _file(workspace, table_name)
    src = _duckdb_source(*located) if located else None
    if src is None:
        # an unsupported file type (excel, …): fall back, but cap via pandas head
        df = workspace.read_data_as_df(table_name)
        return df.head(sample_rows()), total, len(df) > sample_rows()
    try:
        import duckdb
        df = duckdb.sql(f"SELECT * FROM {src} LIMIT {sample_rows()}").df()
        return df, total, total > len(df)
    except Exception:  # noqa: BLE001
        logger.info("[bigdata] sample read failed for %s; loading fully", table_name, exc_info=True)
        df = workspace.read_data_as_df(table_name)
        return df, total, False


def sampled_note(total_rows: int, shown_rows: int) -> str:
    """One line telling the model the schema/samples came from a sample."""
    return (f"- **large table ({total_rows:,} rows)** — schema and sample values below are from the "
            f"first {shown_rows:,} rows. Aggregate it with DuckDB SQL over the file rather than loading "
            f"the whole table, e.g. `duckdb.sql(\"SELECT … FROM 'data/<file>' …\").df()`.")


def numeric_stats(workspace: Any, table_name: str, columns: list[str]) -> dict[str, dict[str, float]]:
    """Exact ``{col: {min, max, mean}}`` for numeric columns via one DuckDB
    aggregate over the file — no full pandas load. Empty on any failure (the
    caller then shows nothing rather than wrong numbers)."""
    located = _file(workspace, table_name)
    src = _duckdb_source(*located) if located else None
    if src is None or not columns:
        return {}
    try:
        import duckdb
        parts = []
        for i, c in enumerate(columns):
            q = '"' + c.replace('"', '""') + '"'
            parts += [f"min({q}) AS mn{i}", f"max({q}) AS mx{i}", f"avg({q}) AS av{i}"]
        row = duckdb.sql(f"SELECT {', '.join(parts)} FROM {src}").fetchone()
        out: dict[str, dict[str, float]] = {}
        for i, c in enumerate(columns):
            mn, mx, av = row[3 * i], row[3 * i + 1], row[3 * i + 2]
            if mn is not None:
                out[c] = {"min": mn, "max": mx, "mean": av}
        return out
    except Exception:  # noqa: BLE001 — non-numeric or odd column; skip stats
        logger.info("[bigdata] numeric_stats failed for %s", table_name, exc_info=True)
        return {}
