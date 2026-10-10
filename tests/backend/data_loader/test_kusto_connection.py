from unittest.mock import Mock, patch

import pandas as pd
import pytest

from alidade.data_loader.kusto_data_loader import (
    KustoDataLoader,
    _KustoDelegatedCredential,
)
from alidade.data_loader.external_data_loader import ConnectorParamError


def _loader() -> KustoDataLoader:
    loader = object.__new__(KustoDataLoader)
    loader.client = Mock()
    loader.kusto_cluster = "https://example.kusto.windows.net"
    loader.kusto_database = "analytics"
    return loader


def test_connection_uses_direct_sdk_probe() -> None:
    loader = _loader()
    loader.query = Mock(side_effect=AssertionError("query conversion must not run"))

    assert loader.test_connection() is True
    loader.client.execute.assert_called_once_with(
        "analytics",
        ".show tables",
    )
    loader.query.assert_not_called()


def test_connection_returns_false_when_live_probe_fails() -> None:
    loader = _loader()
    loader.client.execute.side_effect = RuntimeError("credential unavailable")

    assert loader.test_connection() is False


def test_database_is_required() -> None:
    params = {
        "kusto_cluster": "https://example.kusto.windows.net",
        "kusto_database": "",
    }

    with pytest.raises(ConnectorParamError, match="kusto_database"):
        KustoDataLoader.validate_params(params)


def test_service_principal_path_requires_complete_credentials() -> None:
    params = {
        "kusto_cluster": "https://example.kusto.windows.net",
        "kusto_database": "analytics",
        "_auth_path": "service_principal",
        "client_id": "client",
    }

    with pytest.raises(ConnectorParamError) as exc_info:
        KustoDataLoader.validate_params(params)

    assert "client_secret" in str(exc_info.value)
    assert "tenant_id" in str(exc_info.value)


def test_ambient_path_does_not_require_service_principal_fields() -> None:
    params = {
        "kusto_cluster": "https://example.kusto.windows.net",
        "kusto_database": "analytics",
        "_auth_path": "ambient",
    }

    KustoDataLoader.validate_params(params)


def test_ambient_is_default_and_no_delegated_sign_in(monkeypatch) -> None:
    # The Kusto Microsoft sign-in gateway was removed: even with the old
    # setting present, no sign-in option or login URL is advertised.
    monkeypatch.setenv("KUSTO_OAUTH_CLIENT_ID", "client")

    paths = KustoDataLoader.auth_paths()

    assert paths[0]["id"] == "ambient"
    assert paths[0]["default"] is True
    assert [p["id"] for p in paths] == ["ambient", "service_principal"]
    assert KustoDataLoader.delegated_login_config() is None


def test_delegated_credential_refreshes_expired_token(monkeypatch) -> None:
    monkeypatch.setenv("KUSTO_OAUTH_CLIENT_ID", "client")
    monkeypatch.setenv("KUSTO_OAUTH_TENANT_ID", "tenant")
    response = Mock(ok=True)
    response.json.return_value = {
        "access_token": "new-access",
        "refresh_token": "new-refresh",
        "expires_in": 3600,
    }
    credential = _KustoDelegatedCredential(
        "https://help.kusto.windows.net",
        "expired-access",
        "refresh",
        0,
    )

    with patch(
        "alidade.data_loader.kusto_data_loader.http.post",
        return_value=response,
    ) as post:
        token = credential.get_token("https://help.kusto.windows.net/.default")

    assert token.token == "new-access"
    assert credential.refresh_token == "new-refresh"
    assert post.call_args.kwargs["data"]["grant_type"] == "refresh_token"


def test_legacy_complete_service_principal_infers_path() -> None:
    params = {
        "kusto_cluster": "https://example.kusto.windows.net",
        "kusto_database": "analytics",
        "client_id": "client",
        "client_secret": "secret",
        "tenant_id": "tenant",
    }

    KustoDataLoader.validate_params(params)

    assert params["_auth_path"] == "service_principal"


def test_database_options_are_loaded_only_on_demand() -> None:
    loader = _loader()
    result = Mock()
    result.primary_results = [Mock()]
    loader.client.execute.return_value = result

    with patch.object(KustoDataLoader, "__init__", return_value=None), \
         patch.object(KustoDataLoader, "client", loader.client, create=True), \
         patch(
             "alidade.data_loader.kusto_data_loader.dataframe_from_result_table",
             return_value=pd.DataFrame({
                 "DatabaseName": ["Sales", "analytics", "Sales", None],
             }),
         ):
        options = KustoDataLoader.discover_param_options(
            "kusto_database",
            {"kusto_cluster": "https://example.kusto.windows.net"},
        )

    assert options == ["analytics", "Sales"]
    loader.client.execute.assert_called_once_with(None, ".show databases")