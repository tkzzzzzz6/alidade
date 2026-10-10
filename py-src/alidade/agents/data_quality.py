"""A quick, code-only health check of a table, for the agent's first look.

The agent sees each table's schema, a few sample rows and some value samples.
That is enough to spot obvious problems, but not the ones that only show up
across the whole table: re-exported duplicate rows, a text column that is
really money, three date spellings mixed in one column, or "North" / "north" /
" NORTH " counted as three regions. A naive analysis of such a table gives a
confident, wrong answer.

``data_quality_notes(df)`` scans the table with pandas (no LLM) and returns a
few short lines describing what it found, each with the fix. The notes go into
the table context next to the samples, so the agent sees them before it writes
any code. A clean table gets no notes at all.
"""

from __future__ import annotations

import re
from typing import Any

import pandas as pd

MAX_NOTES = 8               # per table — keep the context small
SCAN_ROWS = 50_000          # text-pattern checks run on a sample this large
MIN_SHARE = 0.9             # "looks numeric / like a date" if ≥90% of non-empty values do
MISSING_WARN = 0.05         # mention columns with ≥5% missing values
LABEL_MAX_DISTINCT = 50     # look for case/space variants only in category-like columns
KEY_MIN_UNIQUE = 0.8         # an id-named column this unique is meant to identify rows
DUP_CHECK_MAX_ROWS = 2_000_000  # whole-row hashing above this is too slow for a first look

PLACEHOLDERS = {"", "n/a", "na", "nan", "null", "none", "-", "--", "?", "#n/a"}

_NUMBER_RE = re.compile(r"^[\s(]*[-+]?[$€£¥]?\s*[-+]?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?\s*%?[)\s]*$")
_DATE_STYLES = [
    ("YYYY-MM-DD", re.compile(r"^\d{4}-\d{1,2}-\d{1,2}([ T]\d{1,2}:\d{2}(:\d{2})?)?$")),
    ("YYYY/MM/DD", re.compile(r"^\d{4}/\d{1,2}/\d{1,2}$")),
    ("MM/DD/YYYY", re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")),
    ("DD.MM.YYYY", re.compile(r"^\d{1,2}\.\d{1,2}\.\d{4}$")),
    ("Mon D, YYYY", re.compile(r"^[A-Za-z]{3,9}\.? \d{1,2},? \d{4}$")),
    ("D Mon YYYY", re.compile(r"^\d{1,2}[ -][A-Za-z]{3,9}[ -]\d{2,4}$")),
]
_KEY_NAME_RE = re.compile(r"(^|_|\b)(id|key|code|uuid|no|number)$|编号|单号", re.IGNORECASE)


def _is_text(series: pd.Series) -> bool:
    return pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series)


def _text_values(series: pd.Series) -> pd.Series:
    """Non-null values as stripped strings (sampled for big tables)."""
    s = series.dropna()
    if len(s) > SCAN_ROWS:
        s = s.sample(SCAN_ROWS, random_state=0)
    return s.astype(str).str.strip()


def _quote(values: list[Any], limit: int = 3) -> str:
    return ", ".join(repr(str(v)) for v in values[:limit])


def _placeholder_mask(values: pd.Series) -> pd.Series:
    return values.str.lower().isin(PLACEHOLDERS)


def _duplicate_notes(df: pd.DataFrame) -> list[str]:
    notes = []
    try:
        dup_rows = int(df.duplicated().sum())
    except TypeError:       # unhashable cells (lists, dicts)
        return notes
    if dup_rows:
        notes.append(f"{dup_rows:,} rows are exact duplicates of another row "
                     f"({dup_rows / len(df):.0%}) — likely re-exported records; drop them "
                     f"(df.drop_duplicates()) before counting or summing.")
    # ids still repeated once exact duplicate rows are gone: conflicting records
    base = df.drop_duplicates() if dup_rows else df
    for col in base.columns:
        s = base[col]
        if not (_KEY_NAME_RE.search(str(col)) and (_is_text(s) or pd.api.types.is_integer_dtype(s))):
            continue
        try:
            n_unique = s.nunique(dropna=True)
        except TypeError:
            continue
        if n_unique < KEY_MIN_UNIQUE * len(base):   # not meant as a key (a customer id on orders)
            continue
        repeated = int(s.notna().sum()) - n_unique
        if repeated:
            notes.append(f"Key column '{col}' has {repeated:,} values that appear on more than one "
                         f"row{' even after dropping exact duplicates' if dup_rows else ''} — "
                         f"check whether these are duplicate records before counting.")
        break
    return notes


