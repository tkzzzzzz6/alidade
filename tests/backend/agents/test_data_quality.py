"""The automatic data quality check shown with each table (agents/data_quality.py)."""

import pandas as pd
import pytest

from alidade.agents.data_quality import MAX_NOTES, data_quality_notes


def _messy() -> pd.DataFrame:
    rows = [
        ("A1", "2024-01-05", "North", "$1,200.50"),
        ("A2", "2024/02/10", "north", "N/A"),
        ("A3", "Mar 5, 2024", " NORTH ", "$80.00"),
        ("A4", "2024-04-01", "East", ""),
        ("A5", "Mar 9, 2024", "East", "$300.25"),
        ("A6", "2024-06-30", "West", "$45.10"),
    ]
    df = pd.DataFrame(rows, columns=["order_id", "order_date", "region", "amount"])
    return pd.concat([df, df.iloc[[0, 5]]], ignore_index=True)   # two re-exported rows


def _joined(notes):
    return "\n".join(notes)


def test_clean_table_gets_no_notes():
    df = pd.DataFrame({"id": [1, 2, 3], "date": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"]),
                       "city": ["Oslo", "Rome", "Lima"], "sales": [1.0, 2.5, 3.0]})
    assert data_quality_notes(df) == []


def test_messy_table_reports_every_problem_with_a_fix():
    text = _joined(data_quality_notes(_messy()))
    assert "2 rows are exact duplicates" in text and "drop_duplicates" in text
    assert "'order_date' is a date stored as text in 3 different formats" in text and "format='mixed'" in text
    assert "'region' spells the same value" in text and "' NORTH '" in text
    assert "'amount' holds numbers stored as text" in text and "'$1,200.50'" in text
    assert "N/A" in text and "not 0" in text


def test_key_column_repeats_beyond_exact_duplicates():
    df = pd.DataFrame({"order_id": ["A1", "A1", "A2", "A3", "A4"], "amount": [10, 12, 5, 6, 7]})
    text = _joined(data_quality_notes(df))
    assert "exact duplicates" not in text
    assert "Key column 'order_id' has 1 values that appear on more than one row" in text


def test_exact_duplicates_alone_do_not_repeat_the_key_note():
    df = pd.DataFrame({"order_id": ["A1", "A1", "A2", "A3"], "amount": [10, 10, 5, 6]})
    notes = data_quality_notes(df)
    assert len(notes) == 1 and "1 rows are exact duplicates" in notes[0]


def test_non_key_repeats_are_fine():
    # a customer id repeats on purpose across orders
    df = pd.DataFrame({"customer_id": ["c1", "c1", "c2", "c2", "c3", "c1"], "amount": range(6)})
    assert data_quality_notes(df) == []


@pytest.mark.parametrize("values,expected", [
    (["2024-01-01", "2024-02-01", "2024-03-01"], None),                 # ISO text is harmless
    (["Jun 12 1998", "Jan 3 2001", "Dec 25 1999"], "date stored as text"),
    (["2024-01-01", "01/02/2024", "2024-03-01"], "2 different formats"),
])
def test_dates_as_text(values, expected):
    text = _joined(data_quality_notes(pd.DataFrame({"d": values})))
    assert (expected in text) if expected else text == ""


def test_numbers_as_text_detected_without_symbols_too():
    text = _joined(data_quality_notes(pd.DataFrame({"qty": ["10", "20", "1,250", "7"]})))
    assert "'qty' holds numbers stored as text" in text


def test_free_text_is_not_mistaken_for_numbers_or_labels():
    df = pd.DataFrame({"comment": ["great", "ok 5 stars", "Great", "bad"] * 20})
    text = _joined(data_quality_notes(df))
    assert "numbers stored as text" not in text
    assert "'Great', 'great'" in text      # a real case variant is still reported


def test_missing_values_sorted_and_placeholders_counted():
    df = pd.DataFrame({
        "a": [1, None, None, None, 5, 6, 7, 8, 9, 10],
        "b": ["x", "N/A", "y", "z", "w", "v", "u", "t", "s", "r"],
        "c": list(range(10)),
    })
    notes = data_quality_notes(df)
    assert notes == ["Missing values: 'a' 30%, 'b' 10% — say how you handled them (excluded, not counted as 0)."]


def test_notes_are_capped_and_never_raise():
    df = pd.DataFrame({f"c{i}": [f"Mar {i + 1}, 2024", "2024-01-01", "x"] for i in range(20)})
    assert len(data_quality_notes(df)) <= MAX_NOTES
    assert data_quality_notes(pd.DataFrame()) == []
    odd = pd.DataFrame({"id": [[1], [1], [2]], "v": [1, 1, 2]})        # unhashable cells
    data_quality_notes(odd)


def test_notes_appear_in_the_agent_table_context(tmp_path):
    from alidade.agents.context import build_lightweight_table_context
    from alidade.datalake.workspace import Workspace

    ws = Workspace("u", root_dir=tmp_path / "ws")
    ws.write_parquet(_messy(), "sales_orders")
    ws.write_parquet(pd.DataFrame({"k": ["a", "b"], "v": [1, 2]}), "clean")
    text = build_lightweight_table_context([{"name": "sales_orders"}, {"name": "clean"}], ws)
    sales, clean = text.split("clean", 1)[0], text.rsplit("sales_orders", 1)[1]
    assert "Data quality check (automatic, whole table)" in sales
    assert "exact duplicates" in sales
    assert text.count("Data quality check") == 1
