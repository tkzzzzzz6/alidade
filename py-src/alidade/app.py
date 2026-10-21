# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import argparse
import sys
import os
import warnings
import mimetypes
mimetypes.add_type('application/javascript', '.js')
mimetypes.add_type('application/javascript', '.mjs')

# Suppress a noisy pydantic serializer warning emitted by litellm/openai
# when a Chat Completions ``usage`` dict (prompt_tokens / completion_tokens)
# gets serialized through a model whose field is typed as the Responses
# API ``ResponseAPIUsage``. The response itself is correct (HTTP 200);
# only the serializer's "may not be as expected" notice is misleading.
warnings.filterwarnings(
    "ignore",
    message=r"Pydantic serializer warnings",
    category=UserWarning,
    module=r"pydantic\..*",
)

import flask
from flask import Flask, request, send_from_directory
from flask import stream_with_context, Response

import webbrowser
import threading
import numpy as np
import time

import logging

import json
from pathlib import Path

from dotenv import load_dotenv
import secrets
import base64

APP_ROOT = Path(Path(__file__).parent).absolute()

# Load env files early so FLASK_SECRET_KEY is available before app setup.
load_dotenv(os.path.join(APP_ROOT, "..", "..", '.env'))
load_dotenv(os.path.join(APP_ROOT, '.env'))

from alidade import langsmith_tracing  # noqa: E402
langsmith_tracing.configure()               # ALIDADE_LANGSMITH=1 → LangSmith tracing

# .env files written before the rename use the old variable names (see alidade.legacy).
from alidade.legacy import apply_legacy_env, default_home  # noqa: E402
apply_legacy_env()

# Create Flask app (lightweight, no heavy imports yet)
app = Flask(__name__, static_url_path='', static_folder=os.path.join(APP_ROOT, "dist"))
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(16)
app.json.sort_keys = False
app.json.ensure_ascii = False
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500 MB
app.config['PERMANENT_SESSION_LIFETIME'] = 60 * 60 * 24 * 365
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# Server-side session via flask-session (filesystem / cachelib backend).
# Stores SSO tokens + service tokens without hitting the 4 KB cookie limit.
_data_home = os.environ.get(
    'ALIDADE_HOME',
    str(default_home()),
)
_session_dir = os.path.join(_data_home, 'sessions')
os.makedirs(_session_dir, exist_ok=True)

app.config['SESSION_TYPE'] = 'cachelib'
app.config['SESSION_PERMANENT'] = True
app.config['SESSION_USE_SIGNER'] = True
app.config['SESSION_KEY_PREFIX'] = 'df_session:'
app.config['SESSION_CLEANUP_N_REQUESTS'] = 100

try:
    from cachelib import FileSystemCache
    app.config['SESSION_CACHELIB'] = FileSystemCache(
        cache_dir=_session_dir, threshold=500,
    )
    from flask_session import Session
    Session(app)
except ImportError:
    logging.getLogger(__name__).warning(
        "flask-session not installed; falling back to cookie sessions "
        "(TokenStore features will be limited)"
    )

class CustomJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.int64):
            return int(obj)
        if isinstance(obj, (bytes, bytearray)):
            return base64.b64encode(obj).decode('ascii')
        return super().default(obj)

app.json_encoder = CustomJSONEncoder

# Default config from env (can be overridden by CLI args)
# DISABLE_DATABASE=true is a convenience preset for multi-user anonymous deployments.
# It bundles: ephemeral workspace + no data connectors + no custom models + hide keys.
_disable_database = os.environ.get('DISABLE_DATABASE', 'false').lower() == 'true'
_default_ws_backend = os.environ.get('WORKSPACE_BACKEND', 'local')
if _disable_database and _default_ws_backend == 'local':
    _default_ws_backend = 'ephemeral'
