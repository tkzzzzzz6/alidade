# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Large tables are summarised and read without loading them whole
(``analyst.engine.table_access`` and its use in the two table-context builders).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from alidade.analyst.engine import table_access
from alidade.agents.agent_utils import generate_data_summary
from alidade.agents.context import build_lightweight_table_context

pytestmark = [pytest.mark.backend]


class _Workspace:
    """Minimal workspace over real files; a large table must never be full-loaded."""

    def __init__(self, data_dir: Path, tables: dict[str, dict]):
        self._dir = data_dir
        self._tables = tables          # name -> {filename, file_type, row_count, large}

    def get_table_metadata(self, name):
        t = self._tables.get(name)
        if t is None:
            return None
        return SimpleNamespace(filename=t["filename"], file_type=t["file_type"],
                               row_count=t.get("row_count"))

    def get_file_path(self, filename):
        return self._dir / filename

    def get_relative_data_file_path(self, name):
        return f"data/{self._tables[name]['filename']}"

    def read_data_as_df(self, name):
        t = self._tables[name]
        if t.get("large"):
            raise AssertionError(f"read_data_as_df must not be called for large table {name!r}")
        return pd.read_parquet(self._dir / t["filename"])

    # metadata-lookup hooks used by the context builders (empty here)
    def get_metadata(self):
        return None
    user_home = None


@pytest.fixture
def big(tmp_path, monkeypatch):
    monkeypatch.setenv("ALIDADE_BIGDATA", "1")
    monkeypatch.setenv("ALIDADE_BIGDATA_ROWS", "50000")
    monkeypatch.setenv("ALIDADE_BIGDATA_SAMPLE", "2000")
    rng = np.random.default_rng(0)
    n = 300_000
    df = pd.DataFrame({
        "region": rng.choice(["North", "South", "East"], n),
        "qty": rng.integers(1, 20, n),
        "price": np.round(rng.uniform(5, 500, n), 2),
    })
    df.to_parquet(tmp_path / "big.parquet")
    small = pd.DataFrame({"region": ["A", "B"], "qty": [1, 2]})
    small.to_parquet(tmp_path / "small.parquet")
    ws = _Workspace(tmp_path, {
        "big": {"filename": "big.parquet", "file_type": "parquet", "large": True},
        "small": {"filename": "small.parquet", "file_type": "parquet", "large": False},
    })
    return ws, df


# ── the module ───────────────────────────────────────────────────────────────

def test_row_count_from_parquet_footer_without_loading(big):
    ws, df = big
    assert table_access.true_row_count(ws, "big") == len(df)     # would AssertionError on a full load

def test_is_large_respects_threshold_and_flag(big, monkeypatch):
    ws, _ = big
    assert table_access.is_large(ws, "big") is True
    assert table_access.is_large(ws, "small") is False
    monkeypatch.setenv("ALIDADE_BIGDATA", "0")
    assert table_access.is_large(ws, "big") is False            # flag off → nothing is "large"

def test_read_for_summary_samples_a_large_table(big):
    ws, df = big
    sdf, total, sampled = table_access.read_for_summary(ws, "big")
    assert total == len(df) and sampled is True
    assert len(sdf) == 2000 and list(sdf.columns) == list(df.columns)

def test_read_for_summary_loads_a_small_table_whole(big):
    ws, _ = big
    sdf, total, sampled = table_access.read_for_summary(ws, "small")
    assert sampled is False and len(sdf) == 2

def test_disabled_flag_always_loads_whole(big, monkeypatch):
    ws, df = big
    monkeypatch.setenv("ALIDADE_BIGDATA", "0")
    with pytest.raises(AssertionError):            # read_data_as_df is used again
        table_access.read_for_summary(ws, "big")

def test_numeric_stats_are_exact_over_the_whole_file(big):
    ws, df = big
    stats = table_access.numeric_stats(ws, "big", ["qty", "price"])
    assert stats["qty"]["min"] == df["qty"].min() and stats["qty"]["max"] == df["qty"].max()
    assert stats["price"]["max"] == pytest.approx(df["price"].max())
    assert stats["qty"]["mean"] == pytest.approx(df["qty"].mean())

def test_row_count_from_csv_count(tmp_path, monkeypatch):
    monkeypatch.setenv("ALIDADE_BIGDATA_ROWS", "10")
    pd.DataFrame({"x": range(100)}).to_csv(tmp_path / "t.csv", index=False)
    ws = _Workspace(tmp_path, {"t": {"filename": "t.csv", "file_type": "csv", "large": True}})
    assert table_access.true_row_count(ws, "t") == 100


# ── used by the context builders ─────────────────────────────────────────────

def test_lightweight_context_summarises_large_table_without_loading(big):
    ws, df = big
    out = build_lightweight_table_context([{"name": "big"}], ws)      # AssertionError if it full-loads
    assert "300,000 rows" in out                                     # true count, not the 2000 sample
    assert "large table" in out and "DuckDB" in out                  # the hint to aggregate in SQL
    # numeric stats are the exact whole-file values, not sample values
    assert f"max={df['qty'].max()}" in out

def test_lightweight_context_small_table_unchanged(big):
    ws, _ = big
    out = build_lightweight_table_context([{"name": "small"}], ws)
    assert "large table" not in out

def test_generate_data_summary_large_table_without_loading(big):
    ws, _ = big
    out = generate_data_summary([{"name": "big"}], workspace=ws)
    assert "300,000 rows" in out and "large table" in out
