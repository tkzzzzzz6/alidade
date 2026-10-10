"""Number provenance: are the numbers in a conclusion backed by the run?

An analyst's closing answer usually quotes numbers ("GOOGL +73.9%", "平均
511.54 美元", "约 2.4 亿"). This module checks each of them against what the
run actually produced:

* **computed** — text the code printed (tool outputs) and the data behind the
  charts; also differences and ratios of two printed numbers, since "12.4
  个百分点" or "高出 3 倍" are derived on the fly;
* **raw** — values in the input tables (quoting a cell is fine);
* **given** — numbers in the user's question or the data notes.

A number that matches none of them, within the precision it is written at,
is *unsupported*: the model may have guessed it, misremembered it, or done
the arithmetic in its head. That is what this check measures.

Pure Python with no dependency on the rest of the package, so the evaluation
harness can load it for any engine (including the pre-change one).

What is deliberately not checked: years (1800–2100 written without a unit),
small whole numbers ≤ 10 (counts, list items, "3 个国家"), numbers glued to
letters (PG-13, Q3, s1, CO2), and ranges/dates (8-12 月, 2025-07-31).
"""

from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass, field
from typing import Iterable

# ── claims: numbers in the conclusion ─────────────────────────────────────

_UNITS: list[tuple[str, float, str]] = [
    # (regex alternative, multiplier, kind)
    ("个百分点", 1.0, "pp"), ("百分点", 1.0, "pp"), ("pp", 1.0, "pp"), ("pts", 1.0, "pp"),
    ("%", 1.0, "pct"), ("％", 1.0, "pct"),
    ("万亿", 1e12, "scale"), ("亿", 1e8, "scale"), ("千万", 1e7, "scale"), ("百万", 1e6, "scale"),
    ("万", 1e4, "scale"), ("千", 1e3, "scale"),
    ("trillion", 1e12, "scale"), ("billion", 1e9, "scale"), ("million", 1e6, "scale"),
    ("thousand", 1e3, "scale"), ("bn", 1e9, "scale"), ("B", 1e9, "scale"), ("M", 1e6, "scale"),
    ("K", 1e3, "scale"), ("k", 1e3, "scale"),
    ("倍", 1.0, "times"), ("x", 1.0, "times"), ("×", 1.0, "times"),
]
_UNIT_INFO = {u: (m, k) for u, m, k in _UNITS}

def _unit_alt(unit: str) -> str:
    # a letter unit must end the word: "5 M" is a million, "5 Mining" is not
    return re.escape(unit) + (r"(?![A-Za-z])" if unit[-1].isascii() and unit[-1].isalpha() else "")


_CLAIM_RE = re.compile(
    r"(?P<sign>[-+])?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?![\d.,]\d)"
    r"(?:\s?(?P<unit>" + "|".join(_unit_alt(u) for u, _, _ in _UNITS) + r"))?"
    r"(?!\d)"
)
_DATE_PART = re.compile(r"^\s?(月|日|号|季度|周|时|点)")
_LETTER_NEXT = re.compile(r"^\s?[A-Za-z]")
MAX_CONTEXT = 40
NEAR_BEFORE, NEAR_AFTER = 8, 8   # "高约 12.4 个百分点", "跌了 2.501 美元", "剩 515 笔"


@dataclass
class Claim:
    text: str                 # as written, e.g. "+73.9%"
    value: float              # the number itself, e.g. 73.9
    unit: str                 # "", "%", "亿", "个百分点", …
    candidates: list[tuple[float, float]]   # (absolute value to look for, tolerance)
    context: str              # a few characters around it, for the report
    kind: str = ""            # "pct" / "pp" / "scale" / "times" / ""
    near: str = ""            # the few characters right around it (comparison words)
    sig_digits: int = 0       # significant digits as written ("5080" → 3, "300,519.99" → 8)


def _normalize(text: str) -> str:
    return (text.replace("−", "-").replace("–", "-").replace("，", ",")
            .replace("．", "."))


def _half_unit(num: str) -> float:
    """Half of the last significant written digit.

    '73.9' → 0.05, '511' → 0.5, and trailing zeros of a whole number are read
    as rounding: '8,660' → 5, '2,300' → 50.
    """
    if "." in num:
        return 0.5 * 10 ** (-len(num.split(".")[1]))
    whole = num.replace(",", "")
    zeros = len(whole) - len(whole.rstrip("0")) if whole.strip("0") else 0
    return 0.5 * 10 ** zeros


