"""Generate a large sales table for the big-data evaluation (deterministic).

    python eval/data/make_large.py            # default 3,000,000 rows
    python eval/data/make_large.py --rows 5_000_000

Writes ``eval/data/large_sales.parquet`` (git-ignored — it is tens of MB). The
``bigdata`` questions in questions.yaml run on it. The ground-truth answers
below are printed at the end and are stable for the default row count and seed.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
SEED = 20261019
REGIONS = ["North", "South", "East", "West", "Central"]
CATEGORIES = ["Furniture", "Technology", "Office Supplies", "Appliances", "Outdoor", "Toys"]


def make(rows: int) -> pd.DataFrame:
    rng = np.random.default_rng(SEED)
    # a deliberate signal: Technology has the highest margin, Toys the lowest,
    # so "which category is most profitable" has a stable answer.
    margin = {"Technology": 0.42, "Appliances": 0.30, "Furniture": 0.26,
              "Office Supplies": 0.20, "Outdoor": 0.18, "Toys": 0.12}
    cat = rng.choice(CATEGORIES, rows)
    price = np.round(rng.uniform(5, 500, rows), 2)
    m = np.array([margin[c] for c in cat])
    cost = np.round(price * (1 - m) * rng.uniform(0.9, 1.1, rows), 2)
    return pd.DataFrame({
        "region": rng.choice(REGIONS, rows),
        "category": cat,
        "customer_id": rng.integers(1, 80_000, rows),
        "order_date": pd.to_datetime("2021-01-01") + pd.to_timedelta(rng.integers(0, 1460, rows), unit="D"),
        "quantity": rng.integers(1, 15, rows),
        "price": price,
        "cost": cost,
    })


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--rows", type=lambda s: int(s.replace("_", "")), default=3_000_000)
    args = p.parse_args()
    df = make(args.rows)
    out = HERE / "large_sales.parquet"
    df.to_parquet(out, index=False)
    df["profit"] = df.quantity * (df.price - df.cost)
    by_cat = df.groupby("category")["profit"].sum().sort_values(ascending=False)
    by_region = df.groupby("region")["profit"].sum().sort_values(ascending=False)
    print(f"wrote {out} ({args.rows:,} rows, {out.stat().st_size/2**20:.0f} MB)")
    print("ground truth (default rows & seed):")
    print("  most profitable category:", by_cat.index[0])
    print("  region profit ranking   :", ", ".join(by_region.index))
    print("  top region              :", by_region.index[0])


if __name__ == "__main__":
    main()
