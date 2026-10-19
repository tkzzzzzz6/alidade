# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Guards for libraries that reach the file system from native code.

The local sandbox confines model-written code with a Python audit hook
(``local_sandbox._warm_worker_loop``). That hook only sees what goes through
the interpreter: ``open()``, ``os.*``, ``socket.*``. Two libraries the
sandbox pre-imports do their own file I/O in C++ and never raise those
events:

* **DuckDB** reads and writes any path (``read_csv('/etc/...')``,
  ``COPY ... TO``) and can download extensions.
* **PyArrow** reads and writes any path (``pyarrow.csv.read_csv``,
  ``pyarrow.parquet.write_table``).

So inside the worker:

* DuckDB connections are opened locked down: external access off, only the
  files that were in the workspace when the code started readable,
  extension loading off, configuration locked; ``COPY ... TO`` /
  ``EXPORT DATABASE`` are refused. Because a locked connection no longer
  finds pandas DataFrames by name ("replacement scans"), DataFrames named in
  a query are registered on the connection first, so
  ``duckdb.sql("SELECT ... FROM df")`` keeps working.
* PyArrow's path-taking readers check the path is inside the workspace, and
  its path-taking writers refuse (writing to in-memory buffers still works).

These are guard rails against mistakes and casual misuse by generated code,
not a boundary against code written to escape: native libraries have other,
rarer ways to the file system. For untrusted code use the Docker sandbox
(``SANDBOX=docker``: no network, read-only workspace, resource limits).
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any, Callable

_ACTIVE = [False]                       # model code is running (set by the worker around exec)
_WORKSPACE: list[str | None] = [None]   # realpath + separator of the current workspace
_WORKSPACE_FILES: list[tuple[str, ...]] = [()]
_MAX_LISTED_FILES = 5000

_WRITE_SQL = re.compile(r"\bcopy\b[\s\S]*?\bto\b|\bexport\s+database\b", re.IGNORECASE)


class SandboxPermissionError(PermissionError):
    pass


def begin(workspace: str | None) -> None:
    """The worker is about to run model code with *workspace* (realpath + separator) as its folder."""
    _WORKSPACE[0] = workspace
    files: list[str] = []
    if workspace:
        for root, _dirs, names in os.walk(workspace):
            for n in names:
                files.append(os.path.join(root, n))
                if len(files) >= _MAX_LISTED_FILES:
                    break
            if len(files) >= _MAX_LISTED_FILES:
                break
    _WORKSPACE_FILES[0] = tuple(files)
    _ACTIVE[0] = True
    refresh_duckdb_default()


def end() -> None:
    _ACTIVE[0] = False


def _pathish(value: Any) -> str | None:
    if isinstance(value, (str, os.PathLike)):
        try:
            p = os.fspath(value)
        except TypeError:
            return None
        if isinstance(p, bytes):
            p = p.decode(errors="replace")
        # URIs (s3://, http://) and in-memory sources are not local paths
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]+://", p) and not p.lower().startswith("file://"):
            raise SandboxPermissionError(f"remote file access forbidden in sandbox: {p}")
        return p[len("file://"):] if p.lower().startswith("file://") else p
    return None


def check_read(value: Any) -> None:
    if not _ACTIVE[0]:
        return
    p = _pathish(value)
    if p is None:
        return
    ws = _WORKSPACE[0]
    if ws is None:
        raise SandboxPermissionError(f"file read forbidden in sandbox (no workspace): {p}")
    resolved = os.path.realpath(p)
    if not (resolved + os.sep).startswith(ws) and not resolved.startswith(ws):
        raise SandboxPermissionError(f"file read outside workspace forbidden: {p}")


def check_write(value: Any) -> None:
    if not _ACTIVE[0]:
        return
    p = _pathish(value)
    if p is not None:
        raise SandboxPermissionError(f"file write forbidden in sandbox: {p}")


# ── PyArrow ──────────────────────────────────────────────────────────────

def _first_arg(args: tuple, kwargs: dict, names: tuple[str, ...]) -> Any:
    if args:
        return args[0]
    for n in names:
        if n in kwargs:
            return kwargs[n]
    return None


def _guard_fn(module: Any, name: str, check: Callable[[Any], None], arg_names: tuple[str, ...],
              arg_index: int = 0) -> None:
    fn = getattr(module, name, None)
    if fn is None or getattr(fn, "__alidade_guarded__", False):
        return

    def guarded(*args, **kwargs):
        target = args[arg_index] if len(args) > arg_index else _first_arg((), kwargs, arg_names)
        check(target)
        return fn(*args, **kwargs)

    guarded.__alidade_guarded__ = True  # type: ignore[attr-defined]
    guarded.__name__ = getattr(fn, "__name__", name)
    guarded.__doc__ = getattr(fn, "__doc__", None)
    setattr(module, name, guarded)


