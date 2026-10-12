"""Statistical insight scan: finds planted patterns, stays quiet on noise."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from alidade.analyst.engine import insights

from test_analyst_graph import FakeClient, call, cp_path, run_agent, text, workspace  # noqa: F401

pytestmark = [pytest.mark.backend]


def table(seed=0, k=1.0, n=1200):
    rng = np.random.default_rng(seed)
    months = pd.date_range("2021-01-01", periods=36, freq="MS")
    month = pd.DatetimeIndex(rng.choice(months, n))
    t = np.asarray((month - months[0]).days / 30)
    region = rng.choice(["North", "South", "East", "West"], n)
    price = rng.uniform(5, 50, n)
    return pd.DataFrame({
        "order_id": np.arange(n),
        "month": month,
        "region": region,
        "revenue": 100 + 2 * k * t + rng.normal(0, 30, n),
        "margin": rng.normal(0.2, 0.05, n) + k * np.where(region == "North", 0.06, 0.0),
        "price": price,
        "units": np.maximum(1, 120 - 3 * k * price + rng.normal(0, 25, n)).round(),
        "noise": rng.normal(10, 2, n),
    })


def kinds(result):
    return {(f.kind, *sorted(f.columns)) for f in result.findings}


def test_planted_patterns_are_found_with_their_numbers():
    result = insights.scan(table())
    found = kinds(result)
    assert ("trend", "month", "revenue") in found
    assert ("group_difference", "margin", "region") in found
    assert ("correlation", "price", "units") in found
    assert not any("noise" in f.columns or "order_id" in f.columns for f in result.findings)
    group = next(f for f in result.findings if f.kind == "group_difference")
    assert "North" in group.statement and group.q_value < 0.05 and 0 < group.score <= 1
    text_ = insights.describe("orders", result)
    assert "Benjamini–Hochberg" in text_ and "Kendall tau" in text_ and "not causes" in text_


@pytest.mark.parametrize("seed", range(5))
def test_pure_noise_reports_little(seed):
    assert len(insights.scan(table(seed, k=0.0)).findings) <= 1


def test_benjamini_hochberg():
    q = insights._bh([0.01, 0.04, 0.03, 0.2])
    assert q == pytest.approx([0.04, 0.16 / 3, 0.16 / 3, 0.2])
    assert insights._bh([]) == []


def test_roles_skip_identifiers_and_find_times():
    roles = insights.column_roles(pd.DataFrame({
        "id": range(100), "when": pd.date_range("2020-01-01", periods=100).astype(str),
        "city": ["a", "b"] * 50, "value": np.linspace(0, 1, 100), "year": [2020 + i % 5 for i in range(100)],
    }))
    assert roles == {"time": ["when", "year"], "numeric": ["value"], "category": ["city"]}


def test_outliers_need_to_be_far_beyond_a_skewed_tail():
    rng = np.random.default_rng(1)
    v = rng.lognormal(3, 0.4, 2000)
    assert not [f for f in insights.scan(pd.DataFrame({"amount": v})).findings if f.kind == "outliers"]
    v[:3] *= 50
    [out] = [f for f in insights.scan(pd.DataFrame({"amount": v})).findings if f.kind == "outliers"]
    assert out.statement.startswith("3 value(s) of amount")


def test_tool_in_the_running_graph(workspace, cp_path):  # noqa: F811
    client = FakeClient([call("find_insights", {"table_names": ["sales"]}), text("Nothing big stands out.")])
    events = run_agent(client, workspace, cp_path)
    ev = next(e for e in events if e["type"] == "insights")
    assert ev["table"] == "sales" and ev["rows"] == 4
    result = next(e for e in events if e["type"] == "tool_result" and e["tool"] == "find_insights")
    assert result["status"] == "ok" and "Insight scan of sales" in result["stdout"]
    observation = [m["content"] for m in client.seen[1] if m.get("role") == "tool"][0]
    assert "Insight scan of sales" in observation


def test_one_series_per_entity_reports_who_moved_most():
    rng = np.random.default_rng(3)
    dates = pd.date_range("2025-01-01", periods=60, freq="D")
    rows = []
    for sym, drift, level in (("AAA", 0.01, 100), ("BBB", -0.004, 500), ("CCC", 0.002, 50), ("DDD", 0.0, 300)):
        price = level * np.cumprod(1 + drift + rng.normal(0, 0.005, len(dates)))
        rows += [{"symbol": sym, "date": d, "open": p * 0.99, "close": p, "volume": int(rng.integers(1e6, 5e6))}
                 for d, p in zip(dates, price)]
    df = pd.DataFrame(rows)
    roles = insights.column_roles(df)
    assert roles["numeric"] == ["close", "volume"]               # open is a near copy of close; volume is not an id
    result = insights.scan(df)
    change = next(f for f in result.findings if f.kind == "panel_change" and "close" in f.columns)
    assert "for AAA (the highest)" in change.statement and "BBB (the lowest)" in change.statement
    assert result.findings.index(change) < 3
    entity_diffs = [f for f in result.findings if f.kind == "group_difference"]
    assert all(f.score <= 0.5 for f in entity_diffs)               # "AAA costs more than CCC" is not news
