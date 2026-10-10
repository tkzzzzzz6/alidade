from __future__ import annotations

import threading

import pyarrow as pa
import pytest

from alidade.data_loader.mysql_data_loader import MySQLDataLoader
from alidade.data_loader.postgresql_data_loader import PostgreSQLDataLoader

pytestmark = [pytest.mark.backend, pytest.mark.plugin]


def test_postgresql_search_catalog_returns_lightweight_tree(monkeypatch):
    loader = PostgreSQLDataLoader.__new__(PostgreSQLDataLoader)
    loader.params = {"database": "analytics"}
    loader.database = "analytics"

    def fake_read_sql_on(query, dbname=None):
        assert "information_schema.tables" in query
        assert "information_schema.columns" not in query
        return pa.table({
            "table_schema": ["public"],
            "table_name": ["orders"],
        })

    monkeypatch.setattr(loader, "_read_sql_on", fake_read_sql_on)

    result = loader.search_catalog("ord", limit=10)

    assert result["truncated"] is False
    assert result["tree"][0]["name"] == "public"
    table = result["tree"][0]["children"][0]
    assert table["name"] == "orders"
    assert table["metadata"]["_source_name"] == "analytics.public.orders"


def test_mysql_search_catalog_returns_lightweight_tree(monkeypatch):
    loader = MySQLDataLoader.__new__(MySQLDataLoader)
    loader.params = {"database": ""}
    loader.database = ""
    loader._lock = threading.Lock()

    def fake_read_sql(query):
        assert "information_schema.tables" in query
        assert "information_schema.columns" not in query
        return pa.table({
            "TABLE_SCHEMA": ["analytics"],
            "TABLE_NAME": ["orders"],
        })

    monkeypatch.setattr(loader, "_read_sql", fake_read_sql)

    result = loader.search_catalog("ord", limit=10)

    assert result["truncated"] is False
    assert result["tree"][0]["name"] == "analytics"
    table = result["tree"][0]["children"][0]
    assert table["name"] == "orders"
    assert table["metadata"]["_source_name"] == "analytics.orders"