app.config['CLI_ARGS'] = {
    'host': os.environ.get('HOST', '127.0.0.1'),
    'sandbox': os.environ.get('SANDBOX', 'local'),
    'disable_display_keys': _disable_database or os.environ.get('DISABLE_DISPLAY_KEYS', 'false').lower() == 'true',
    'disable_data_connectors': _disable_database or os.environ.get('DISABLE_DATA_CONNECTORS', 'false').lower() == 'true',
    'disable_custom_models': _disable_database or os.environ.get('DISABLE_CUSTOM_MODELS', 'false').lower() == 'true',
    'max_display_rows': int(os.environ.get('MAX_DISPLAY_ROWS', '10000')),
    'scratch_max_bytes': int(os.environ.get('SCRATCH_MAX_SIZE_MB', '1024')) * 1024 * 1024,
    'scratch_max_file_bytes': int(os.environ.get('SCRATCH_MAX_FILE_SIZE_MB', '20')) * 1024 * 1024,
    'data_dir': os.environ.get('ALIDADE_HOME', None),
    'dev': os.environ.get('DEV_MODE', 'false').lower() == 'true',
    'workspace_backend': _default_ws_backend,
    'available_languages': [
        lang.strip() for lang in os.environ.get('AVAILABLE_LANGUAGES', 'en,zh').split(',') if lang.strip()
    ],
}

# Get logger for this module (logging config moved to run_app function)
logger = logging.getLogger(__name__)

_LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
# Marker attribute so we never attach the persistent file handler twice
# (configure_logging runs early; run_app re-checks once --data-dir is known).
_FILE_HANDLER_MARKER = '_df_persistent_file_handler'


def _resolve_data_home() -> Path:
    """Resolve the Alidade home directory for logging.

    Mirrors ``get_alidade_home()`` but is safe to call outside an
    application context (logging is configured before the app runs).
    Order: CLI_ARGS['data_dir'] > ALIDADE_HOME env > ~/.alidade.
    """
    data_dir = app.config.get('CLI_ARGS', {}).get('data_dir')
    if data_dir:
        return Path(data_dir)
    env_home = os.getenv('ALIDADE_HOME')
    if env_home:
        return Path(env_home)
    return default_home()


def configure_file_logging() -> None:
    """Attach a rotating file handler that persists all logs under
    ``<ALIDADE_HOME>/logs/alidade.log``.

    This is the artifact users can send when reporting problems. Output is
    passed through :class:`SensitiveDataFilter` so API keys / tokens are
    redacted. The handler is idempotent — if the resolved path changes
    (e.g. ``--data-dir`` supplied after early configuration) the old handler
    is replaced.
    """
    from logging.handlers import RotatingFileHandler
    from alidade.security.log_sanitizer import SensitiveDataFilter

    try:
        log_dir = _resolve_data_home() / 'logs'
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / 'alidade.log'
    except Exception as exc:  # pragma: no cover - filesystem edge cases
        logging.getLogger('alidade').warning(
            "Could not set up persistent file logging: %s", exc,
        )
        return

    root = logging.getLogger()

    # Skip if an identical handler is already attached; otherwise drop any
    # previous persistent handler so we don't duplicate output.
    for h in list(root.handlers):
        if getattr(h, _FILE_HANDLER_MARKER, False):
            if getattr(h, 'baseFilename', None) == str(log_path.resolve()):
                app.config['LOG_FILE_PATH'] = str(log_path)
                return
            root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass

    handler = RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding='utf-8',
    )
    setattr(handler, _FILE_HANDLER_MARKER, True)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    handler.addFilter(SensitiveDataFilter())
    root.addHandler(handler)

    # Mirror onto the Flask app logger too (its handlers are managed separately).
    if not any(getattr(h, _FILE_HANDLER_MARKER, False) for h in app.logger.handlers):
        app.logger.addHandler(handler)

    app.config['LOG_FILE_PATH'] = str(log_path)
    logging.getLogger('alidade').info(
        "Persistent logs: %s", log_path,
    )


