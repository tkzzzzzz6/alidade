# Set up a local Alidade development environment
How to set up your local machine.

## Prerequisites
* Python >= 3.11
* Node.js
* Yarn
* [uv](https://docs.astral.sh/uv/) (recommended) or pip

## Backend (Python)

### Option 1: With uv (recommended)

uv is faster and provides reproducible builds via lockfile.

```bash
uv sync                        # Creates .venv and installs all dependencies
uv run alidade         # Run app (opens browser automatically)
uv run alidade --dev   # Run backend only (for frontend development)
```

**Which command to use:**
- **End users / testing the full app**: `uv run alidade` - starts server and opens browser to http://localhost:5567
- **Frontend development**: `uv run alidade --dev` - starts backend server only, then run `yarn start` separately for the Vite dev server on http://localhost:5173

### Option 2: With pip (fallback)

- **Create a Virtual Environment**  
    ```bash
    python -m venv venv
    source venv/bin/activate  # Unix
    # or .\venv\Scripts\activate  # Windows
    ```

- **Install Dependencies**  
    ```bash
    pip install -r requirements.txt
    ```
- **Configure environment variables (optional)**
    - copy `.env.template` to `.env` and fill in your values:
        - **API keys**: set `{PROVIDER}_ENABLED=true`, `{PROVIDER}_API_KEY=...`, and `{PROVIDER}_MODELS=...` for each LLM provider you want to use. See the [LiteLLM setup](https://docs.litellm.ai/docs#litellm-python-sdk) guide for provider-specific fields.
        - **Server settings**: `DISABLE_DISPLAY_KEYS`, `SANDBOX`, etc.
    - this lets Alidade automatically load API keys at startup so you don't need to enter them in the UI.


- **Run the app**
    ```bash
    # Unix
    ./local_server.sh
    
    # Windows
    .\local_server.bat
    
    # Or directly
    alidade         # Opens browser automatically
    alidade --dev   # Backend only (for frontend development)
    ```

## Frontend (TypeScript)

- **Install NPM packages**  
    
    ```bash
    yarn
    ```

- **Development mode**

    First, start the backend server (in a separate terminal):
    ```bash
    uv run alidade --dev   # or ./local_server.sh
    ```

    Then, run the frontend in development mode with hot reloading:
    ```bash
    yarn start
    ```
    Open [http://localhost:5173](http://localhost:5173) to view it in the browser.
    The page will reload if you make edits. You will also see any lint errors in the console.

### Advanced: developing against a local `flint-chart`

DF depends on the published [`flint-chart`](https://www.npmjs.com/package/flint-chart) package;
by default (`FLINT_CHART_LOCAL` unset) the bare `flint-chart` import resolves to that npm package.

For co-development against a local Flint checkout with hot reload, point `FLINT_CHART_LOCAL` at
its source when starting the dev server (or tests):

```bash
FLINT_CHART_LOCAL=../flint-chart/packages/flint-js/src yarn start
# tests:
FLINT_CHART_LOCAL=../flint-chart/packages/flint-js/src yarn test
```

Opt-in, advanced-dev only — leave it unset for normal work and CI, which use the installed
package. The alias is wired in `vite.config.ts` and `vitest.config.ts`.

## Build for Production

- **Build the frontend and then the backend**

    Compile the TypeScript files and bundle the project:
    ```bash
    yarn build
    ```
    This builds the app for production to the `py-src/alidade/dist` folder.  

    Then, build python package:

    ```bash
    # With uv
    uv build
    
    # Or with pip
    pip install build
    python -m build
    ```
    This will create a python wheel in the `dist/` folder. The name would be `alidade-<version>-py3-none-any.whl`

- **Test the artifact**

    You can then install the build result wheel (testing in a virtual environment is recommended):
    ```bash
    # replace <version> with the actual build version. 
    pip install dist/alidade-<version>-py3-none-any.whl 
    ```

    Once installed, you can run Alidade with:
    ```bash
    alidade
    ```
    or 
    ```bash
    python -m alidade
    ```

    Open [http://localhost:5567](http://localhost:5567) to view it in the browser.


## Docker

Docker is the easiest way to run Alidade without installing Python or Node.js locally.

### Quick start

1. **Copy the environment template and add your API keys:**

    ```bash
    cp .env.template .env
    # Edit .env and set your OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.
    ```

2. **Build and start the container:**

    ```bash
    docker compose up --build
    ```

3. Open [http://localhost:5567](http://localhost:5567) in your browser.

To stop the container: `docker compose down`

Workspace data (uploaded files, sessions) is persisted in a Docker volume (`alidade_home`) so it survives container restarts.

### Build the image manually

```bash
docker build -t alidade .
docker run --rm -p 5567:5567 --env-file .env alidade
```

### Docker sandbox (`SANDBOX=docker`) is not supported inside a container

The Docker sandbox backend works by calling `docker run -v <host_path>:...` to bind-mount temporary workspace directories into child containers. When Alidade itself runs in a Docker container those paths refer to the *container* filesystem, not the host, so Docker daemon cannot mount them and the feature does not work.

Use `SANDBOX=docker` only when running Alidade **directly on the host** (e.g. with `uv run alidade --sandbox docker` or `python -m alidade --sandbox docker`). When using the Docker image, keep the default `SANDBOX=local`.


## Sandbox

AI-generated Python code runs inside a **sandbox** to isolate it from the main server process. Two backends are available:

| Backend | Flag | How it works | Overhead |
|---------|------|--------------|----------|
| **local** (default) | `--sandbox local` | Persistent warm subprocess with pre-imported pandas/numpy/duckdb. Audit hooks block file writes and dangerous operations (subprocess, shutil, etc.). | ~1 ms |
| **docker** | `--sandbox docker` | Each execution runs in a disposable `docker run --rm` container. Workspace is mounted read-only; output is returned via a bind-mounted parquet file. Memory/CPU/PID limits enforced. | ~700 ms |

```bash
# Use the default local sandbox
python -m alidade

# Use Docker sandbox (requires Docker daemon)
python -m alidade --sandbox docker
```

The Docker sandbox image is built from `py-src/alidade/sandbox/Dockerfile.sandbox`:

```bash
docker build -t alidade-sandbox -f py-src/alidade/sandbox/Dockerfile.sandbox .
```

Source: [`py-src/alidade/sandbox/`](py-src/alidade/sandbox/)


## Analyst Agent Engine

The data analyst agent (`py-src/alidade/analyst/`) runs as a
[LangGraph](https://langchain-ai.github.io/langgraph/) state graph. Models are
still called through LiteLLM (`agents/client_utils.py`); LangGraph only
orchestrates.

| Module (`analyst/engine/`) | Role |
|---|---|
| `graph.py` | Nodes `prepare → agent ⇄ tools`, `agent → act → (agent / await_user / finish)` |
| `state.py` | Checkpointed state (plain JSON data only) |
| `llm.py` | Streaming tool-calling LLM call; live `write_report` forwarding |
| `execution.py` | Sandbox execution: one persistent exploration session per run; `visualize` code always runs fresh |
| `planning.py` | `update_plan` tool — plan-then-execute, streamed as `plan` events |
| `chart_check.py` | Rule-based chart self-check; blocking issues send the chart back to the model (max 2 retries) |
| `compression.py` | Trims / summarises older steps above `ALIDADE_AGENT_CONTEXT_BUDGET` tokens (default 60000) |
| `checkpoints.py` | Per-user SQLite checkpoints for paused runs |

**Pause / resume.** `ask_user` pauses the graph with `interrupt()`. The run is
checkpointed in `ALIDADE_HOME/agent-checkpoints/<identity>.sqlite` and
the `interact` event carries its `run_id`; the frontend resumes by posting
`run_id` + the reply to `/api/agent/analyst-streaming`. Completed runs are
deleted immediately; paused runs nobody resumes are pruned after 7 days.
Multi-instance deployments need sticky sessions (or a shared
`ALIDADE_HOME`) so a resume reaches the instance holding the checkpoint.

**Stream events** (NDJSON): `context_info`, `plan`, `thinking_text`,
`tool_start` / `tool_result`, `skill_loaded`, `action`, `text_delta`,
`chart_check`, `context_compressed`, `result`, `error`, and the terminal
`completion` or `interact`.

## Deployment Profiles

Alidade supports three deployment configurations. **All defaults are optimized for Profile 1 (single-user local)** — you only need to set flags when deploying as multi-user.

### Profile 1: Single-User Local (default)

A personal instance running on `localhost`. No login required, full feature access.

```bash
# Everything uses defaults — just run it:
alidade

# Or equivalently:
alidade \
  --workspace-backend local \
  --sandbox local
```

| Setting | Value | Why |
|---------|-------|-----|
| `AUTH_PROVIDER` | *(unset)* | Single user, no login needed |
| `WORKSPACE_BACKEND` | `local` | Persist workspaces to `~/.alidade/` |
| `DISABLE_DATA_CONNECTORS` | `false` | Full access to MySQL, PostgreSQL, etc. |
| `DISABLE_CUSTOM_MODELS` | `false` | User can add any LLM endpoint |
| `DISABLE_DISPLAY_KEYS` | `false` | User can see/manage their own API keys |
| Credential vault | auto-enabled | Remembers DB credentials across restarts |
| Identity | `local:<os_username>` | Fixed, OS-derived — survives localStorage clear |

**Security notes:** In single-user localhost mode, the server ignores the `X-Identity-Id` header entirely and uses a fixed identity derived from the OS username (e.g., `local:alice`). This means vault credentials and workspaces are tied to your OS account, not a random browser UUID — clearing localStorage won't orphan your data.

### Profile 2: Multi-User Anonymous (demo / public hosting)

A shared server (e.g., for demos, workshops, public access). No login, short-lived server-side workspaces, and no sensitive features.

```bash
alidade \
  --workspace-backend ephemeral \
  --disable-data-connectors \
  --disable-custom-models \
  --disable-display-keys
```

> **Shortcut:** `--disable-database` (or `DISABLE_DATABASE=true`) bundles all of the above into a single flag.

Or via environment variables:

```env
WORKSPACE_BACKEND=ephemeral
# Ephemeral retention only; local mode is durable and ignores these settings.
EPHEMERAL_WORKSPACE_TTL_HOURS=24
EPHEMERAL_WORKSPACE_MAX_BYTES=10737418240
EPHEMERAL_WORKSPACE_CLEANUP_INTERVAL_SECONDS=1800
DISABLE_DATA_CONNECTORS=true
DISABLE_CUSTOM_MODELS=true
DISABLE_DISPLAY_KEYS=true
# Pre-configure the LLM models users can access:
OPENAI_ENABLED=true
OPENAI_API_KEY=sk-...
OPENAI_MODELS=gpt-4.1
```

| Setting | Value | Why |
|---------|-------|-----|
| `AUTH_PROVIDER` | *(unset)* | Anonymous access for demos |
| `WORKSPACE_BACKEND` | `ephemeral` | Temporary server-local workspaces with TTL/LRU cleanup |
| `DISABLE_DATA_CONNECTORS` | `true` | **Critical** — prevents DB credential exposure via identity spoofing |
| `DISABLE_CUSTOM_MODELS` | `true` | Prevents users from adding arbitrary LLM endpoints (SSRF risk) |
| `DISABLE_DISPLAY_KEYS` | `true` | Hides server-configured API keys from UI |
| Credential vault | N/A | No connectors → no credentials to store |
| Identity | anonymous (`browser:<uuid>`) | Isolates temporary workspaces by browser identity |

**Retention notes:** Ephemeral workspaces may disappear after inactivity or when the configured byte cap is reached. The browser keeps only a row-free recovery snapshot for read-only viewing. Use `WORKSPACE_BACKEND=local` for durable workspaces; ephemeral TTL/LRU cleanup never scans or deletes local-mode workspaces.

**Security notes:** Keep data connectors and custom models disabled for anonymous deployments. Browser identities are client-provided and are suitable for isolating disposable demo workspaces, not for protecting durable credentials or sensitive server-side state.

### Profile 3: Multi-User Authenticated (enterprise / team)

A shared server with SSO login. Full features, proper identity isolation.

```bash
alidade \
  --disable-display-keys
```

```env
AUTH_PROVIDER=oidc
OIDC_ISSUER_URL=https://your-idp.example.com/realms/main
OIDC_CLIENT_ID=alidade
ALLOW_ANONYMOUS=false
WORKSPACE_BACKEND=local
DISABLE_DISPLAY_KEYS=true
DISABLE_CUSTOM_MODELS=true
FLASK_SECRET_KEY=<generate-with-secrets-token-hex-32>
```

| Setting | Value | Why |
|---------|-------|-----|
| `AUTH_PROVIDER` | `oidc` / `github` / `azure_easyauth` | Verified identity from SSO |
| `ALLOW_ANONYMOUS` | `false` | Login required — no anonymous fallback |
| `WORKSPACE_BACKEND` | `local` | Persistent per-user workspaces on the server's disk |
| `DISABLE_DATA_CONNECTORS` | `false` | Safe — identity comes from auth provider, not spoofable |
| `DISABLE_CUSTOM_MODELS` | `true` | Users only use server-configured models |
| `DISABLE_DISPLAY_KEYS` | `true` | Hide server keys; users add their own |
| `FLASK_SECRET_KEY` | set explicitly | Required for stable sessions across server restarts |
| Credential vault | auto-enabled | DB credentials scoped to verified `user:<id>` |
| Identity | `user:<sub>` from auth provider | Server-verified, cannot be spoofed |

**Security notes:** With an auth provider, `get_identity_id()` returns `user:<verified_id>` from the IdP token — the `X-Identity-Id` header is ignored entirely. Workspaces, vault credentials, and DB connections are all scoped to the verified identity. Set `ALLOW_ANONYMOUS=false` to prevent unauthenticated access.

### Profile Comparison

| Feature | Profile 1 (Local) | Profile 2 (Demo) | Profile 3 (Enterprise) |
|---------|:-:|:-:|:-:|
| Login required | No | No | Yes |
| Data connectors (DB) | Yes | **No** | Yes |
| Custom LLM endpoints | Yes | **No** | Operator choice |
| Credential vault | Yes | N/A | Yes |
| Workspace persistence | Local disk | Browser only | Server disk |
| Identity | `local:<os_user>` (fixed) | `browser:<uuid>` (client) | `user:<sub>` (SSO) |

### CLI Flags Reference (complete)

| Flag | Env var | Default | Description |
|------|---------|---------|-------------|
| `--workspace-backend` | `WORKSPACE_BACKEND` | `local` | `local` or `ephemeral` |
| `--sandbox` | `SANDBOX` | `local` | Code execution backend: `local` or `docker` |
| `--disable-database` | `DISABLE_DATABASE` | `false` | **Multi-user anonymous preset**: bundles ephemeral + no connectors + no custom models + hide keys |
| `--disable-display-keys` | `DISABLE_DISPLAY_KEYS` | `false` | Hide API keys in frontend UI |
| `--disable-data-connectors` | `DISABLE_DATA_CONNECTORS` | `false` | Disable external DB connectors |
| `--disable-custom-models` | `DISABLE_CUSTOM_MODELS` | `false` | Prevent users from adding custom LLM endpoints |
| `--max-display-rows` | `MAX_DISPLAY_ROWS` | `10000` | Max rows sent to frontend |
| `--data-dir` | `ALIDADE_HOME` | `~/.alidade` | Data directory |
| `--host` | `HOST` | `127.0.0.1` | Network interface to bind |
| `-p`, `--port` | — | `5567` | Port number |
| `--dev` | `DEV_MODE` | `false` | Development mode (no auto-open browser) |
| — | `AUTH_PROVIDER` | *(unset)* | `oidc`, `github`, `azure_easyauth`, or unset for anonymous |
| — | `ALLOW_ANONYMOUS` | `true` | Allow unauthenticated access when auth provider is set |
| — | `ALIDADE_ALLOWED_API_BASES` | *(unset, all allowed)* | Comma-separated URL globs for LLM endpoint allowlist |
| — | `FLASK_SECRET_KEY` | auto-generated | Session signing key (set explicitly for production) |


## Security Considerations for Production Deployment

⚠️ **IMPORTANT SECURITY WARNING FOR PRODUCTION DEPLOYMENT**

### Identity System

Alidade uses a **namespaced identity** system with three tiers:
- **Local mode** (`127.0.0.1`, no auth provider): Identity is `local:<os_username>`, determined by the server. The `X-Identity-Id` header is ignored. Vault and workspaces are tied to the OS user.
- **Anonymous mode** (multi-user, no auth provider): Identity is `browser:<uuid>` where the UUID is generated in the browser's `localStorage`. The server trusts the client-provided `X-Identity-Id` header, but always forces the `browser:` prefix.
- **Authenticated mode** (auth provider configured): Identity is `user:<verified_id>` from the auth provider. The `X-Identity-Id` header is ignored entirely.

**Key security principle**: An attacker sending `X-Identity-Id: user:alice@...` gets `browser:alice@...` — completely separate from the real `user:alice@...` that only authenticated Alice can access.

**Anonymous spoofing risk**: In anonymous mode, if an attacker knows another user's browser UUID, they can impersonate them via the `X-Identity-Id` header. This is why **Profile 2 disables data connectors** (no DB credentials to steal) and **Profile 3 requires authentication** (header is ignored).

### Data Storage

| Backend | Flag | Storage | Persistence |
|---------|------|---------|-------------|
| **local** (default) | `--workspace-backend local` | `~/.alidade/users/<identity>/workspaces/` | Server filesystem |
| **ephemeral** | `--workspace-backend ephemeral` | Browser IndexedDB (frontend) + temp dirs (backend) | Browser session only |

### Recommended Security Measures

1. **Multi-user anonymous (demos)**: Use Profile 2 — `--workspace-backend ephemeral --disable-data-connectors --disable-custom-models --disable-display-keys` (or `--disable-database` as shortcut)
2. **Multi-user authenticated**: Use Profile 3 — set `AUTH_PROVIDER`, `ALLOW_ANONYMOUS=false`, and `DISABLE_CUSTOM_MODELS=true`
3. **HTTPS**: Use a reverse proxy (nginx, Azure App Gateway) with TLS termination
4. **`FLASK_SECRET_KEY`**: Set explicitly for production (auto-generated key changes on restart)

### Server Migration Checklist

When migrating Alidade to a new server (or rebuilding a Docker container), the following secrets and data files **must** be carried over. Losing any of them will break the corresponding functionality.

| Item | Location | What breaks if lost |
|------|----------|---------------------|
| `FLASK_SECRET_KEY` | `.env` (env var) | All user sessions invalidated (SSO login, plugin tokens). Agent code signatures fail verification — saved charts cannot refresh until re-executed. |
| `.vault_key` | `ALIDADE_HOME/.vault_key` | `credentials.db` becomes undecryptable — all stored database passwords / service credentials are lost. |
| `credentials.db` | `ALIDADE_HOME/credentials.db` | Same as above — the encrypted credential store itself. |
| `ALIDADE_CODE_SIGNING_SECRET` | `.env` (env var, optional) | If set, overrides Flask-derived signing key. Must match the old value or all code signatures break. |
| `CREDENTIAL_VAULT_KEY` | `.env` (env var, optional) | If set, overrides `.vault_key` file. Must match or vault data is unreadable. |
| `users/` & `workspaces/` | `ALIDADE_HOME/` | User workspace data (parquet files, session metadata). |

**Minimum migration steps:**

```bash
# On the OLD server — back up secrets + data
cp .env                             /backup/.env
cp $ALIDADE_HOME/.vault_key /backup/.vault_key
cp $ALIDADE_HOME/credentials.db /backup/credentials.db
# Copy workspace data if using local backend
cp -r $ALIDADE_HOME/users   /backup/users
cp -r $ALIDADE_HOME/workspaces /backup/workspaces

# On the NEW server — restore before first start
cp /backup/.env .env
cp /backup/.vault_key $ALIDADE_HOME/.vault_key
cp /backup/credentials.db $ALIDADE_HOME/credentials.db
cp -r /backup/users $ALIDADE_HOME/users
cp -r /backup/workspaces $ALIDADE_HOME/workspaces
```

> **Tip:** If you forgot to back up `FLASK_SECRET_KEY` and it was auto-generated, there is no way to recover it. Users will need to log in again, and any chart with a cached code signature will need to be re-executed by the Agent.

> **中文版:** 详细的迁移操作指南见 [docs/docs-cn/7-server-migration-guide.md](docs/docs-cn/7-server-migration-guide.md)。

## Authentication Architecture

Alidade supports a **hybrid identity system** with anonymous and authenticated modes.
See **Deployment Profiles** above for which mode to use in each scenario.

### Identity Flow

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Frontend Request                             │
├─────────────────────────────────────────────────────────────────────┤
│  Headers:                                                            │
│    X-Identity-Id: "local:alice" / "browser:550e8400-..." / ...      │
│    Authorization: Bearer <jwt>  (if auth provider configured)        │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Backend Identity Resolution                       │
│                       (auth.py: get_identity_id)                    │
├─────────────────────────────────────────────────────────────────────┤
│  Priority 1: Auth provider (OIDC/GitHub/EasyAuth) → "user:<id>"     │
│  Priority 2: Localhost mode (127.0.0.1)           → "local:<user>"  │
│              (ignores X-Identity-Id header)                          │
│  Priority 3: X-Identity-Id header                 → "browser:<id>"  │
│              (client-provided namespace prefix is IGNORED)           │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      Storage Isolation                               │
├─────────────────────────────────────────────────────────────────────┤
│  "user:alice@example.com"  → alice's workspace (ONLY via auth)       │
│  "local:alice"             → localhost user's workspace (fixed)      │
│  "browser:550e8400-..."    → anonymous user's workspace              │
└─────────────────────────────────────────────────────────────────────┘
```

### Auth Provider Setup

See the `AUTH_PROVIDER` section in `.env.template` for configuration details.

| Provider | `AUTH_PROVIDER` | Setup |
|----------|----------------|-------|
| OIDC / OAuth2 | `oidc` | Set `OIDC_ISSUER_URL` + `OIDC_CLIENT_ID` |
| GitHub | `github` | Set `GITHUB_CLIENT_ID` + `GITHUB_CLIENT_SECRET` |
| Azure EasyAuth | `azure_easyauth` | Enable in Azure App Service (no extra env vars) |
| Anonymous only | *(unset)* | Default — no login, `browser:<uuid>` identity |

## Usage
See the [Usage section on the README.md page](README.md#usage).