def _guard_class(module: Any, name: str, check: Callable[..., None], arg_names: tuple[str, ...],
                 arg_index: int = 0, mode_aware: bool = False) -> None:
    """Replace a class with a subclass that checks the path before the object is built.

    The check runs in ``__new__``: native (Cython) classes open the file in
    ``__cinit__``, before any ``__init__`` would run.
    """
    cls = getattr(module, name, None)
    if cls is None or getattr(cls, "__alidade_guarded__", False):
        return
    plain_new = cls.__new__ is object.__new__

    def __new__(klass, *args, **kwargs):  # noqa: N807
        target = args[arg_index] if len(args) > arg_index else _first_arg((), kwargs, arg_names)
        if mode_aware:
            mode = args[1] if len(args) > 1 else kwargs.get("mode", "r")
            (check_read if mode in ("r", "rb") else check_write)(target)
        else:
            check(target)
        return object.__new__(klass) if plain_new else cls.__new__(klass, *args, **kwargs)

    try:
        guarded = type(name, (cls,), {"__new__": __new__, "__alidade_guarded__": True,
                                      "__module__": getattr(cls, "__module__", None),
                                      "__doc__": getattr(cls, "__doc__", None)})
    except TypeError:       # a final native class: the module-level helpers are still guarded
        return
    setattr(module, name, guarded)


def install_pyarrow_guards() -> None:
    try:
        import pyarrow as pa
    except ImportError:
        return
    src = ("source", "path", "where", "input_file", "path_or_paths", "file")
    sink = ("where", "sink", "path", "output_file", "dest", "root_path")
    # readers
    for modname, fns in (
        ("pyarrow.parquet", ("read_table", "read_schema", "read_metadata", "read_pandas")),
        ("pyarrow.csv", ("read_csv", "open_csv")),
        ("pyarrow.json", ("read_json",)),
        ("pyarrow.feather", ("read_table", "read_feather")),
        ("pyarrow.orc", ("read_table",)),
        ("pyarrow.dataset", ("dataset", "parquet_dataset")),
        ("pyarrow.ipc", ("open_file", "open_stream")),
        ("pyarrow", ("memory_map", "input_stream")),
    ):
        mod = sys.modules.get(modname)
        if mod is None:
            continue
        for fn in fns:
            _guard_fn(mod, fn, check_read, src)
    pq = sys.modules.get("pyarrow.parquet")
    if pq is not None:
        _guard_class(pq, "ParquetFile", check_read, src)
        _guard_class(pq, "ParquetDataset", check_read, src)
        _guard_class(pq, "ParquetWriter", check_write, sink)
        for fn in ("write_table", "write_to_dataset", "write_metadata"):
            _guard_fn(pq, fn, check_write, sink, arg_index=1)
    for modname, fns, idx in (
        ("pyarrow.csv", ("write_csv",), 1),
        ("pyarrow.feather", ("write_feather",), 1),
        ("pyarrow.orc", ("write_table",), 1),
        ("pyarrow.dataset", ("write_dataset",), 1),
        ("pyarrow.ipc", ("new_file", "new_stream"), 0),
        ("pyarrow", ("output_stream", "create_memory_map"), 0),
    ):
        mod = sys.modules.get(modname)
        if mod is None:
            continue
        for fn in fns:
            _guard_fn(mod, fn, check_write, sink, arg_index=idx)
    csv = sys.modules.get("pyarrow.csv")
    if csv is not None:
        _guard_class(csv, "CSVWriter", check_write, sink)
    _guard_class(pa, "OSFile", check_read, ("path",), mode_aware=True)


# ── DuckDB ───────────────────────────────────────────────────────────────

