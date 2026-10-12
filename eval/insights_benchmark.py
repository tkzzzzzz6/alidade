#!/usr/bin/env python
"""How well does the statistical insight scan find what is really there?

Builds synthetic sales tables with **planted** patterns (a trend over time, a
level shift, a group difference, a correlation, a few outliers, one dominant
group) next to **noise** columns that are related to nothing, runs
``analyst/engine/insights.py`` on each, and counts:

* recall — planted patterns that were reported;
* false findings — reported findings that involve a noise column (pure chance),
  and the share of tables with at least one.

No model is involved, so the numbers are exact for this generator.

    python eval/insights_benchmark.py --tables 50
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py-src"))

from alidade.analyst.engine import insights  # noqa: E402

PLANTED = {
    "trend": ("month", "revenue"),
    "shift": ("month", "revenue"),
    "group_difference": ("region", "margin"),
    "correlation": ("price", "units"),
    "outliers": ("order_size",),
    "concentration": ("channel",),
}
NOISE = {"noise_a", "noise_b", "noise_group"}


def make_table(seed: int, n: int = 1500, strength: float = 1.0) -> pd.DataFrame:
    """``strength`` scales every planted pattern; 0 gives a table with no pattern at all."""
    rng = np.random.default_rng(seed)
    k = strength
    months = pd.date_range("2021-01-01", periods=36, freq="MS")
    month = pd.DatetimeIndex(rng.choice(months, n))
    t = np.asarray((month - months[0]).days / 30)
    shift = np.where(np.asarray(month >= months[24]), 40.0 * k, 0.0)                 # level change in 2023
    revenue = 100 + 1.5 * k * t + shift + rng.normal(0, 30, n)
    region = rng.choice(["North", "South", "East", "West"], n)
    margin = rng.normal(0.20, 0.05, n) + k * np.select([region == "North", region == "South"], [0.04, -0.03], 0)
    price = rng.uniform(5, 50, n)
    units = np.maximum(1, 120 - 3 * k * price + rng.normal(0, 30, n)).round()
    order_size = rng.lognormal(3, 0.3, n)
    if k > 0:
        order_size[rng.choice(n, 5, replace=False)] *= 1 + 39 * k                   # a few huge orders
    top = 0.2 + 0.35 * k                                                            # one dominant channel
    channel = rng.choice(["Direct", "Partner", "Online", "Retail", "Phone"], n,
                         p=[top, *[(1 - top) / 4] * 4])
    return pd.DataFrame({
        "order_id": np.arange(n), "month": month, "region": region, "channel": channel,
        "revenue": revenue.round(2), "margin": margin.round(3), "price": price.round(2), "units": units,
        "order_size": order_size.round(2),
        "noise_a": rng.normal(50, 10, n).round(2), "noise_b": rng.exponential(5, n).round(2),
        "noise_group": rng.choice(["A", "B", "C"], n),
    })


def evaluate(findings: list[insights.Finding]) -> tuple[set[str], int]:
    """Planted kinds found, and findings that involve a noise column."""
    found = set()
    for kind, cols in PLANTED.items():
        if any(f.kind == kind and set(cols) <= set(f.columns) for f in findings):
            found.add(kind)
    false = sum(1 for f in findings if NOISE & set(f.columns))
    return found, false


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tables", type=int, default=50)
    p.add_argument("--max-findings", type=int, default=12)
    p.add_argument("--strengths", default="1,0.5,0.25,0",
                   help="pattern strengths to test; 0 = tables with no pattern at all")
    args = p.parse_args(argv)
    n = args.tables
    strengths = [float(x) for x in args.strengths.split(",")]
    print(f"# Insight scan on synthetic tables ({n} per strength; planted patterns + noise columns)\n")
    print("| strength | " + " | ".join(PLANTED) + " | recall | findings / table | false findings | tables with a false finding |")
    print("|---" * (len(PLANTED) + 5) + "|")
    for strength in strengths:
        hits = {k: 0 for k in PLANTED}
        false_total, tables_with_false, reported = 0, 0, 0
        for seed in range(n):
            result = insights.scan(make_table(seed, strength=strength), max_findings=args.max_findings)
            found, false = evaluate(result.findings)
            if strength == 0:                      # nothing is planted: every finding is false
                false = len(result.findings)
                found = set()
            for k in found:
                hits[k] += 1
            false_total += false
            tables_with_false += bool(false)
            reported += len(result.findings)
        recall = "—" if strength == 0 else f"{sum(hits.values()) / (n * len(PLANTED)):.0%}"
        cells = " | ".join("—" if strength == 0 else f"{v / n:.0%}" for v in hits.values())
        print(f"| {strength:g} | {cells} | {recall} | {reported / n:.1f} | "
              f"{false_total} ({false_total / max(1, reported):.0%}) | {tables_with_false}/{n} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