def configure_logging():
    """Configure logging for the Flask application."""
    log_level_str = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    app_log_level = getattr(logging, log_level_str, logging.INFO)

    from alidade.security.log_sanitizer import SensitiveDataFilter

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(SensitiveDataFilter())

    logging.basicConfig(
        level=logging.WARNING,
        format=_LOG_FORMAT,
        handlers=[handler],
    )

    logging.getLogger('alidade').setLevel(app_log_level)

    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('litellm').setLevel(logging.WARNING)
    logging.getLogger('openai').setLevel(logging.WARNING)

    app.logger.handlers = []
    for h in logging.getLogger().handlers:
        app.logger.addHandler(h)

    # Persist everything to a file under the DF home so users can send logs.
    configure_file_logging()

    logging.getLogger('alidade').info(
        "Log level: %s (sanitize=%s)", log_level_str,
        os.getenv("LOG_SANITIZE", "true"),
    )


_blueprints_registered = False

def _register_blueprints():
    """
    Import and register blueprints. This is where heavy imports happen.
    Called at module level (for gunicorn) and from run_app() (for CLI).
    Guarded to prevent double registration.
    """
    global _blueprints_registered
    if _blueprints_registered:
        return
    _blueprints_registered = True

    # Register unified error handlers and request-id middleware
    from alidade.error_handler import register_error_handlers
    register_error_handlers(app)

    from alidade._startup_spinner import spinner

    # Import tables routes (imports database connectors)
    with spinner("Loading data loader drivers"):
        from alidade.routes.tables import tables_bp

    # Import agent routes (imports AI/ML libraries: litellm, sklearn, etc.)
    with spinner("Loading AI agents"):
        from alidade.routes.agents import agent_bp
    
    # Import session routes
    from alidade.routes.sessions import session_bp

    # Import demo stream routes
    from alidade.routes.demo_stream import demo_stream_bp, limiter as demo_stream_limiter
    demo_stream_limiter.init_app(app)

    # Import server-log inspection routes (local-mode gated)
    from alidade.routes.logs import logs_bp
    from alidade.routes.model_endpoints import model_endpoints_bp

    # Register blueprints
    app.register_blueprint(tables_bp)
    app.register_blueprint(agent_bp)
    app.register_blueprint(session_bp)
    app.register_blueprint(demo_stream_bp)
    app.register_blueprint(logs_bp)
    app.register_blueprint(model_endpoints_bp)

    # Initialise pluggable authentication (reads AUTH_PROVIDER env var)
    from alidade.auth.identity import init_auth, get_active_provider
    init_auth(app)

    # Register auth gateway blueprints for stateful providers (e.g. GitHub OAuth)
    provider = get_active_provider()
    if provider and provider.name == "github":
        from alidade.auth.gateways.github_gateway import github_bp
        app.register_blueprint(github_bp)

    # Register auth token management routes (always active)
    from alidade.auth.gateways.oidc_gateway import auth_tokens_bp
    app.register_blueprint(auth_tokens_bp)

    # Register backend OIDC gateway (auto-detected: active when OIDC_CLIENT_SECRET is set)
    from alidade.auth.providers.oidc import is_backend_oidc_mode
    if is_backend_oidc_mode():
        from alidade.auth.gateways.oidc_gateway import oidc_bp, oidc_callback_bp
        app.register_blueprint(oidc_bp)
        app.register_blueprint(oidc_callback_bp)

    # Register credential vault API (safe even when vault is not configured)
    from alidade.routes.credentials import credential_bp
    app.register_blueprint(credential_bp)

    # Register knowledge management API (rules, skills, workflows)
    from alidade.routes.knowledge import knowledge_bp
    app.register_blueprint(knowledge_bp)

    # Run traces (what each analyst run did, for the trace viewer)
    from alidade.routes.traces import traces_bp
    app.register_blueprint(traces_bp)

    # Auto-register all installed data loaders as DataConnector instances.
    # We always run this so the connectors blueprint and the built-in
    # 'sample_datasets' connector are available; the function itself
    # honors disable_data_connectors by skipping admin YAML/env specs.
    with spinner("Loading data connectors"):
        from alidade.data_connector import register_data_connectors
        register_data_connectors(app)
    if app.config['CLI_ARGS'].get('disable_data_connectors'):
        print("  External data connectors disabled (DISABLE_DATA_CONNECTORS=true) - sample datasets remain available", flush=True)


