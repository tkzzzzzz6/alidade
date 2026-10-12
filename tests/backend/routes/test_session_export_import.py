"""Unit tests for the export / import session routes.

Verifies that the endpoints correctly route to the specified workspace_id
instead of relying on the X-Workspace-Id header.
"""

from __future__ import annotations

import io
import json
import zipfile
from unittest.mock import MagicMock, patch

import flask
import pytest

from alidade.routes.sessions import session_bp

pytestmark = [pytest.mark.backend]


@pytest.fixture
def app():
    from alidade.error_handler import register_error_handlers

    _app = flask.Flask(__name__)
    _app.config["TESTING"] = True
    _app.register_blueprint(session_bp)
    register_error_handlers(_app)
    return _app


@pytest.fixture
def client(app):
    return app.test_client()


def _make_zip_bytes(state: dict | None = None) -> bytes:
    """Build a minimal session zip with a state.json entry."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("state.json", json.dumps(state or {"tables": []}))
    buf.seek(0)
    return buf.read()


# ── Export ────────────────────────────────────────────────────────────────


class TestExportSession:
    def test_export_uses_workspace_id_from_body(self, client):
        ws = MagicMock()
        zip_buf = io.BytesIO(_make_zip_bytes())
        ws.export_session_zip.return_value = zip_buf

        mgr = MagicMock()
        mgr.workspace_exists.return_value = True
        mgr.open_workspace.return_value = ws

        with (
            patch("alidade.routes.sessions.get_identity_id", return_value="user:alice"),
            patch("alidade.routes.sessions.get_workspace_manager", return_value=mgr),
            patch("alidade.datalake.workspace_manager._strip_sensitive", side_effect=lambda s: s),
        ):
            resp = client.post(
                "/api/sessions/export",
                json={"state": {"tables": []}, "workspace_id": "ws-123"},
            )

        assert resp.status_code == 200
        assert resp.content_type.startswith("application/zip")
        mgr.open_workspace.assert_called_once_with("ws-123", "user:alice")

    def test_export_rejects_missing_workspace_id(self, client):
        with (
            patch("alidade.routes.sessions.get_identity_id", return_value="user:alice"),
        ):
            resp = client.post(
                "/api/sessions/export",
                json={"state": {"tables": []}},
            )

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "error"
        assert body["error"]["code"] == "INVALID_REQUEST"
        assert "workspace_id" in body["error"]["message"]

    def test_export_rejects_missing_state(self, client):
        with (
            patch("alidade.routes.sessions.get_identity_id", return_value="user:alice"),
        ):
            resp = client.post(
                "/api/sessions/export",
                json={"workspace_id": "ws-123"},
            )

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "error"
        assert body["error"]["code"] == "INVALID_REQUEST"

    def test_export_returns_error_for_unknown_workspace(self, client):
        mgr = MagicMock()
        mgr.workspace_exists.return_value = False

        with (
            patch("alidade.routes.sessions.get_identity_id", return_value="user:alice"),
            patch("alidade.routes.sessions.get_workspace_manager", return_value=mgr),
        ):
            resp = client.post(
                "/api/sessions/export",
                json={"state": {"tables": []}, "workspace_id": "nonexistent"},
            )

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "error"
        assert body["error"]["code"] == "TABLE_NOT_FOUND"

# ── Import ────────────────────────────────────────────────────────────────


class TestImportSession:
    def test_import_creates_workspace_when_not_existing(self, client):
        ws = MagicMock()
        ws.import_session_zip.return_value = {
            "tables": [],
            "activeWorkspace": {"id": "ws-new", "displayName": "MySession"},
        }

        mgr = MagicMock()
        mgr.workspace_exists.return_value = False
        mgr.create_and_open_workspace.return_value = ws

        with (
            patch("alidade.routes.sessions.get_identity_id", return_value="user:bob"),
            patch("alidade.routes.sessions.get_workspace_manager", return_value=mgr),
        ):
            data = {"file": (io.BytesIO(_make_zip_bytes()), "session.zip")}
            resp = client.post(
                "/api/sessions/import",
                data={**data, "workspace_id": "ws-new"},
                content_type="multipart/form-data",
            )

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "success"
        mgr.create_and_open_workspace.assert_called_once_with("ws-new", "user:bob")

    def test_import_opens_existing_workspace(self, client):
        ws = MagicMock()
        ws.import_session_zip.return_value = {"tables": []}

        mgr = MagicMock()
        mgr.workspace_exists.return_value = True
        mgr.open_workspace.return_value = ws

        with (
            patch("alidade.routes.sessions.get_identity_id", return_value="user:bob"),
            patch("alidade.routes.sessions.get_workspace_manager", return_value=mgr),
        ):
            data = {"file": (io.BytesIO(_make_zip_bytes()), "session.zip")}
            resp = client.post(
                "/api/sessions/import",
                data={**data, "workspace_id": "ws-existing"},
                content_type="multipart/form-data",
            )

        assert resp.status_code == 200
        mgr.open_workspace.assert_called_once_with("ws-existing", "user:bob")

    def test_import_falls_back_to_active_workspace(self, client):
        ws = MagicMock()
        ws.import_session_zip.return_value = {"tables": []}

        with (
            patch("alidade.routes.sessions.get_identity_id", return_value="user:bob"),
            patch("alidade.routes.sessions.get_workspace", return_value=ws),
        ):
            data = {"file": (io.BytesIO(_make_zip_bytes()), "session.zip")}
            resp = client.post(
                "/api/sessions/import",
                data=data,
                content_type="multipart/form-data",
            )

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "success"

    def test_import_rejects_missing_file(self, client):
        resp = client.post(
            "/api/sessions/import",
            data={"workspace_id": "ws-1"},
            content_type="multipart/form-data",
        )

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "error"
        assert body["error"]["code"] == "INVALID_REQUEST"


# ── Import a bundled demo (read on the server) ────────────────────────────


class TestImportDemo:
    def _mgr(self, state=None):
        ws = MagicMock()
        ws.import_session_zip.return_value = state or {"tables": ["t"]}
        mgr = MagicMock()
        mgr.workspace_exists.return_value = False
        mgr.create_and_open_workspace.return_value = ws
        return mgr, ws

    def test_imports_a_demo_from_the_server_folder(self, client, tmp_path):
        (tmp_path / "demo_movies.zip").write_bytes(_make_zip_bytes())
        mgr, ws = self._mgr()
        with (
            patch("alidade.routes.sessions.get_identity_id", return_value="user:alice"),
            patch("alidade.routes.sessions.get_workspace_manager", return_value=mgr),
            patch("alidade.routes.sessions.demo_dirs", return_value=[tmp_path]),
        ):
            resp = client.post("/api/sessions/import-demo",
                               json={"demo": "/demos/demo_movies.zip", "workspace_id": "ws-9"})
        body = resp.get_json()
        assert body["status"] == "success" and body["data"]["state"] == {"tables": ["t"]}
        mgr.create_and_open_workspace.assert_called_once_with("ws-9", "user:alice")
        assert ws.import_session_zip.call_args.args[0].read()[:2] == b"PK"

    @pytest.mark.parametrize("demo", ["../secrets.zip", "demo_x.txt", "", "demo_missing.zip"])
    def test_rejects_unknown_or_unsafe_names(self, client, tmp_path, demo):
        with (
            patch("alidade.routes.sessions.get_identity_id", return_value="user:alice"),
            patch("alidade.routes.sessions.demo_dirs", return_value=[tmp_path]),
        ):
            body = client.post("/api/sessions/import-demo", json={"demo": demo}).get_json()
        assert body["status"] == "error"

    def test_bundled_demos_are_real_zips(self):
        from pathlib import Path
        demos = Path(__file__).resolve().parents[3] / "public" / "demos"
        zips = sorted(demos.glob("demo_*.zip"))
        assert zips and all(p.read_bytes()[:2] == b"PK" for p in zips)


def test_upload_that_is_not_a_zip_says_so(client):
    with patch("alidade.routes.sessions.get_identity_id", return_value="user:alice"):
        body = client.post(
            "/api/sessions/import",
            data={"file": (io.BytesIO(b""), "x.zip"), "workspace_id": "ws-1"},
            content_type="multipart/form-data",
        ).get_json()
    assert body["status"] == "error"
    assert "not a zip (0 bytes received)" in body["error"]["message"]