def _text_column_notes(col: str, series: pd.Series) -> list[str]:
    values = _text_values(series)
    if values.empty:
        return []
    holders = _placeholder_mask(values)
    filled = values[~holders]
    if filled.empty:
        return []
    notes = []

    # numbers stored as text
    numeric_like = filled.str.match(_NUMBER_RE)
    if numeric_like.mean() >= MIN_SHARE:
        formatted = filled[numeric_like & filled.str.contains(r"[$€£¥,%()]", regex=True)]
        example = _quote(formatted.unique().tolist() or filled.unique().tolist(), 2)
        note = f"'{col}' holds numbers stored as text (e.g. {example})"
        if holders.any():
            found = sorted({v if v else '""' for v in values[holders].unique()})[:4]
            note += f" plus placeholders {', '.join(found)}"
        note += (" — strip symbols/commas and convert with pd.to_numeric(errors='coerce'); "
                 "treat placeholders as missing, not 0.")
        notes.append(note)
        return notes

    # dates stored as text
    styles: dict[str, list[str]] = {}
    matched = pd.Series(False, index=filled.index)
    for name, rx in _DATE_STYLES:
        hit = filled.str.match(rx) & ~matched
        if hit.any():
            styles[name] = filled[hit].head(1).tolist()
            matched |= hit
    if styles and matched.mean() >= MIN_SHARE:
        examples = _quote([v[0] for v in styles.values()], 4)
        if len(styles) > 1:
            notes.append(f"'{col}' is a date stored as text in {len(styles)} different formats "
                         f"(e.g. {examples}) — parse with pd.to_datetime(..., format='mixed') and "
                         f"check no values become NaT; parsing one format only silently drops the rest.")
        elif "YYYY-MM-DD" not in styles:   # plain ISO text parses (and sorts) fine
            notes.append(f"'{col}' is a date stored as text (e.g. {examples}) — parse it with "
                         f"pd.to_datetime before sorting or grouping by year/month.")
        return notes

    # the same category spelled differently
    distinct = filled.unique()
    if 1 < len(distinct) <= LABEL_MAX_DISTINCT:
        groups: dict[str, list[str]] = {}
        for v in series.dropna().astype(str).unique():
            if v.strip().lower() in PLACEHOLDERS:
                continue
            groups.setdefault(" ".join(v.split()).lower(), []).append(v)
        variants = [g for g in groups.values() if len(g) > 1]
        if variants:
            shown = "; ".join(_quote(sorted(g), 3) for g in variants[:3])
            notes.append(f"'{col}' spells the same value in different case/spacing ({shown}) — "
                         f"normalize with .str.strip().str.title() (or similar) before grouping.")
    return notes


def _missing_notes(df: pd.DataFrame, already: set[str]) -> list[str]:
    parts = []
    n = len(df)
    for col in df.columns:
        if col in already:
            continue
        s = df[col]
        missing = int(s.isna().sum())
        if _is_text(s):
            missing += int(_placeholder_mask(s.dropna().astype(str).str.strip()).sum())
        if missing / n >= MISSING_WARN:
            parts.append((missing / n, f"'{col}' {missing / n:.0%}"))
    if not parts:
        return []
    parts = [text for _, text in sorted(parts, key=lambda p: -p[0])]
    return ["Missing values: " + ", ".join(parts[:6]) + (" …" if len(parts) > 6 else "")
            + " — say how you handled them (excluded, not counted as 0)."]


def data_quality_notes(df: pd.DataFrame) -> list[str]:
    """Short findings about ``df`` that change analysis results; [] when clean."""
    if df is None or len(df) == 0 or len(df.columns) == 0:
        return []
    notes: list[str] = []
    try:
        if len(df) <= DUP_CHECK_MAX_ROWS:
            notes += _duplicate_notes(df)
        flagged: set[str] = set()
        for col in df.columns:
            if _is_text(df[col]):
                found = _text_column_notes(str(col), df[col])
                if found:
                    flagged.add(col)
                    notes += found
        notes += _missing_notes(df, flagged)
    except Exception:  # noqa: BLE001 — a failed check must never block the agent
        return notes[:MAX_NOTES]
    return notes[:MAX_NOTES]