def _safety_checks():
    """Warn about dangerous configuration combinations at startup."""
    cli = app.config.get('CLI_ARGS', {})
    backend = cli.get('workspace_backend', 'local')
    sandbox = cli.get('sandbox', 'not_a_sandbox')
    multi_user = backend != 'local'

    if multi_user and sandbox == 'not_a_sandbox':
        logger.critical(
            "SECURITY WARNING: Multi-user mode with no sandbox is dangerous. "
            "LLM-generated code can read/write arbitrary files on the server. "
            "Set SANDBOX=docker or SANDBOX=local for production deployments."
        )


# Register blueprints at module level so WSGI servers (gunicorn) pick up all routes.
# The guard inside _register_blueprints() prevents double registration when run via CLI.
_register_blueprints()
_safety_checks()


@app.route("/", defaults={"path": ""})
def index_alt(path):
    logger.info(app.static_folder)
    return send_from_directory(app.static_folder, "index.html")

@app.route('/api/auth/info', methods=['GET'])
def get_auth_info():
    """Return authentication configuration for the frontend.

    The response tells the frontend how to initiate login based on the
    active provider (OIDC PKCE, GitHub redirect, transparent, or none).
    """
    from alidade.auth.identity import get_active_provider
    from alidade.error_handler import json_ok
    provider = get_active_provider()
    if provider:
        return json_ok(provider.get_auth_info())
    return json_ok({"action": "none"})


