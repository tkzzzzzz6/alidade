"""Number provenance: numbers in a conclusion must come from the run."""
from __future__ import annotations

import pytest

from alidade.analyst.engine import number_check as nc


def claims(text):
    return [c.text for c in nc.extract_claims(text)]


def check(conclusion, printed="", charts="", raw=(), given=()):
    ev = nc.Evidence(computed=nc.numbers_in(charts), printed=nc.numbers_in(printed),
                     raw=list(raw), given=list(given))
    return nc.check(conclusion, ev)


# ── which numbers count as claims ──────────────────────────────────────────

def test_findings_are_extracted_with_units():
    text = "GOOGL +73.9% 领跑，META −12.9%；NVDA 日均约 1.8 亿股，均价 511.54 美元，高 12.4 个百分点，回报 11.7 倍。"
    assert claims(text) == ["+73.9%", "-12.9%", "1.8 亿", "511.54", "12.4 个百分点", "11.7 倍"]


@pytest.mark.parametrize("text", [
    "2022 年最高", "从 2000 到 2020", "2025-07-31 至 2026-01-26", "8-12 月", "7 月到 12 月",
    "共 3 个国家", "前 5 名", "PG-13 级", "s1 和 s2", "Q3 财报", "CO2 排放", "5G 网络", "v2.1 版本",
])
def test_years_counts_dates_and_identifiers_are_skipped(text):
    assert claims(text) == []


def test_number_near_a_year_with_a_unit_is_still_checked():
    assert claims("增长 1959 TWh") == ["1959"]
    assert claims("2000 万美元") == ["2000 万"]


def test_letter_units_need_a_word_boundary():
    assert claims("约 5 M 美元") == ["5 M"]
    assert [c.unit for c in nc.extract_claims("相差 15 Mining 行业")] == [""]


# ── evidence parsing ───────────────────────────────────────────────────────

def test_numbers_in_printed_output():
    out = nc.numbers_in("GOOGL    0.739214\nmean   1,234.5\nbig 2.41e+08\nshare 73.9%")
    assert 0.739214 in out and 1234.5 in out and 241000000.0 in out
    assert 73.9 in out and pytest.approx(0.739) in out          # a printed % counts both ways


# ── matching ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("conclusion,printed", [
    ("收益率 +73.9%", "GOOGL 0.739214"),                 # fraction printed, percent written
    ("收益率 73.92%", "GOOGL 73.9214"),                  # percent printed
    ("平均 511.54 美元", "511.5431"),
    ("约 512 美元", "511.5431"),                         # rounded to whole dollars
    ("约 2.41 亿", "241171386"),                         # Chinese scale unit
    ("约 241M 美元", "241171386"),
    ("下降 41.2%", "-0.412345"),                         # sign is in the words
    ("1,234,567 次", "1234567"),
])
def test_written_forms_of_a_printed_number_match(conclusion, printed):
    report = check(conclusion, printed=printed)
    assert report.checked == 1 and report.unsupported == [], conclusion


@pytest.mark.parametrize("conclusion,printed", [
    ("高 12.4 个百分点", "2006 6.683\n2009 19.050"),     # difference of two printed numbers
    ("是预算的 11.68 倍", "gross 2767891499\nbudget 237000000"),   # ratio
    ("跌了 60.8%", "max 4.114\nmin 1.613"),             # decline
    ("涨了 45%", "start 100\nend 145"),                  # change
])
def test_simple_derived_numbers_match(conclusion, printed):
    report = check(conclusion, printed=printed)
    assert report.unsupported == [] and report.by_source == {"derived": 1}


def test_chart_data_and_input_cells_and_question_count():
    assert check("NVDA 1.8 亿股", charts='[{"v": 180409198}]').by_source == {"computed": 1}
    assert check("Avatar 票房 2,767,891,499 美元", raw=[2767891499.0]).by_source == {"raw": 1}
    assert check("预算 237000000 美元", given=[237000000.0]).by_source == {"computed": 1}