def _sql_literal(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _lock(con: Any) -> Any:
    files = list(_WORKSPACE_FILES[0])
    if os.sep == "\\":     # let DuckDB match either separator on Windows
        files += [f.replace("\\", "/") for f in files]
    if files:
        con.execute("SET allowed_paths=[" + ",".join(_sql_literal(f) for f in files) + "]")
    con.execute("SET enable_external_access=false")
    con.execute("SET autoinstall_known_extensions=false")
    con.execute("SET autoload_known_extensions=false")
    con.execute("SET lock_configuration=true")
    return con


def _frame_tables(depth: int, query: str) -> dict[str, Any]:
    """DataFrames / Arrow tables visible to the caller and named in *query*.

    *depth*: frames between the function that called ``_prepare`` and the
    user's code (1 = the user called it directly).
    """
    try:
        frame = sys._getframe(depth + 2)
    except ValueError:
        return {}
    names = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", query))
    found: dict[str, Any] = {}
    scopes = (frame.f_locals, frame.f_globals)
    for scope in scopes:
        for name in names:
            if name in found or name not in scope:
                continue
            value = scope[name]
            mod = type(value).__module__ or ""
            if mod.startswith(("pandas", "pyarrow", "polars", "numpy")) and hasattr(value, "__len__"):
                found[name] = value
    return found


class GuardedConnection:
    """A locked DuckDB connection that still finds DataFrames by name."""

    def __init__(self, con: Any):
        object.__setattr__(self, "_con", con)

    def _prepare(self, query: Any, depth: int) -> None:
        if not isinstance(query, str):
            return
        if _WRITE_SQL.search(query):
            raise SandboxPermissionError("writing files from DuckDB (COPY ... TO / EXPORT) is forbidden in sandbox")
        for name, value in _frame_tables(depth, query).items():
            try:
                self._con.register(name, value)
            except Exception:  # noqa: BLE001 — not every object can be registered; DuckDB will say so
                pass

    def sql(self, query, *args, **kwargs):
        self._prepare(query, 1)
        return self._con.sql(query, *args, **kwargs)

    def query(self, query, *args, **kwargs):
        self._prepare(query, 1)
        return self._con.query(query, *args, **kwargs)

    def execute(self, query, *args, **kwargs):
        self._prepare(query, 1)
        out = self._con.execute(query, *args, **kwargs)
        return self if out is self._con else out

    def executemany(self, query, *args, **kwargs):
        self._prepare(query, 1)
        out = self._con.executemany(query, *args, **kwargs)
        return self if out is self._con else out

    def __getattr__(self, name):
        return getattr(self._con, name)

    def __setattr__(self, name, value):
        setattr(self._con, name, value)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._con.close()


_DUCK: dict[str, Any] = {}


def install_duckdb_guards() -> None:
    try:
        import duckdb
    except ImportError:
        return
    if getattr(duckdb, "__alidade_guarded__", False):
        return
    native_connect = duckdb.connect
    _DUCK["connect"] = native_connect

    def connect(database: str = ":memory:", read_only: bool = False, config: dict | None = None, **kwargs):
        if _ACTIVE[0] and (database not in (":memory:", "", None) or config):
            raise SandboxPermissionError("only in-memory DuckDB connections are allowed in sandbox")
        return GuardedConnection(_lock(native_connect(":memory:")))

    duckdb.connect = connect
    inner = getattr(duckdb, "duckdb", None)
    if inner is not None and hasattr(inner, "connect"):
        try:
            inner.connect = connect
        except (AttributeError, TypeError):
            pass

    # module-level helpers (duckdb.sql, duckdb.read_parquet, …) use the default connection
    def default() -> GuardedConnection:
        key = (_WORKSPACE[0] or "", hash(_WORKSPACE_FILES[0]))  # new files → a new allow-list
        con = _DUCK.get(("default",) + key)
        if con is None:
            for k in [k for k in _DUCK if isinstance(k, tuple)]:
                try:
                    _DUCK.pop(k)._con.close()
                except Exception:  # noqa: BLE001
                    pass
            con = GuardedConnection(_lock(native_connect(":memory:")))
            _DUCK[("default",) + key] = con
            if hasattr(duckdb, "set_default_connection"):
                duckdb.set_default_connection(con._con)
        return con

    _DUCK["default"] = default

    def module_level(name: str) -> None:
        native = getattr(duckdb, name, None)
        if native is None:
            return

        def fn(*args, **kwargs):
            con = default()
            query = args[0] if args else kwargs.get("query")
            if name in ("sql", "query", "execute", "query_df", "from_query"):
                con._prepare(query, 1)
            target = getattr(con._con, name, None)
            if target is None:
                return native(*args, **kwargs)
            return target(*args, **kwargs)

        fn.__name__ = name
        setattr(duckdb, name, fn)

    for name in ("sql", "query", "execute", "executemany", "from_query", "read_csv", "read_json",
                 "read_parquet", "from_csv_auto", "from_parquet", "from_df", "from_arrow",
                 "table", "view", "values", "register", "unregister", "fetchall", "fetchone", "fetchdf", "df"):
        module_level(name)
    duckdb.__alidade_guarded__ = True


def refresh_duckdb_default() -> None:
    """After the workspace changed: the next module-level call builds a new default connection."""
    fn = _DUCK.get("default")
    if fn is not None:
        fn()


def install() -> None:
    install_pyarrow_guards()
    install_duckdb_guards()