@app.route('/api/app-config', methods=['GET'])
def get_app_config():
    """Provide frontend configuration settings from CLI arguments"""
    args = app.config['CLI_ARGS']
    
    workspace_backend = args.get('workspace_backend', 'local')
    config = {
        "SANDBOX": args['sandbox'],
        "DISABLE_DISPLAY_KEYS": args['disable_display_keys'],
        "DISABLE_DATA_CONNECTORS": args.get('disable_data_connectors', False),
        "DISABLE_CUSTOM_MODELS": args.get('disable_custom_models', False),
        "MAX_DISPLAY_ROWS": args['max_display_rows'],
        "DEV_MODE": args.get('dev', False),
        "WORKSPACE_BACKEND": workspace_backend,
        "AVAILABLE_LANGUAGES": args.get('available_languages', ['en', 'zh']),
    }

    from alidade.auth.identity import is_local_mode
    config["IS_LOCAL_MODE"] = is_local_mode()

    if workspace_backend == 'local':
        from alidade.datalake.workspace import get_alidade_home
        config["ALIDADE_HOME"] = str(get_alidade_home())

    from alidade.auth.identity import get_active_provider
    provider = get_active_provider()
    if provider:
        config["AUTH_PROVIDER"] = provider.name
        config["AUTH_INFO"] = provider.get_auth_info()

    # Return the server-assigned identity so the frontend can use it.
    # For localhost mode this is the fixed local:<os_username> identity;
    # for anonymous mode the server echoes back the browser-provided UUID.
    identity = None
    try:
        from alidade.auth.identity import get_identity_id
        identity = get_identity_id()
        id_type, _, id_value = identity.partition(':')
        config["IDENTITY"] = {"type": id_type, "id": id_value}
    except Exception:
        pass  # No identity available (e.g. during startup)

    # Expose credential vault availability to the frontend
    from alidade.auth.vault import get_credential_vault
    config["CREDENTIAL_VAULT_ENABLED"] = get_credential_vault() is not None

    # Expose data connectors to the frontend
    from alidade.data_connector import _public_connector_id, _visible_connector_items
    visible_connectors = _visible_connector_items(identity)
    if visible_connectors:
        connectors_info: list[dict] = []
        for registry_key, src, _is_admin in visible_connectors:
            connectors_info.append(src.get_frontend_config())
            connectors_info[-1]["source_id"] = _public_connector_id(registry_key, src)
        config["CONNECTORS"] = connectors_info

    # Tell the frontend which connectors the current user has vault credentials for
    # so it can render "Connected" vs "Available" without N status calls.
    if identity:
        connected_ids: list[str] = []
        for registry_key, src, _is_admin in visible_connectors:
            if src.has_stored_credentials(identity) or src._get_loader(identity) is not None:
                connected_ids.append(_public_connector_id(registry_key, src))
        if connected_ids:
            config["CONNECTED_CONNECTORS"] = connected_ids

    # Expose disabled data sources (missing deps) so UI can show greyed-out entries
    from alidade.data_loader import DISABLED_LOADERS
    if DISABLED_LOADERS:
        config["DISABLED_SOURCES"] = {
            name: {"install_hint": hint}
            for name, hint in DISABLED_LOADERS.items()
        }

    from alidade.error_handler import json_ok
    return json_ok(config)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Alidade")
    parser.add_argument("-p", "--port", type=int, default=5567, help="The port number you want to use")
    parser.add_argument("--host", type=str, default=os.environ.get('HOST', '127.0.0.1'),
        help="Network interface to bind to (default: 127.0.0.1). "
             "Use 0.0.0.0 to accept connections from other machines.")
    parser.add_argument("--sandbox", type=str, default=os.environ.get('SANDBOX', 'local'),
        choices=['local', 'docker'],
        help="Python code execution backend: 'local' (default, isolated subprocess with audit hooks), "
             "'docker' (maximum isolation, requires Docker)")
    parser.add_argument("--disable-display-keys", action='store_true', default=False,
        help="Whether disable displaying keys in the frontend UI, recommended to turn on if you host the app not just for yourself.")
    parser.add_argument("--disable-database", action='store_true', default=False,
        help="Multi-user anonymous preset: enables ephemeral workspace, disables data connectors, "
             "disables custom LLM endpoints, and hides API keys. Equivalent to setting "
             "--workspace-backend=ephemeral --disable-data-connectors --disable-custom-models --disable-display-keys.")
    parser.add_argument("--disable-data-connectors", action='store_true', default=False,
        help="Disable external data connectors (MySQL, PostgreSQL, etc.). "
             "Recommended for multi-user anonymous deployments to prevent credential exposure.")
    parser.add_argument("--disable-custom-models", action='store_true', default=False,
        help="Prevent users from adding custom LLM endpoints via the UI. "
             "Only server-configured models will be available.")
    parser.add_argument("--max-display-rows", type=int,
        default=int(os.environ.get('MAX_DISPLAY_ROWS', '10000')),
        help="Maximum number of rows to send to the frontend for display (default: 10000)")
    parser.add_argument("--scratch-max-size-mb", type=int,
        default=int(os.environ.get('SCRATCH_MAX_SIZE_MB', '1024')),
        help="Max total size (MB) of a workspace's agent scratch dir before LRU eviction (default: 1024)")
    parser.add_argument("--scratch-max-file-size-mb", type=int,
        default=int(os.environ.get('SCRATCH_MAX_FILE_SIZE_MB', '20')),
        help="Max size (MB) of a single agent-written scratch file, e.g. a fetch_url download (default: 20)")
    parser.add_argument("--data-dir", type=str, default=None,
        help="Alidade home directory for workspaces and sessions (default: ~/.alidade)")
    parser.add_argument("--dev", action='store_true', default=False,
        help="Launch the app in development mode (prevents the app from opening the browser automatically)")
    parser.add_argument("--desktop", action='store_true',
        default=os.environ.get('ALIDADE_DESKTOP') == '1',
        help="Open Alidade in a native desktop window (pywebview) instead of a browser tab. "
             "Requires the 'desktop' extra: pip install \"alidade[desktop]\".")
    parser.add_argument("--workspace-backend", type=str,
        default=os.environ.get('WORKSPACE_BACKEND', 'local'),
        choices=['local', 'ephemeral'],
           help="Workspace storage backend: 'local' (default, durable filesystem without TTL cleanup) "
               "or 'ephemeral' (temporary server-local storage with TTL/LRU cleanup)")
    return parser.parse_args()


