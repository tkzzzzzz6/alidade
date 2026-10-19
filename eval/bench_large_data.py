"""Measure the cost of analysing a large table two ways: load the whole file
into pandas, or query it in place with DuckDB and only materialise the (small)
aggregate.

This is the primitive behind "use DuckDB for large data": the analyst's
questions are almost always aggregates (totals by group, a filtered sum, a
top-N), whose *result* is tiny even when the *input* is tens of millions of
rows. Loading the whole file into pandas pays for every row in time and
memory; DuckDB streams the file and hands back only the result.

What it reports, per table size and per query, for each engine:

* wall-clock seconds (loading the file and computing, together — that is what
  a step actually waits for),
* peak resident memory of the worker (MB),
* rows in the result (what would become model input — identical across
  engines, shown once).

Each (engine, query) runs in its own subprocess so the peak-memory number is
that run alone, not the high-water mark of the whole benchmark.

Usage::

    python eval/bench_large_data.py --rows 1_000_000 5_000_000 10_000_000
    python eval/bench_large_data.py --rows 2_000_000 --keep   # keep the data files

No model and no API key: this measures time and memory only. The token
comparison is a separate, agent-level evaluation (see eval/README.md).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ── synthetic data ──────────────────────────────────────────────────────────

REGIONS = ["North", "South", "East", "West", "Central"]
CATEGORIES = ["Furniture", "Technology", "Office Supplies", "Appliances", "Outdoor", "Toys"]


def make_data(rows: int, path_parquet: Path, path_csv: Path | None) -> None:
    """A sales-like table: region, category, customer, date, quantity, price, cost."""
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(42)
    chunk = 2_000_000
    writer = None
    import pyarrow as pa
    import pyarrow.parquet as pq
    csv_header = True
    written = 0
    while written < rows:
        n = min(chunk, rows - written)
        df = pd.DataFrame({
            "region": rng.choice(REGIONS, n),
            "category": rng.choice(CATEGORIES, n),
            "customer_id": rng.integers(1, 50_000, n),
            "order_date": pd.to_datetime("2020-01-01") + pd.to_timedelta(rng.integers(0, 2000, n), unit="D"),
            "quantity": rng.integers(1, 20, n),
            "price": np.round(rng.uniform(5, 500, n), 2),
            "cost": np.round(rng.uniform(2, 400, n), 2),
        })
        table = pa.Table.from_pandas(df, preserve_index=False)
        if writer is None:
            writer = pq.ParquetWriter(path_parquet, table.schema)
        writer.write_table(table)
        if path_csv is not None:
            df.to_csv(path_csv, mode="w" if csv_header else "a", header=csv_header, index=False)
            csv_header = False
        written += n
    if writer is not None:
        writer.close()


# ── the three analyses, each on each engine ─────────────────────────────────
# Written as source so the subprocess runs exactly one (engine, query).

QUERIES = {
    "profit_by_region": {
        "pandas": "df['profit'] = df.quantity * (df.price - df.cost)\n"
                  "res = df.groupby('region', as_index=False)['profit'].sum()",
        "sql": "SELECT region, SUM(quantity * (price - cost)) AS profit FROM {src} GROUP BY region",
    },
    "profit_by_category_in_west": {
        "pandas": "d = df[df.region == 'West'].copy()\n"
                  "d['profit'] = d.quantity * (d.price - d.cost)\n"
                  "res = d.groupby('category', as_index=False)['profit'].sum()",
        "sql": "SELECT category, SUM(quantity * (price - cost)) AS profit FROM {src} "
               "WHERE region = 'West' GROUP BY category",
    },
    "top_20_customers": {
        "pandas": "df['spend'] = df.quantity * df.price\n"
                  "res = (df.groupby('customer_id', as_index=False)['spend'].sum()"
                  ".sort_values('spend', ascending=False).head(20))",
        "sql": "SELECT customer_id, SUM(quantity * price) AS spend FROM {src} "
               "GROUP BY customer_id ORDER BY spend DESC LIMIT 20",
    },
}

# A subprocess body: load/compute one way, print {seconds, peak_mb, rows} as JSON.
WORKER = r"""
import json, sys, time
engine, query, parquet, csv = sys.argv[1:5]
t0 = time.perf_counter()
if engine == "duckdb":
    import duckdb
    sql = {sql!r}.format(src="read_parquet('%s')" % parquet)
    res = duckdb.sql(sql).df()
