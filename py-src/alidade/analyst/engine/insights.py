# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Statistical insight scan: what stands out in a table, without an LLM.

Used when the user asks "what's interesting here?" (the ``find_insights``
tool) and by the MCP server. Every candidate finding is a statistical test or
a descriptive check:

* **trend** — a numeric column over time (Kendall's tau on the per-period
  mean; the period is chosen so there are 8–200 points);
* **shift** — the single largest level change in that series (Welch's t-test,
  Bonferroni-corrected for the positions tried);
* **group difference** — a numeric column across the groups of a category with
  2–30 groups (Kruskal–Wallis, effect size epsilon²);
* **correlation** — two numeric columns (Spearman's rho);
* **outliers** — values more than 5 robust standard deviations (MAD) from the
  median (descriptive, no p-value);
* **concentration** — one group holds a large share of a total (descriptive).

All p-values of a table go through the Benjamini–Hochberg procedure together,
so at most about 5% of the reported tested findings are expected to be chance
(the false-discovery rate). Findings must also be large enough to matter
(minimum effect sizes below), and are ranked by effect size. The numbers in
each finding are what the agent quotes, so they are printed with the finding.

scipy comes with scikit-learn, a direct dependency.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pandas as pd

MAX_ROWS = 200_000          # sample above this (rows chosen at random, fixed seed)
MAX_NUMERIC = 12            # numeric columns considered
MAX_CATEGORIES = 8          # category columns considered
MAX_GROUPS = 30
MIN_GROUP_ROWS = 5
FDR = 0.05

MIN_TAU = 0.3               # |Kendall tau| for a trend
MIN_RHO = 0.4               # |Spearman rho| for a correlation
MAX_RHO = 0.98              # above this two columns are near copies: not an insight
MIN_EPS2 = 0.06             # epsilon² for a group difference (≈ a medium effect)
MIN_SHIFT_D = 0.5           # level shift, in row-level standard deviations
MIN_DRIFT = 0.2             # a trend must move the level by this many row-level standard deviations
OUTLIER_Z = 5.0
MIN_SHARE = 0.4             # one group's share of a total


@dataclass
class Finding:
    kind: str
    columns: list[str]
    statement: str                       # one sentence with the numbers
    effect: float                        # kind-specific effect size (|tau|, |rho|, eps², d, z, share)
    p_value: float | None = None
    q_value: float | None = None         # after Benjamini–Hochberg (tested kinds only)
    score: float = 0.0                   # 0–1, for ranking across kinds
    method: str = ""
    chart: dict[str, Any] = field(default_factory=dict)   # suggested chart: type, x, y, color, aggregate

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("effect", "p_value", "q_value", "score"):
            if isinstance(d[k], float):
                d[k] = float(f"{d[k]:.4g}")
        return d


# ── column roles ──────────────────────────────────────────────────────────

_ID_NAME = re.compile(r"(^|_)(id|uuid|key|code|zip|phone)$|^index$", re.IGNORECASE)


def _as_datetime(s: pd.Series) -> pd.Series | None:
    if pd.api.types.is_datetime64_any_dtype(s):
        return s
    if s.dtype == object or pd.api.types.is_string_dtype(s):
        sample = s.dropna().astype(str).head(200)
        if sample.empty or sample.str.contains(r"\d{4}|\d{1,2}[/-]\d{1,2}|[A-Za-z]{3} \d", regex=True).mean() < 0.8:
            return None
        try:
            parsed = pd.to_datetime(s, errors="coerce", format="mixed")
        except (TypeError, ValueError):
            return None
        if parsed.notna().mean() >= 0.9 and parsed.dt.year.between(1800, 2200).mean() > 0.9:
            return parsed
    return None


def _counter_like(s: pd.Series) -> bool:
    """1, 2, 3, ... — a row number, not a measure."""
    if not pd.api.types.is_integer_dtype(s) or len(s) < 50:
        return False
    v = s.dropna()
    return v.is_unique and (v.max() - v.min() + 1) <= 1.1 * len(v)


_PREFERRED = re.compile(r"close|total|value|amount|price|sales|revenue", re.IGNORECASE)


def _drop_near_copies(df: pd.DataFrame, cols: list[str]) -> list[str]:
    """Keep one of each group of near-identical numeric columns (open/high/low/close)."""
    if len(cols) < 2:
        return cols
    sample = df[cols].apply(pd.to_numeric, errors="coerce").head(5000)
    corr = sample.corr(method="spearman").abs()
    groups: list[list[str]] = []
    for c in cols:
        for g in groups:
            if corr.loc[c, g[0]] > MAX_RHO:
                g.append(c)
                break
        else:
            groups.append([c])
    return [next((c for c in g if _PREFERRED.search(str(c))), g[0]) for g in groups]


def column_roles(df: pd.DataFrame) -> dict[str, list[str]]:
    """Which columns are times, measures and categories."""
    times, numeric, categories = [], [], []
    n = len(df)
    for col in df.columns:
        s = df[col]
        if pd.api.types.is_bool_dtype(s):
            continue
        if pd.api.types.is_numeric_dtype(s):
            nunique = s.nunique(dropna=True)
            name = str(col)
            if _ID_NAME.search(name) or _counter_like(s):
                continue                                    # identifiers
            if name.lower() in ("month", "day", "week", "quarter", "weekday", "hour"):
                continue                                    # positions in a cycle, not a time axis
            if name.lower() == "year" or (
                    pd.api.types.is_integer_dtype(s) and s.dropna().between(1800, 2200).all() and nunique <= 300
                    and "year" in name.lower()):
                times.append(col)
                continue
            if nunique >= 3:
                numeric.append(col)
            continue
        if _as_datetime(s) is not None:
            times.append(col)
            continue
        nunique = s.nunique(dropna=True)
        if 2 <= nunique <= MAX_GROUPS and nunique < n:
            categories.append(col)
    numeric = _drop_near_copies(df, numeric[:MAX_NUMERIC * 2])
    return {"time": times, "numeric": numeric[:MAX_NUMERIC], "category": categories[:MAX_CATEGORIES]}


def panel_of(df: pd.DataFrame, roles: dict[str, list[str]]) -> tuple[str, str] | None:
    """(entity column, time column) when the table is one series per entity
    (stock prices per symbol, emissions per country): comparisons across those
    entities are mostly obvious, and averages over them mix unlike things."""
    for cat in roles["category"]:
        for tcol in roles["time"]:
            groups = df[cat].nunique(dropna=True)
            if groups < 3:
                continue
            dup = df.duplicated([cat, tcol]).mean()
            per_group = df.groupby(cat)[tcol].nunique()
            if dup < 0.05 and per_group.min() >= 8:
                return cat, tcol
    return None


def _label_column(df: pd.DataFrame, exclude: list[str]) -> str | None:
    """A text column that names rows (for outlier examples)."""
    for col in df.columns:
        if col in exclude:
            continue
        s = df[col]
        if (s.dtype == object or pd.api.types.is_string_dtype(s)) and s.nunique(dropna=True) > 0.5 * len(s):
            return col
    return None


# ── statistics helpers ────────────────────────────────────────────────────

def _bh(pvalues: list[float]) -> list[float]:
    """Benjamini–Hochberg adjusted p-values (q-values), same order."""
    m = len(pvalues)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvalues[i])
    q = [0.0] * m
    prev = 1.0
    for rank, i in reversed(list(enumerate(order, start=1))):
        prev = min(prev, pvalues[i] * m / rank)
        q[i] = prev
    return q