def extract_claims(text: str) -> list[Claim]:
    """Numbers in ``text`` that a reader would take as findings."""
    text = _normalize(text or "")
    claims: list[Claim] = []
    for m in _CLAIM_RE.finditer(text):
        start = m.start("num")
        before = text[start - 1] if start > 0 else ""
        before2 = text[start - 2] if start > 1 else ""
        # glued to a word or part of a range/date: PG-13, s1, 8-12, 2025-07-31, v2.1
        if before and before.isascii() and (before.isalnum() or before in "._/"):
            continue
        if before == "-" and before2 and before2.isascii() and (before2.isalnum() or before2 in "._"):
            continue
        num, unit = m.group("num"), m.group("unit") or ""
        after = text[m.end():m.end() + 1]
        if after in ("-", "/", ".") and text[m.end() + 1:m.end() + 2].isdigit():
            continue                              # start of a range or a date: 8-12, 2025-07
        value = float(num.replace(",", ""))
        whole = "." not in num
        rest = text[m.end():]
        if not unit and whole and 1800 <= value <= 2100 and not _LETTER_NEXT.match(rest):
            continue                              # a year (but "1959 TWh" is a quantity)
        if not unit and whole and value <= 31 and _DATE_PART.match(rest):
            continue                              # 12 月, 3 日, 第 2 季度
        if not unit and whole and value <= 10:
            continue                              # counts, list numbers
        digits = num.replace(",", "").replace(".", "").lstrip("0")
        sig = len(digits.rstrip("0")) if "." not in num else len(digits)
        claims.append(Claim(
            text=m.group(0).strip(), value=value, unit=unit,
            kind=_UNIT_INFO.get(unit, (1.0, ""))[1], sig_digits=max(sig, 1),
            near=text[max(0, m.start() - NEAR_BEFORE): m.end() + NEAR_AFTER],
            candidates=_candidates(num, value, unit),
            context=text[max(0, m.start() - MAX_CONTEXT // 2): m.end() + MAX_CONTEXT // 2].replace("\n", " "),
        ))
    return claims


def _candidates(num: str, value: float, unit: str) -> list[tuple[float, float]]:
    """The values a claim may stand for, each with its rounding tolerance."""
    half = _half_unit(num)
    mult, kind = _UNIT_INFO.get(unit, (1.0, ""))
    # Only the precision the number is written at: a looser relative margin
    # lets wrong numbers match something in a big result by chance.
    rel = 1e-9
    out: list[tuple[float, float]] = []

    def add(v: float, tol: float) -> None:
        out.append((abs(v), max(tol, abs(v) * rel)))

    if kind in ("pct", "pp"):
        add(value / 100, half / 100)              # 73.9% ↔ 0.739
        add(value, half)                          # 73.9% ↔ 73.9 (already in percent)
    elif kind == "scale":
        add(value * mult, half * mult)            # 2.41 亿 ↔ 241,000,000
    else:
        add(value, half)                          # plain numbers and "3.2 倍"
    return out


# ── evidence: numbers the run produced ────────────────────────────────────

_EVIDENCE_RE = re.compile(r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][-+]?\d+)?%?")


def numbers_in(text: str) -> list[float]:
    """Every number in printed output: 1,234.5 / 2.41e+08 / -0.739 / 73.9%."""
    out: list[float] = []
    for tok in _EVIDENCE_RE.findall(_normalize(text or "")):
        pct = tok.endswith("%")
        try:
            v = float(tok.rstrip("%").replace(",", ""))
        except ValueError:
            continue
        if math.isfinite(v):
            out.append(abs(v))
            if pct:
                out.append(abs(v) / 100)
    return out


class _Sorted:
    def __init__(self, values: Iterable[float]):
        self.values = sorted({round(v, 12) for v in values if math.isfinite(v)})

    def near(self, target: float, tol: float) -> bool:
        i = bisect.bisect_left(self.values, target - tol)
        return i < len(self.values) and self.values[i] <= target + tol

    def __len__(self) -> int:
        return len(self.values)


MAX_DERIVED_INPUTS = 300   # printed numbers used for differences/ratios (n² pairs)
MAGNITUDE_SHIFTS = (1e-9, 1e-6, 1e-3, 1e3, 1e6, 1e9)
# a cell of a large input table matches only numbers written this precisely;
# shorter ones hit some cell by chance far too often
RAW_MIN_DIGITS = 5
# "突破 0.27" for 0.276: a bound, not a rounding — allow one unit of the last digit
_BOUND = re.compile(r"突破|超过|超出|高于|低于|不足|不到|以上|以下|至少|至多|逾|over|above|below|under|at least|exceeds?",
                    re.IGNORECASE)
_COMPARISON = re.compile(
    r"高|低|差|增|减|涨|跌|降|升|多|少|倍|变化|幅|占|比|剩|余|去掉|删除|剔除|排除|过滤|缺失|无效|有效|合计|共|总|"
    r"more|less|higher|lower|increase|decrease|change|diff|share|gap|total|remain", re.IGNORECASE)


@dataclass
class Evidence:
    computed: list[float] = field(default_factory=list)   # printed output + chart data
    printed: list[float] = field(default_factory=list)    # printed output only (for derived values)
    raw: list[float] = field(default_factory=list)        # input table cells
    given: list[float] = field(default_factory=list)      # question, notes

    def __post_init__(self) -> None:
        self._direct = _Sorted([*self.computed, *self.printed, *self.given])
        self._raw = _Sorted(self.raw)
        self._derived: _Sorted | None = None

    def _derived_set(self) -> _Sorted:
        if self._derived is None:
            base = sorted({v for v in self.printed if v != 0})[:MAX_DERIVED_INPUTS]
            derived: list[float] = []
            for i, a in enumerate(base):
                for b in base[i + 1:]:
                    derived.append(b - a)              # difference
                    derived.append(b / a)              # ratio ("3.2 倍")
                    derived.append(a / b)              # share ("占 34.5%")
                    derived.append(b / a - 1)          # change ("+45%")
                    derived.append(1 - a / b)          # decline ("−31%")
            self._derived = _Sorted(derived)
        return self._derived

    def source_of(self, claim: Claim) -> str | None:
        """'computed' / 'raw' / 'derived', or None when nothing backs the claim.

        Each rule below was kept only if it did not let deliberately wrong
        numbers through (see ``eval/README.md``, "数字溯源"): raw cells must
        match precisely, derived values are only tried for numbers that read
        as a comparison, and unit shifts only for numbers written with one.
        """
        candidates = claim.candidates
        if _BOUND.search(claim.near):
            candidates = [(v, 2 * t) for v, t in candidates]
        if any(self._direct.near(v, t) for v, t in candidates):
            return "computed"
        if claim.sig_digits >= RAW_MIN_DIGITS and any(self._raw.near(v, t) for v, t in claim.candidates):
            return "raw"
        if claim.kind in ("pct", "pp", "times") or _COMPARISON.search(claim.near):
            # a change written in % is a fraction of the base, never "45" itself
            cands = claim.candidates[:1] if claim.kind == "pct" else claim.candidates
            if any(self._derived_set().near(v, t) for v, t in cands):
                return "derived"
        if claim.kind == "scale":
            # The same quantity in another magnitude unit: volume printed in
            # millions (50.84) written "5080 万股"; a column in kt (300,519.99)
            # written "3.005 亿吨". Shift by powers of 1000.
            shifted = [(v * f, t * f) for v, t in claim.candidates for f in MAGNITUDE_SHIFTS]
            if any(self._direct.near(v, t) for v, t in shifted):
                return "computed"
            if claim.sig_digits >= RAW_MIN_DIGITS and any(self._raw.near(v, t) for v, t in shifted):
                return "raw"
        return None


@dataclass
class Report:
    checked: int
    unsupported: list[Claim]
    by_source: dict[str, int]

    def to_dict(self) -> dict:
        return {
            "checked": self.checked,
            "unsupported": len(self.unsupported),
            "by_source": self.by_source,
            "unsupported_items": [{"text": c.text, "context": c.context} for c in self.unsupported[:20]],
        }


def check(conclusion: str, evidence: Evidence) -> Report:
    """Check every number in ``conclusion`` against ``evidence``."""
    claims = extract_claims(conclusion)
    unsupported: list[Claim] = []
    by_source: dict[str, int] = {}
    for c in claims:
        src = evidence.source_of(c)
        if src is None:
            unsupported.append(c)
        else:
            by_source[src] = by_source.get(src, 0) + 1
    return Report(checked=len(claims), unsupported=unsupported, by_source=by_source)