@pytest.mark.parametrize("conclusion", [
    "AAPL 2024 年涨了 30.7%",                           # data has no 2024
    "平均 6.05 分",                                       # a number from a different row
    "约 9.2 倍",                                          # remembered, not computed
])
def test_numbers_without_a_source_are_flagged(conclusion):
    report = check(conclusion, printed="AAPL 0.233\nR 6.432\nPG 5.933\nratio 11.679")
    assert len(report.unsupported) == 1
    item = report.to_dict()["unsupported_items"][0]
    assert item["text"] in conclusion and item["context"]


def test_precision_matters():
    # 6.4 is a fair rounding of 6.432, 6.5 is not
    assert check("R 级 6.4 分", printed="6.432").unsupported == []
    assert len(check("R 级 6.5 分", printed="6.432").unsupported) == 1


def test_report_counts():
    report = check("A 73.9%，B 12.9%，C 99.9%", printed="0.739 0.129")
    d = report.to_dict()
    assert d["checked"] == 3 and d["unsupported"] == 1 and d["by_source"] == {"computed": 2}


def test_empty_inputs():
    assert check("").checked == 0
    assert check("没有数字的结论").to_dict()["unsupported"] == 0
    assert len(check("一个 42.5 的数").unsupported) == 1        # no evidence at all


# ── the same quantity in another magnitude unit ────────────────────────────

@pytest.mark.parametrize("conclusion,printed", [
    ("AAPL 日均 5080 万股", "AAPL 50.84"),                     # printed in millions
    ("约 300.5 百万吨", "2019 300519.989"),                     # printed in kt
    ("300,519.99 千吨（约 3.005 亿吨）", "2019 300519.989"),
    ("约 3.73 亿吨", "2000 373120.0"),
    ("浪漫喜剧（8,660 万）", "Romantic Comedy 86617851"),      # trailing zero = rounding
])
def test_magnitude_unit_changes_match(conclusion, printed):
    assert check(conclusion, printed=printed).unsupported == [], conclusion


def test_raw_cells_need_a_precise_number():
    # a big table holds thousands of cells: a short number hits one by chance
    assert check("约 3.73 亿吨", raw=(373120.0,)).unsupported != []
    assert check("Avatar 票房约 27.7 亿", raw=(2767891499.0,)).unsupported != []
    assert check("排放 300,519.99 千吨", raw=(300519.989,)).unsupported == []


def test_derived_numbers_need_a_comparison():
    printed = "max 4.114\nmin 1.613"
    assert check("每加仑跌了 2.501 美元", printed=printed).by_source == {"derived": 1}
    assert check("价格是 2.501 美元", printed=printed).unsupported != []   # reads as a level, not a change


def test_share_of_a_total_is_derived():
    assert check("占总额的 34.5%", printed="north 96541.2\ntotal 279827.58").by_source == {"derived": 1}


def test_imprecise_numbers_do_not_shift_against_raw_cells():
    # "37 万" has 2 significant digits: too easy to hit some cell by chance
    assert len(check("约 37 万吨", raw=(373.0,)).unsupported) == 1
    assert check("约 37 万吨", printed="373").unsupported == []    # printed output is fine


def test_percentages_never_shift():
    assert len(check("占比 73.9%", printed="739").unsupported) == 1


def test_significant_digits():
    sig = {c.text: c.sig_digits for c in nc.extract_claims("5080 万、300,519.99、0.0450、12.40、700")}
    assert sig == {"5080 万": 3, "300,519.99": 8, "0.0450": 3, "12.40": 4, "700": 1}


def test_bounds_allow_one_unit_and_exclusions_are_comparisons():
    assert check("2012 年突破 $0.27", printed="spread 0.276").unsupported == []
    assert check("2012 年为 $0.27", printed="spread 0.276").unsupported != []     # stated as the value
    assert check("2899 部中有 302 部被排除", printed="valid 2899 of 3201").by_source == {"computed": 1, "derived": 1}
    assert check("2899 部中有 302 部因缺失或无效被排除", printed="valid 2899 of 3201").unsupported == []