def _fmt(v: float) -> str:
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "n/a"
    a = abs(v)
    if a >= 1000:
        return f"{v:,.0f}"
    if a >= 10:
        return f"{v:.1f}"
    if a >= 0.01:
        return f"{v:.3g}"
    return f"{v:.2g}"


def _p(p: float | None) -> str:
    if p is None:
        return ""
    return "p<0.001" if p < 0.001 else f"p={p:.3f}"


def _period_series(times: pd.Series, values: pd.Series) -> tuple[pd.Series, str] | None:
    """Mean per period with 8–200 points: year, quarter, month, week or day."""
    t = pd.to_datetime(times, errors="coerce") if not pd.api.types.is_numeric_dtype(times) else None
    frame = pd.DataFrame({"v": pd.to_numeric(values, errors="coerce")})
    if t is None:                       # a year column
        frame["p"] = pd.to_numeric(times, errors="coerce")
        s = frame.dropna().groupby("p")["v"].mean()
        return (s, "year") if 8 <= len(s) <= 300 else None
    frame["t"] = t
    frame = frame.dropna()
    if frame.empty:
        return None
    for freq, name in (("YS", "year"), ("QS", "quarter"), ("MS", "month"), ("W", "week"), ("D", "day")):
        s = frame.set_index("t")["v"].resample(freq).mean().dropna()
        if 8 <= len(s) <= 200:
            return s, name
    s = frame.set_index("t")["v"].resample("D").mean().dropna()
    return (s.tail(200), "day") if len(s) >= 8 else None


