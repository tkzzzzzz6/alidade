# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Attack tests for the local sandbox — each is a thing generated code
must NOT be able to do, written the way an attacker would try it.

The threat model and what each defense is (and is not) covers is in
``docs/sandbox.md``. These tests are the executable version of it: the
audit hook, the native-library guards (DuckDB / PyArrow) and the host's
time / memory limits.

Everything runs against ``LocalSandbox`` / ``SandboxSession`` on the real
worker pool, so a regression that opens a hole fails here.
"""

import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import pandas as pd
import pytest

from alidade.sandbox import LocalSandbox
from alidade.sandbox.local_sandbox import SandboxSession

pytestmark = [pytest.mark.backend]


# ── helpers ────────────────────────────────────────────────────────────────

class _Workspace:
    def __init__(self, path):
        self._path = str(path)

    @contextmanager
    def local_dir(self):
        yield self._path


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "data.csv").write_text("name,value\nA,1\nB,2\n")
    pd.DataFrame({"x": [1, 2, 3]}).to_parquet(tmp_path / "data.parquet")
    return _Workspace(tmp_path)


@pytest.fixture
def outside(tmp_path_factory):
    """A directory the sandbox must never reach, holding a secret."""
    d = tmp_path_factory.mktemp("outside")
    (d / "secret.txt").write_text("TOPSECRET")
    (d / "secret.csv").write_text("s\nTOPSECRET\n")
    pd.DataFrame({"s": ["TOPSECRET"]}).to_parquet(d / "secret.parquet")
    (d / "victim.txt").write_text("keep me")
    return d


def run(ws, code):
    return LocalSandbox().run_python_code(
        code + "\nimport pandas as pd\noutput_df = pd.DataFrame({'ok': [1]})", ws, "output_df")


def blocked(ws, code):
    return run(ws, code)["status"] == "error"


def leaked(result) -> bool:
    """A read attack succeeds if the secret shows up in the output."""
    return "TOPSECRET" in str(result.get("content", ""))


# ── 1. runaway resource use ─────────────────────────────────────────────────

class TestResourceLimits:
    def test_infinite_loop_is_stopped_by_the_time_limit(self, ws, monkeypatch):
        monkeypatch.setenv("ALIDADE_SANDBOX_TIMEOUT", "2")
        with SandboxSession() as s:
            t0 = time.monotonic()
            r = s.execute("while True:\n    pass", {}, ws._path)
            elapsed = time.monotonic() - t0
        assert r["status"] == "error" and r.get("limit") == "timeout"
        assert elapsed < 10          # stopped near the 2 s limit, not left running

    def test_busy_c_loop_is_stopped_too(self, ws, monkeypatch):
        # a tight loop inside numpy holds the GIL; the host watchdog, not a
        # Python-level check, is what stops it
        monkeypatch.setenv("ALIDADE_SANDBOX_TIMEOUT", "2")
        with SandboxSession() as s:
            r = s.execute("import numpy as np\nwhile True:\n    np.linalg.inv(np.random.rand(200, 200))",
                          {}, ws._path)
        assert r["status"] == "error" and r.get("limit") == "timeout"

    def test_memory_bomb_is_stopped_by_the_memory_limit(self, ws, monkeypatch):
        monkeypatch.setenv("ALIDADE_SANDBOX_MEMORY_MB", "512")
        with SandboxSession() as s:
            r = s.execute("blocks = []\nwhile True:\n    blocks.append(bytearray(20_000_000))",
                          {}, ws._path)
        assert r["status"] == "error" and r.get("limit") == "memory"

    def test_session_recovers_after_a_limit_kill(self, ws, monkeypatch):
        monkeypatch.setenv("ALIDADE_SANDBOX_TIMEOUT", "2")
        with SandboxSession() as s:
            assert s.execute("while True:\n    pass", {}, ws._path)["status"] == "error"
            # the next statement runs in a fresh worker; the caller is told variables are gone
            r = s.execute("import pandas as pd\nout = pd.DataFrame({'a': [1]})",
                          {"out": None}, ws._path)
            assert r["status"] == "ok"
            assert isinstance(r["allowed_objects"]["out"], pd.DataFrame)
            assert s.restarts == 1

    def test_the_kill_message_tells_the_model_what_happened(self, ws, monkeypatch):
        monkeypatch.setenv("ALIDADE_SANDBOX_TIMEOUT", "2")
        with SandboxSession() as s:
            msg = s.execute("while True:\n    pass", {}, ws._path)["error_message"]
        assert "timed out" in msg and "restarted" in msg.lower()


# ── 2. reading files outside the workspace ───────────────────────────────────

class TestFileReadConfinement:
    def test_open_read(self, ws, outside):
        assert not leaked(run(ws, f"print(open(r'{outside}/secret.txt').read())"))

    def test_os_open_read(self, ws, outside):
        assert blocked(ws, f"import os\nfd = os.open(r'{outside}/secret.txt', os.O_RDONLY)\nprint(os.read(fd, 100))")

    def test_pandas_read_csv(self, ws, outside):
        assert not leaked(run(ws, f"import pandas as pd\nprint(pd.read_csv(r'{outside}/secret.csv'))"))

    def test_pandas_read_parquet(self, ws, outside):
        assert not leaked(run(ws, f"import pandas as pd\nprint(pd.read_parquet(r'{outside}/secret.parquet'))"))

    def test_pyarrow_read_table(self, ws, outside):
        assert not leaked(run(ws, f"import pyarrow.parquet as pq\nprint(pq.read_table(r'{outside}/secret.parquet'))"))

    def test_pyarrow_csv(self, ws, outside):
        assert not leaked(run(ws, f"import pyarrow.csv as pc\nprint(pc.read_csv(r'{outside}/secret.csv'))"))

    def test_pyarrow_dataset(self, ws, outside):
        assert not leaked(run(ws, f"import pyarrow.dataset as ds\nprint(ds.dataset(r'{outside}').to_table())"))

    def test_duckdb_read_csv(self, ws, outside):
        assert not leaked(run(ws, f"import duckdb\nprint(duckdb.sql(\"SELECT * FROM read_csv('{outside}/secret.csv')\").fetchall())"))

    def test_duckdb_read_parquet(self, ws, outside):
        assert not leaked(run(ws, f"import duckdb\nprint(duckdb.read_parquet(r'{outside}/secret.parquet').fetchall())"))

    def test_duckdb_fresh_connection(self, ws, outside):
        assert not leaked(run(ws, f"import duckdb\ncon = duckdb.connect()\nprint(con.sql(\"SELECT * FROM read_csv('{outside}/secret.csv')\").fetchall())"))

    def test_listing_an_outside_directory(self, ws, outside):
        assert not leaked(run(ws, f"import os\nprint(os.listdir(r'{outside}'))"))

    def test_glob_outside(self, ws, outside):
        assert not leaked(run(ws, f"import glob\nprint(glob.glob(r'{outside}/*'))"))

    def test_path_traversal_out_of_workspace(self, ws, outside):
        rel = os.path.relpath(outside / "secret.txt", ws._path)
        assert not leaked(run(ws, f"print(open(r'{rel}').read())"))

    def test_workspace_files_still_readable(self, ws):
        # the confinement must not break legitimate reads
        r = run(ws, "import pandas as pd\nprint(pd.read_csv('data.csv').shape)\nprint(pd.read_parquet('data.parquet').shape)")
        assert r["status"] == "ok"


# ── 3. writing / changing files ──────────────────────────────────────────────

class TestFileWriteBlocked:
    @pytest.mark.parametrize("code", [
        "open('evil.txt', 'w').write('x')",
        "import os\nfd = os.open('evil.txt', os.O_WRONLY | os.O_CREAT)\nos.write(fd, b'x')",
        "import pandas as pd\npd.DataFrame({'a': [1]}).to_csv('evil.csv')",
        "import pandas as pd\npd.DataFrame({'a': [1]}).to_parquet('evil.parquet')",
        "import numpy as np\nnp.save('evil.npy', np.zeros(3))",
        "import pyarrow as pa, pyarrow.parquet as pq\npq.write_table(pa.table({'a': [1]}), 'evil.parquet')",
        "import pyarrow as pa, pyarrow.csv as pc\npc.write_csv(pa.table({'a': [1]}), 'evil.csv')",
    ])
    def test_write_into_workspace_blocked(self, ws, code):
        assert blocked(ws, code)
        assert not (Path(ws._path) / "evil.txt").exists()
        assert not (Path(ws._path) / "evil.csv").exists()
        assert not (Path(ws._path) / "evil.parquet").exists()

    def test_duckdb_copy_to_file_blocked(self, ws):
        assert blocked(ws, "import duckdb\nduckdb.sql(\"COPY (SELECT 1) TO 'evil.csv'\")")
        assert not (Path(ws._path) / "evil.csv").exists()

    def test_duckdb_export_database_blocked(self, ws):
        assert blocked(ws, "import duckdb\nduckdb.sql(\"EXPORT DATABASE 'evil_dir'\")")

    def test_delete_a_file_outside(self, ws, outside):
        victim = outside / "victim.txt"
        assert blocked(ws, f"import os\nos.remove(r'{victim}')")
        assert victim.exists()

    def test_rename_a_file_outside(self, ws, outside):
        assert blocked(ws, f"import os\nos.rename(r'{outside}/victim.txt', r'{outside}/moved.txt')")
        assert (outside / "victim.txt").exists()

    def test_make_a_directory_outside(self, ws, outside):
        assert blocked(ws, f"import os\nos.mkdir(r'{outside}/newdir')")
        assert not (outside / "newdir").exists()


# ── 4. running other programs ────────────────────────────────────────────────

class TestNoSubprocesses:
    @pytest.mark.parametrize("code", [
        "import os\nos.system('touch {probe}')",
        "import os\nos.popen('touch {probe}').read()",
        "import os\nos.execvp('touch', ['touch', '{probe}'])",
        "import os\nos.spawnlp(os.P_WAIT, 'touch', 'touch', '{probe}')",
        "import os\nos.posix_spawn('/usr/bin/touch', ['touch', '{probe}'], {{}})",
        "import subprocess\nsubprocess.run(['touch', '{probe}'])",
        "import subprocess\nsubprocess.Popen(['touch', '{probe}'])",
        "os = __import__('os')\nos.system('touch {probe}')",
        "import sys\nsys.modules['os'].system('touch {probe}')",
        "import _posixsubprocess",
    ])
    def test_spawning_a_process_blocked(self, ws, tmp_path, code):
        probe = tmp_path / "pwned"
        assert blocked(ws, code.format(probe=probe))
        assert not probe.exists()


# ── 5. network ───────────────────────────────────────────────────────────────

class TestNoNetwork:
    @pytest.mark.parametrize("code", [
        "import socket\ns = socket.socket()\ns.connect(('1.1.1.1', 80))",
        "import socket\ns = socket.socket()\ns.bind(('0.0.0.0', 9999))",
        "import socket\nsocket.getaddrinfo('example.com', 80)",
        "import socket\nsocket.create_connection(('1.1.1.1', 80))",
        "import urllib.request\nurllib.request.urlopen('http://example.com')",
        "import http.client\nhttp.client.HTTPConnection('example.com').connect()",
        "import _socket\ns = _socket.socket()\ns.connect(('1.1.1.1', 80))",
        "import duckdb\nduckdb.sql(\"SELECT * FROM read_csv('https://example.com/a.csv')\").fetchall()",
    ])
    def test_outbound_connection_blocked(self, ws, code):
        assert blocked(ws, code)


# ── 6. privilege / escape attempts ───────────────────────────────────────────

class TestEscapeAttempts:
    def test_ctypes_is_blocked(self, ws):
        assert blocked(ws, "import ctypes\nlibc = ctypes.CDLL(None)")

    def test_environment_secrets_are_scrubbed(self, ws, monkeypatch):
        monkeypatch.setenv("ALIDADE_TEST_API_KEY", "sk-secret")
        monkeypatch.setenv("ALIDADE_TEST_PASSWORD", "hunter2")
        r = run(ws, "import os\nprint([k for k in os.environ if 'KEY' in k.upper() or 'PASSWORD' in k.upper()])")
        assert r["status"] == "ok"
        assert "sk-secret" not in str(r["content"]) and "hunter2" not in str(r["content"])

    def test_limits_hold_even_if_code_raises_its_own_rlimits(self, ws, monkeypatch):
        # the time / memory limits are enforced by the host watching from
        # outside, so code raising its own RLIMITs cannot escape them
        monkeypatch.setenv("ALIDADE_SANDBOX_TIMEOUT", "2")
        with SandboxSession() as s:
            r = s.execute(
                "try:\n"
                "    import resource\n"
                "    resource.setrlimit(resource.RLIMIT_CPU, (resource.RLIM_INFINITY,) * 2)\n"
                "except Exception:\n"
                "    pass\n"
                "while True:\n"
                "    pass",
                {}, ws._path)
        assert r["status"] == "error" and r.get("limit") == "timeout"

    def test_duckdb_cannot_reenable_external_access(self, ws, outside):
        code = (f"import duckdb\ncon = duckdb.connect()\n"
                f"con.execute('SET enable_external_access=true')\n"
                f"print(con.sql(\"SELECT * FROM read_csv('{outside}/secret.csv')\").fetchall())")
        assert not leaked(run(ws, code))

    def test_duckdb_cannot_install_extensions(self, ws):
        assert blocked(ws, "import duckdb\nduckdb.sql('INSTALL httpfs')")


# ── 7. the sandbox stays usable for real work ────────────────────────────────

class TestLegitimateWorkStillWorks:
    def test_pandas_transform(self, ws):
        r = run(ws, "import pandas as pd\ndf = pd.read_csv('data.csv')\nprint(df['value'].sum())")
        assert r["status"] == "ok"

    def test_duckdb_over_a_workspace_file(self, ws):
        r = run(ws, "import duckdb\nprint(duckdb.sql(\"SELECT COUNT(*) FROM 'data.parquet'\").fetchall())")
        assert r["status"] == "ok"

    def test_duckdb_over_a_dataframe(self, ws):
        # a locked connection still resolves DataFrames named in the query
        r = run(ws, "import duckdb, pandas as pd\n"
                    "df = pd.DataFrame({'z': [1, 2, 3, 4]})\n"
                    "print(duckdb.sql('SELECT SUM(z) FROM df').fetchall())")
        assert r["status"] == "ok"

    def test_numpy_and_sklearn(self, ws):
        r = run(ws, "import numpy as np\nfrom sklearn.linear_model import LinearRegression\n"
                    "m = LinearRegression().fit(np.arange(10).reshape(-1, 1), np.arange(10))\n"
                    "print(round(m.coef_[0], 2))")
        assert r["status"] == "ok"

    def test_writing_to_an_in_memory_buffer(self, ws):
        # writing to a BytesIO buffer (not a file) is fine and common
        r = run(ws, "import io, pyarrow as pa, pyarrow.parquet as pq\n"
                    "buf = pa.BufferOutputStream()\npq.write_table(pa.table({'a': [1]}), buf)\n"
                    "print(len(buf.getvalue()))")
        assert r["status"] == "ok"