else:
    import pandas as pd
    src = parquet if engine == "pandas_parquet" else csv
    df = pd.read_parquet(src) if engine == "pandas_parquet" else pd.read_csv(csv, parse_dates=["order_date"])
    ns = {{"df": df}}
    exec({pandas!r}, ns)
    res = ns["res"]
seconds = time.perf_counter() - t0
def _peak_mb():
    try:                                   # Unix: exact peak from the kernel
        import resource
        kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return kb / (1024 * 1024 if sys.platform == "darwin" else 1024)
    except Exception:
        pass
    try:                                   # Windows: peak working set via psutil
        import psutil
        return psutil.Process().memory_info().peak_wset / 2**20
    except Exception:
        return float("nan")
pm = _peak_mb()
pm = round(pm) if pm == pm else None   # nan (no probe available) -> None
print(json.dumps({{"seconds": round(seconds, 2), "peak_mb": pm, "rows": int(len(res))}}))
"""


def run_one(engine: str, query: str, parquet: Path, csv: Path) -> dict:
    spec = QUERIES[query]
    body = WORKER.format(sql=spec["sql"], pandas=spec.get("pandas", ""))
    fd, script_path = tempfile.mkstemp(suffix=".py")
    os.close(fd)                 # Windows won't delete a file with an open handle
    script = Path(script_path)
    script.write_text(body, encoding="utf-8")
    try:
        out = subprocess.run(
            [sys.executable, "-I", str(script), engine, query, str(parquet), str(csv)],
            capture_output=True, text=True, timeout=1800,
        )
        if out.returncode != 0:
            return {"error": (out.stderr or out.stdout).strip().splitlines()[-1][:200]}
        return json.loads(out.stdout.strip().splitlines()[-1])
    finally:
        try:
            script.unlink(missing_ok=True)
        except OSError:
            pass                 # a lingering handle on Windows; the temp file is harmless


def human(n: int) -> str:
    return f"{n / 1_000_000:g}M" if n >= 1_000_000 else f"{n:,}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rows", type=lambda s: int(s.replace("_", "")), nargs="+",
                   default=[1_000_000, 5_000_000, 10_000_000])
    p.add_argument("--engines", nargs="+", default=["pandas_csv", "pandas_parquet", "duckdb"],
                   choices=["pandas_csv", "pandas_parquet", "duckdb"])
    p.add_argument("--no-csv", action="store_true", help="skip CSV (and the pandas_csv engine)")
    p.add_argument("--out", default="", help="write the results table (markdown) here")
    p.add_argument("--keep", action="store_true", help="keep the generated data files")
    args = p.parse_args(argv)

    engines = [e for e in args.engines if not (args.no_csv and e == "pandas_csv")]
    data_dir = Path(tempfile.mkdtemp(prefix="alidade_bench_"))
    rows_out: list[str] = []

    def emit(line: str = "") -> None:
        print(line)
        rows_out.append(line)

    emit(f"# Large-data benchmark ({os.cpu_count()} CPUs)")
    emit()
    emit("Load the whole file into pandas vs. query it in DuckDB; only the aggregate is returned.")
    emit()
    for rows in args.rows:
        parquet = data_dir / f"sales_{rows}.parquet"
        csv = data_dir / f"sales_{rows}.csv"
        make_csv = (not args.no_csv) and ("pandas_csv" in engines)
        t0 = time.perf_counter()
        make_data(rows, parquet, csv if make_csv else None)
        gen_s = time.perf_counter() - t0
        pq_mb = parquet.stat().st_size / 2**20
        csv_mb = csv.stat().st_size / 2**20 if make_csv and csv.exists() else None
        emit(f"## {human(rows)} rows  ·  parquet {pq_mb:.0f} MB"
             + (f" · csv {csv_mb:.0f} MB" if csv_mb else "") + f"  (generated in {gen_s:.0f}s)")
        emit()
        emit("| query | engine | seconds | peak MB | result rows |")
        emit("|---|---|--:|--:|--:|")
        for query in QUERIES:
            for engine in engines:
                r = run_one(engine, query, parquet, csv)
                if "error" in r:
                    emit(f"| {query} | {engine} | — | — | {r['error']} |")
                else:
                    mem = r["peak_mb"] if r.get("peak_mb") is not None else "—"
                    emit(f"| {query} | {engine} | {r['seconds']} | {mem} | {r['rows']} |")
        emit()
        if not args.keep:
            parquet.unlink(missing_ok=True)
            csv.unlink(missing_ok=True)

    if args.keep:
        emit(f"Data files kept in {data_dir}")
    if args.out:
        Path(args.out).write_text("\n".join(rows_out), encoding="utf-8")
        print(f"\nWrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