# ── detectors ─────────────────────────────────────────────────────────────

def _trends(df, roles) -> list[Finding]:
    from scipy import stats
    out = []
    panel = panel_of(df, roles)
    for tcol in roles["time"][:2]:
        for col in roles["numeric"]:
            values = df[col]
            per = ""
            if panel and panel[1] == tcol:
                # one series per entity: follow the typical entity, each scaled to its own mean
                ent = panel[0]
                v = pd.to_numeric(values, errors="coerce")
                means = v.groupby(df[ent]).transform("mean")
                if (means > 0).all():
                    values = v / means
                    per = f" (each {ent} scaled to its own average)"
            res = _period_series(df[tcol], values)
            if res is None:
                continue
            s, period = res
            tau, p = stats.kendalltau(np.arange(len(s)), s.values)
            if not np.isfinite(tau):
                continue
            # per-period means are smooth, so even a tiny drift can rank perfectly:
            # the size of the move is judged against the spread of single rows
            row_sd = float(pd.to_numeric(values, errors="coerce").std()) or 1e-12
            k = max(1, len(s) // 10)
            first, last = float(s.iloc[:k].mean()), float(s.iloc[-k:].mean())
            if abs(last - first) / row_sd < MIN_DRIFT:
                tau = 0.0                                   # too small to matter: not reported
            change = (last / first - 1) if first not in (0,) and np.isfinite(first) and first != 0 else None
            direction = "rises" if tau > 0 else "falls"
            ch = f", from {_fmt(first)} to {_fmt(last)} ({change * 100:+.0f}%)" if change is not None else ""
            out.append(Finding(
                kind="trend", columns=[tcol, col], effect=abs(float(tau)), p_value=float(p),
                statement=(f"Mean {col}{per} {direction} steadily over {len(s)} {period}s{ch}; "
                           f"Kendall tau = {tau:.2f}."),
                method=f"Kendall's tau of the {period}ly mean of {col} against time",
                chart={"type": "Line Chart", "x": tcol, "y": col, "aggregate": "mean", "period": period},
            ))
            shift = _shift(s, tcol, col, period, row_sd)
            if shift is not None:
                out.append(shift)
    return out


def _shift(s: pd.Series, tcol: str, col: str, period: str, row_sd: float) -> Finding | None:
    from scipy import stats
    v = s.values.astype(float)
    n = len(v)
    if n < 12:
        return None
    best = None
    for i in range(4, n - 3):
        a, b = v[:i], v[i:]
        sd = math.sqrt((a.var(ddof=1) * (len(a) - 1) + b.var(ddof=1) * (len(b) - 1)) / (n - 2)) or 1e-12
        d = abs(b.mean() - a.mean()) / sd
        if best is None or d > best[0]:
            best = (d, i)
    if best is None:
        return None
    _, i = best
    a, b = v[:i], v[i:]
    d = abs(b.mean() - a.mean()) / (row_sd or 1e-12)          # in row-level standard deviations
    if d < MIN_SHIFT_D:
        return None
    p = float(stats.ttest_ind(a, b, equal_var=False).pvalue) * (n - 7)   # Bonferroni over split points
    when = s.index[i]
    when_text = when.strftime("%Y-%m") if hasattr(when, "strftime") else str(when)
    return Finding(
        kind="shift", columns=[tcol, col], effect=float(d), p_value=min(1.0, p),
        statement=(f"{col} changes level around {when_text}: mean {_fmt(a.mean())} before vs "
                   f"{_fmt(b.mean())} after ({d:.1f} standard deviations of a single row)."),
        method=f"largest mean shift in the {period}ly series (Welch t-test, Bonferroni over split points)",
        chart={"type": "Line Chart", "x": tcol, "y": col, "aggregate": "mean", "period": period},
    )


def _group_differences(df, roles) -> list[Finding]:
    from scipy import stats
    out = []
    for cat in roles["category"]:
        groups_all = df[cat].astype("string")
        for col in roles["numeric"]:
            values = pd.to_numeric(df[col], errors="coerce")
            frame = pd.DataFrame({"g": groups_all, "v": values}).dropna()
            sizes = frame["g"].value_counts()
            keep = sizes[sizes >= MIN_GROUP_ROWS].index
            frame = frame[frame["g"].isin(keep)]
            if frame["g"].nunique() < 2:
                continue
            arrays = [g["v"].values for _, g in frame.groupby("g")]
            try:
                h, p = stats.kruskal(*arrays)
            except ValueError:
                continue
            n, k = len(frame), len(arrays)
            eps2 = max(0.0, (h - k + 1) / (n - k)) if n > k else 0.0
            means = frame.groupby("g")["v"].mean().sort_values()
            top, bottom = means.index[-1], means.index[0]
            out.append(Finding(
                kind="group_difference", columns=[cat, col], effect=float(eps2), p_value=float(p),
                statement=(f"{col} differs by {cat}: highest mean in {top} ({_fmt(means.iloc[-1])}), "
                           f"lowest in {bottom} ({_fmt(means.iloc[0])}) across {k} groups; "
                           f"epsilon² = {eps2:.2f}."),
                method=f"Kruskal–Wallis test of {col} across {cat} groups (≥{MIN_GROUP_ROWS} rows each)",
                chart={"type": "Bar Chart", "x": cat, "y": col, "aggregate": "mean"},
            ))
    return out


def _panel_changes(df, roles) -> list[Finding]:
    """For one-series-per-entity tables: which entity rose and fell the most."""
    panel = panel_of(df, roles)
    if panel is None:
        return []
    ent, tcol = panel
    t = pd.to_datetime(df[tcol], errors="coerce") if not pd.api.types.is_numeric_dtype(df[tcol]) else df[tcol]
    out = []
    for col in roles["numeric"][:4]:
        frame = pd.DataFrame({"e": df[ent], "t": t, "v": pd.to_numeric(df[col], errors="coerce")}).dropna()
        changes = {}
        for name, g in frame.sort_values("t").groupby("e"):
            first, last = g["v"].iloc[0], g["v"].iloc[-1]
            if first > 0:
                changes[name] = last / first - 1
        if len(changes) < 3:
            continue
        ch = pd.Series(changes).sort_values()
        span = float(ch.iloc[-1] - ch.iloc[0])
        if span < 0.1:
            continue
        rising = int((ch > 0).sum())
        out.append(Finding(
            kind="panel_change", columns=[ent, col], effect=span,
            statement=(f"From each {ent}'s first to last {tcol}, {col} changed by {ch.iloc[-1] * 100:+.1f}% for "
                       f"{ch.index[-1]} (the highest) down to {ch.iloc[0] * 100:+.1f}% for {ch.index[0]} (the lowest); "
                       f"{rising} of {len(ch)} went up."),
            method=f"change between each {ent}'s first and last value",
            chart={"type": "Bar Chart", "x": ent, "y": f"{col} change", "aggregate": "first→last % change"},
        ))
    return out


def _correlations(df, roles) -> list[Finding]:
    from scipy import stats
    cols = roles["numeric"]
    out = []
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            frame = pd.DataFrame({"a": pd.to_numeric(df[a], errors="coerce"),
                                  "b": pd.to_numeric(df[b], errors="coerce")}).dropna()
            if len(frame) < 10 or frame["a"].nunique() < 3 or frame["b"].nunique() < 3:
                continue
            rho, p = stats.spearmanr(frame["a"], frame["b"])
            if not np.isfinite(rho) or abs(rho) > MAX_RHO:
                continue
            out.append(Finding(
                kind="correlation", columns=[a, b], effect=abs(float(rho)), p_value=float(p),
                statement=(f"{a} and {b} move {'together' if rho > 0 else 'in opposite directions'}: "
                           f"Spearman rho = {rho:.2f} over {len(frame):,} rows."),
                method="Spearman rank correlation",
                chart={"type": "Scatter Plot", "x": a, "y": b},
            ))
    return out


def _outliers(df, roles) -> list[Finding]:
    out = []
    label = _label_column(df, roles["numeric"])
    for col in roles["numeric"]:
        v = pd.to_numeric(df[col], errors="coerce")
        valid = v.dropna()
        if len(valid) < 30:
            continue
        med = valid.median()
        # skewed positive data (amounts, durations): judge on a log scale and only
        # flag unusually LARGE values — a long right tail is not an outlier
        skewed = bool((valid >= 0).all() and med > 0 and valid.skew() > 0.5)
        if skewed:
            positive = valid[valid > 0]
            offset = float(positive.quantile(0.01)) / 2 if (valid == 0).any() else 0.0
            scale = np.log(v.clip(lower=0) + offset)
        else:
            scale = v
        scale_valid = scale.dropna()
        center = scale_valid.median()
        mad = (scale_valid - center).abs().median() * 1.4826
        if not mad:
            continue
        z = (scale - center) / mad
        flagged = (z > OUTLIER_Z) if skewed else (z.abs() > OUTLIER_Z)
        count = int(flagged.sum())
        if count == 0 or count > 0.01 * len(valid):
            continue
        idx = z[flagged].abs().sort_values(ascending=False).index[:3]
        examples = []
        for j in idx:
            name = f"{df.loc[j, label]}: " if label else ""
            examples.append(f"{name}{_fmt(float(v.loc[j]))}")
        zmax = float(z[flagged].abs().max())
        out.append(Finding(
            kind="outliers", columns=[col], effect=zmax,
            statement=(f"{count} value(s) of {col} lie far from the rest (median {_fmt(med)}): "
                       f"{'; '.join(examples)}."),
            method=f"robust z-score (median / MAD) above {OUTLIER_Z:g}",
            chart=({"type": "Scatter Plot", "x": label, "y": col} if label
                   else {"type": "Histogram", "x": col}),
        ))
    return out


_MEASURE = re.compile(r"revenue|sales|amount|gross|profit|cost|spend|value|units|quantity|qty|volume|"
                      r"count|orders?$|销售|金额|收入|利润|数量|成交", re.IGNORECASE)


def _concentration(df, roles) -> list[Finding]:
    """One group holds a large part of the rows (and of the main measure, if any).

    Shares of a total follow row counts unless the measure depends on the
    group, so this reports the row share first and adds the measure's share
    only for columns that are plainly totals (sales, units, ...).
    """
    out = []
    measures = [c for c in roles["numeric"] if _MEASURE.search(str(c))
                and (pd.to_numeric(df[c], errors="coerce").dropna() >= 0).all()]
    for cat in roles["category"]:
        counts = df[cat].value_counts(dropna=True)
        if len(counts) < 4:
            continue
        share = float(counts.iloc[0] / counts.sum())
        if share < MIN_SHARE:
            continue
        top = counts.index[0]
        extra, columns = "", [cat]
        if measures:
            col = measures[0]
            v = pd.to_numeric(df[col], errors="coerce")
            total = v.sum()
            if total > 0:
                extra = f" and {v[df[cat] == top].sum() / total * 100:.0f}% of total {col}"
                columns = [cat, col]
        out.append(Finding(
            kind="concentration", columns=columns, effect=share,
            statement=f"{top} makes up {share * 100:.0f}% of the rows across {len(counts)} {cat} groups{extra}.",
            method="share of rows (and of the column total) by group",
            chart={"type": "Bar Chart", "x": cat, "y": columns[-1] if len(columns) > 1 else "count",
                   "aggregate": "sum" if len(columns) > 1 else "count"},
        ))
    return out


# ── the scan ──────────────────────────────────────────────────────────────

def _score(f: Finding) -> float:
    if f.kind == "trend":
        return f.effect
    if f.kind == "correlation":
        return f.effect * 0.9
    if f.kind == "group_difference":
        return min(1.0, math.sqrt(f.effect) * 1.2)
    if f.kind == "shift":
        return min(1.0, f.effect / 2)
    if f.kind == "outliers":
        return min(0.8, f.effect / 20)
    if f.kind == "concentration":
        return min(0.8, f.effect)
    if f.kind == "panel_change":
        return min(0.9, 0.6 + f.effect / 4)
    return 0.0


def _large_enough(f: Finding) -> bool:
    return {
        "trend": f.effect >= MIN_TAU,
        "correlation": f.effect >= MIN_RHO,
        "group_difference": f.effect >= MIN_EPS2,
        "shift": f.effect >= MIN_SHIFT_D,
    }.get(f.kind, True)


@dataclass
class ScanResult:
    rows: int
    sampled: bool
    tests: int
    findings: list[Finding]
    roles: dict[str, list[str]]

    def to_dict(self) -> dict[str, Any]:
        return {"rows": self.rows, "sampled": self.sampled, "tests": self.tests, "roles": self.roles,
                "findings": [f.to_dict() for f in self.findings]}


def scan(df: pd.DataFrame, *, max_findings: int = 8, fdr: float = FDR, focus: str | None = None) -> ScanResult:
    """All findings worth showing in ``df``, best first."""
    rows = len(df)
    sampled = rows > MAX_ROWS
    if sampled:
        df = df.sample(MAX_ROWS, random_state=0)
    df = df.reset_index(drop=True)
    roles = column_roles(df)
    candidates: list[Finding] = []
    for detect in (_trends, _panel_changes, _group_differences, _correlations, _outliers, _concentration):
        try:
            candidates.extend(detect(df, roles))
        except Exception:  # noqa: BLE001 — one detector must not sink the scan
            continue
    tested = [f for f in candidates if f.p_value is not None]
    for f, q in zip(tested, _bh([f.p_value for f in tested])):
        f.q_value = q
    kept = [f for f in candidates
            if (f.q_value is None or f.q_value < fdr) and _large_enough(f)]
    if focus:
        kept = [f for f in kept if focus in f.columns] or kept
    panel = panel_of(df, roles)
    for f in kept:
        f.score = _score(f)
        if panel and f.kind in ("group_difference", "concentration") and f.columns[0] == panel[0]:
            f.score *= 0.5          # "China emits more than Kazakhstan": true, but rarely news
    kept.sort(key=lambda f: f.score, reverse=True)
    # variety: one finding per kind and column pair, at most three of a kind
    chosen: list[Finding] = []
    per_kind: dict[str, int] = {}
    seen: set[tuple[str, ...]] = set()
    for f in kept:
        key = (f.kind, *sorted(map(str, f.columns)))
        if key in seen or per_kind.get(f.kind, 0) >= 3:
            continue
        chosen.append(f)
        seen.add(key)
        per_kind[f.kind] = per_kind.get(f.kind, 0) + 1
        if len(chosen) >= max_findings:
            break
    return ScanResult(rows=rows, sampled=sampled, tests=len(tested), findings=chosen, roles=roles)


def describe(table: str, result: ScanResult) -> str:
    """The text the agent reads (and quotes numbers from)."""
    head = (f"Insight scan of {table}: {result.rows:,} rows"
            + (f" (a random {MAX_ROWS:,}-row sample was tested)" if result.sampled else "")
            + f"; {result.tests} statistical tests, false-discovery rate {FDR:.0%} "
              "(Benjamini–Hochberg); only effects large enough to matter are listed.")
    if not result.findings:
        return head + "\nNothing stands out: no test passed and no outliers or concentration were found."
    lines = [head, ""]
    for i, f in enumerate(result.findings, 1):
        stat = f" ({_p(f.p_value)}, q={f.q_value:.3g})" if f.q_value is not None else ""
        chart = ", ".join(f"{k}={v}" for k, v in f.chart.items())
        lines.append(f"{i}. [{f.kind}] {f.statement}{stat}\n   method: {f.method}\n   chart: {chart}")
    lines.append("")
    lines.append("These are associations found by a scan, not causes; check each one before relying on it.")
    return "\n".join(lines)


FIND_INSIGHTS_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "find_insights",
        "description": (
            "Scan tables for what stands out — trends over time, level shifts, differences between "
            "groups, correlations, outliers, concentration — with statistical tests and false-"
            "discovery control (no LLM guessing). Use it when the user asks what is interesting / "
            "notable in the data or wants an automatic overview, then chart the top findings one by "
            "one and quote the statistics it reports. Optional `focus`: a column the user cares about."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "table_names": {"type": "array", "items": {"type": "string"}},
                "focus": {"type": "string", "description": "Optional column name to prefer."},
                "max_findings": {"type": "integer", "minimum": 1, "maximum": 12},
            },
            "required": ["table_names"],
        },
    },
}