def _wait_for_server(host: str, port: int, timeout: float = 20.0) -> bool:
    """Block until the local server accepts a TCP connection (or timeout)."""
    import socket
    connect_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            if s.connect_ex((connect_host, port)) == 0:
                return True
        time.sleep(0.2)
    return False


def _run_desktop(url: str, args: argparse.Namespace) -> None:
    """Serve Flask on a background thread and show a native pywebview window.

    pywebview must own the main thread (required on macOS), so the WSGI server
    runs in a daemon thread. When the window closes, webview.start() returns,
    the main thread ends, the daemon server dies with it, and the process exits.
    """
    try:
        import webview  # pywebview
    except ImportError:
        print(
            "Desktop mode needs pywebview, which is not installed.\n"
            "  Install it:   pip install \"alidade[desktop]\"\n"
            "  Or drop --desktop to open Alidade in your browser instead.",
            file=sys.stderr, flush=True,
        )
        raise SystemExit(1)

    server = threading.Thread(
        target=lambda: app.run(
            host=args.host, port=args.port,
            debug=False, use_reloader=False, threaded=True,
        ),
        daemon=True,
    )
    server.start()

    if not _wait_for_server(args.host, args.port):
        print("Server did not start in time; aborting desktop window.",
              file=sys.stderr, flush=True)
        raise SystemExit(1)

    webview.create_window(
        "Alidade", url,
        width=1440, height=900, min_size=(960, 640),
    )
    webview.start()


def run_app():
    print("Starting Alidade...", flush=True)
    
    configure_logging()
    args = parse_args()
    
    # --disable-database is a convenience preset for multi-user anonymous deployments.
    # It bundles: ephemeral workspace + no data connectors + no custom models + hide keys.
    workspace_backend = args.workspace_backend
    if args.disable_database:
        if workspace_backend == 'local':
            workspace_backend = 'ephemeral'
        args.disable_data_connectors = True
        args.disable_custom_models = True
        args.disable_display_keys = True
        print("  Multi-user anonymous mode (--disable-database): "
              "TTL-managed ephemeral workspace, no connectors, no custom models, keys hidden", flush=True)

    # Override config from CLI args
    app.config['CLI_ARGS'] = {
        'host': args.host,
        'sandbox': args.sandbox,
        'disable_display_keys': args.disable_display_keys,
        'disable_data_connectors': args.disable_data_connectors,
        'disable_custom_models': args.disable_custom_models,
        'max_display_rows': args.max_display_rows,
        'scratch_max_bytes': args.scratch_max_size_mb * 1024 * 1024,
        'scratch_max_file_bytes': args.scratch_max_file_size_mb * 1024 * 1024,
        'data_dir': args.data_dir,
        'dev': args.dev,
        'workspace_backend': workspace_backend,
        'available_languages': [
            lang.strip() for lang in os.environ.get('AVAILABLE_LANGUAGES', 'en,zh').split(',') if lang.strip()
        ],
    }
    
    # Now that --data-dir is applied, ensure the persistent log file lives
    # under the resolved DF home (re-points if it differs from the default).
    configure_file_logging()

    # Register blueprints (this is where heavy imports happen)
    _register_blueprints()

    url = "http://localhost:{0}".format(args.port)

    if args.desktop:
        print(f"Ready! Opening Alidade in a desktop window ({url}).", flush=True)
        _run_desktop(url, args)
        return

    print(f"Ready! Open {url} in your browser.", flush=True)

    if not args.dev:
        threading.Timer(1.5, lambda: webbrowser.open(url, new=2)).start()

    debug_mode = args.dev
    app.run(host=args.host, port=args.port, debug=debug_mode, use_reloader=debug_mode)

if __name__ == '__main__':
    run_app()
