# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Rule-based self-check for charts produced by ``visualize``.

Runs on the full output DataFrame right after the visualize code succeeds and
before the chart is shown. Two severities:

* **blocking** — the chart would be misleading or unreadable (too many
  categories, un-aggregated series that zig-zag, no numeric axis, an empty
  field, ...). The chart is withheld and the issues go back to the model as the
  action's observation so it can fix the code/encodings and try again. After
  ``MAX_CHART_RETRIES`` consecutive failures the chart is shown anyway, with
  the issues attached as warnings.
* **warning** — worth knowing but not wrong (many missing values, a constant
  measure, many facets). The chart is shown; the model is told so it can
  mention or address it.

Every issue carries a stable ``code`` + ``params`` (for the UI's translations)
and an English ``message`` written for the model.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd

MAX_CHART_RETRIES = 2

BAR_LIKE = {
    "Bar Chart", "Stacked Bar Chart", "Grouped Bar Chart", "Lollipop Chart",
    "Waterfall Chart", "Bar Table", "Rose Chart",
}
SERIES_LIKE = {
    "Line Chart", "Area Chart", "Streamgraph", "Bump Chart", "Slope Chart",
    "Range Area Chart",
}
# Charts whose color channel is routinely quantitative or high-cardinality.
COLOR_EXEMPT = {"Heatmap", "Map", "Choropleth", "Scatter Plot", "Regression",
                "Connected Scatter Plot", "Strip Plot"}

MAX_BAR_CATEGORIES = 60
MAX_PIE_SLICES = 12
MAX_COLOR_CATEGORIES = 25
# Line-type charts: past ~10 lines the eye can no longer follow a series; past 20
# the chart is spaghetti. Warn first, then send it back.
LINE_LIKE = {"Line Chart", "Area Chart", "Bump Chart", "Slope Chart"}
LINE_SERIES_WARN = 10
LINE_SERIES_MAX = 20
MAX_FACETS = 16
NULL_WARN_RATIO = 0.3


@dataclass
class Issue:
    code: str
    severity: str  # "blocking" | "warning"
    message: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class CheckReport:
    issues: list[Issue]

    @property
    def blocking(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "blocking"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]

    def to_public(self) -> list[dict[str, Any]]:
        return [asdict(i) for i in self.issues]


def _field_name(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("field")
    return value if isinstance(value, str) and value else None


def _is_numeric(series: pd.Series) -> bool:
    return pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series)


def _is_temporal(series: pd.Series) -> bool:
    if pd.api.types.is_datetime64_any_dtype(series):
        return True
    if series.dtype == object:
        sample = series.dropna().astype(str).head(20)
        if len(sample) and sample.str.match(r"^\d{4}([-/]\d{1,2}){0,2}([ T]\d|$)").all():
            return True
    return False


def check_chart(df: pd.DataFrame, chart_spec: dict[str, Any]) -> CheckReport:
    """Return the issues found for this chart (empty list = looks fine)."""
    chart_type = str((chart_spec or {}).get("chart_type") or (chart_spec or {}).get("type") or "")
    enc = {ch: _field_name(v) for ch, v in ((chart_spec or {}).get("encodings") or {}).items()}
    enc = {ch: f for ch, f in enc.items() if f and f in df.columns}
    issues: list[Issue] = []

    # Fields that are entirely empty / mostly empty.
    for channel, name in enc.items():
        col = df[name]
        if len(col) and col.isna().all():
            issues.append(Issue(
                "chartCheck.emptyField", "blocking",
                f"The field '{name}' on the {channel} channel has no values (all null).",
                {"field": name, "channel": channel},
            ))
        elif len(col):
            ratio = float(col.isna().mean())
            if ratio >= NULL_WARN_RATIO:
                issues.append(Issue(
                    "chartCheck.manyNulls", "warning",
                    f"{ratio:.0%} of '{name}' ({channel}) is missing; those rows are not drawn.",
                    {"field": name, "channel": channel, "percent": int(round(ratio * 100))},
                ))

    x, y = enc.get("x"), enc.get("y")
    color = enc.get("color")

    if chart_type in BAR_LIKE and x and y:
        # Bars may be vertical or horizontal: the category axis is the non-numeric one.
        if _is_numeric(df[x]) and not _is_numeric(df[y]):
            cat, val = y, x
        else:
            cat, val = x, y
        n_cat = int(df[cat].nunique(dropna=True))
        if n_cat > MAX_BAR_CATEGORIES and not _is_numeric(df[cat]):
            issues.append(Issue(
                "chartCheck.tooManyCategories", "blocking",
                f"'{cat}' has {n_cat} categories — too many bars to read. Keep the top "
                f"~{min(20, MAX_BAR_CATEGORIES)} by value (group the rest as 'Other') or choose another chart.",
                {"field": cat, "count": n_cat, "limit": MAX_BAR_CATEGORIES},
            ))
        if not _is_numeric(df[val]) and not _is_temporal(df[val]):
            issues.append(Issue(
                "chartCheck.noQuantitativeAxis", "blocking",
                f"Neither '{x}' nor '{y}' is numeric, so the bars have no length. "
                "Aggregate a measure (count/sum/mean) into a numeric column.",
                {"x": x, "y": y},
            ))
        elif _is_numeric(df[val]) and df[val].nunique(dropna=True) == 1 and len(df) > 1:
            issues.append(Issue(
                "chartCheck.constantMeasure", "warning",
                f"Every bar has the same value of '{val}', so the chart shows no differences.",
                {"field": val},
            ))

    if chart_type in SERIES_LIKE and x and y:
        n_x = int(df[x].nunique(dropna=True))
        if n_x < 2:
            issues.append(Issue(
                "chartCheck.singlePointSeries", "blocking",
                f"A {chart_type} needs at least 2 distinct '{x}' values; found {n_x}. "
                "Use a bar chart or KPI card for a single value.",
                {"field": x, "count": n_x},
            ))
        if _is_numeric(df[y]):
            keys = [k for k in (x, color, enc.get("strokeDash"), enc.get("facet")) if k]
            dupes = int(df.duplicated(subset=keys).sum())
            if dupes:
                issues.append(Issue(
                    "chartCheck.unaggregatedSeries", "blocking",
                    f"{dupes} rows repeat the same {', '.join(keys)} combination, so the line "
                    f"zig-zags between several '{y}' values per point. Aggregate '{y}' per "
                    f"{', '.join(keys)} (e.g. sum or mean), or split the series with color.",
                    {"fields": ", ".join(keys), "count": dupes},
                ))
        elif not _is_temporal(df[y]):
            issues.append(Issue(
                "chartCheck.noQuantitativeAxis", "blocking",
                f"'{y}' (y) is not numeric, so the {chart_type} has nothing to plot.",
                {"x": x, "y": y},
            ))

    if chart_type == "Pie Chart":
        slice_field = color
        size = enc.get("size")
        if slice_field:
            n = int(df[slice_field].nunique(dropna=True))
            if n > MAX_PIE_SLICES:
                issues.append(Issue(
                    "chartCheck.tooManySlices", "blocking",
                    f"The pie has {n} slices — keep the top ~7 and group the rest as 'Other', "
                    "or use a bar chart.",
                    {"field": slice_field, "count": n, "limit": MAX_PIE_SLICES},
                ))
        if size and _is_numeric(df[size]) and (df[size] < 0).any():
            issues.append(Issue(
                "chartCheck.negativePie", "blocking",
                f"'{size}' has negative values, which a pie cannot show. Use a bar chart.",
                {"field": size},
            ))

    line_blocked = False
    if chart_type in LINE_LIKE or "Line" in chart_type:
        series = next((enc[ch] for ch in ("color", "strokeDash", "detail") if enc.get(ch)), None)
        if series and not _is_numeric(df[series]):
            n = int(df[series].nunique(dropna=True))
            if n > LINE_SERIES_MAX:
                line_blocked = True
                issues.append(Issue(
                    "chartCheck.lineTooManySeries", "blocking",
                    f"The {chart_type} draws {n} lines (one per '{series}') — they overlap into an "
                    f"unreadable tangle. Keep at most ~{LINE_SERIES_WARN} (e.g. the top by total or "
                    "by change, grouping the rest as 'Other'), or facet into small panels.",
                    {"field": series, "count": n, "limit": LINE_SERIES_MAX},
                ))
            elif n > LINE_SERIES_WARN:
                issues.append(Issue(
                    "chartCheck.lineManySeries", "warning",
                    f"The {chart_type} draws {n} lines (one per '{series}'); consider showing the "
                    f"top ~{LINE_SERIES_WARN} and grouping the rest, or faceting.",
                    {"field": series, "count": n, "limit": LINE_SERIES_WARN},
                ))

    if color and not line_blocked and chart_type not in COLOR_EXEMPT and chart_type != "Pie Chart":
        if not _is_numeric(df[color]):
            n = int(df[color].nunique(dropna=True))
            if n > MAX_COLOR_CATEGORIES:
                issues.append(Issue(
                    "chartCheck.tooManyColors", "blocking",
                    f"'{color}' (color) has {n} distinct values — the legend and colors become "
                    "unreadable. Keep the top series and group the rest, or drop color.",
                    {"field": color, "count": n, "limit": MAX_COLOR_CATEGORIES},
                ))

    facet = enc.get("facet")
    if facet:
        n = int(df[facet].nunique(dropna=True))
        if n > MAX_FACETS:
            issues.append(Issue(
                "chartCheck.tooManyFacets", "warning",
                f"'{facet}' splits the chart into {n} small panels; consider filtering to fewer.",
                {"field": facet, "count": n},
            ))

    return CheckReport(issues)


def describe_for_model(report: CheckReport, retrying: bool) -> str:
    """The text the model reads for a chart that failed (or passed with notes)."""
    lines = []
    if retrying:
        lines.append(
            "[CHART CHECK FAILED] The chart was NOT shown to the user. Fix these problems "
            "and call `visualize` again:"
        )
    else:
        lines.append("[CHART CHECK] The chart was shown, with these notes:")
    for issue in report.issues:
        lines.append(f"- ({issue.severity}) {issue.message}")
    return "\n".join(lines)
