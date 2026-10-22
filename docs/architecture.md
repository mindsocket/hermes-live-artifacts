# Architecture: hermes-live-artifacts

This document describes the technical architecture, runtime model, security boundaries, and design decisions of `hermes-live-artifacts`.

---

## 1. System Overview

`hermes-live-artifacts` allows Hermes agents to publish small, interactive HTML applications (review lists, forms, dashboards, checklists) that users open on phone or desktop over a private network (such as a Tailscale tailnet), enter data, and have that data persisted and pushed back into the originating chat session.

```
Agent Tools (live_artifact_create, ...) ──┐
                                     ├─► SQLite Store (plugin-data/live-artifacts/artifacts.db)
Hermes CLI / Cron Sessions ──────────┘          ▲
                                                │
                          aiohttp Listener ─────┘ (Inside Hermes Gateway Process)
                                 │
                 ┌───────────────┴───────────────┐
                 ▼                               ▼
       Deep Link over Tailnet             Desktop Embed
       (e.g. Phone Browser)           (Sandboxed Iframe)
```

### Components

1. **Agent Toolset (`artifacts`)**:
   - The `live_artifact_*` tools listed in the README, plus the skills under `plugin/skills/`.
2. **SQLite Document Store (`store.py`)**:
   - Manages artifact metadata and a Firestore-style hierarchical document database (`docs` table).
   - Operates in WAL mode with short immediate transactions and per-call connections for multi-process safety.
3. **aiohttp HTTP Listener (`app.py`)**:
   - Runs inside the long-lived Hermes gateway process.
   - Serves a server-owned shell at `/a/<id>` that frames the rendered artifact HTML at `/a/<id>/frame`
     (`frame-ancestors` derived from `base_url`), and runtime APIs at `/api/<id>/*` (`db`, `mcp`, `events`).
4. **Client Runtime Shim (`shim.js`)**:
   - Injected into artifact HTML at load time.
   - Emulates Claude's artifact runtime contract 0.2.75 (`window.claude.use('db')`, `downloads`, `mcp`).
   - Queues each page's writes in call order (notify included) and provides `claude.notify()`.
5. **Dashboard tab (`plugin/dashboard/`)**:
   - A Hermes dashboard plugin: `manifest.json`, a prebuilt-by-hand `dist/index.js` (plain JS on the host's plugin SDK)
     and `plugin_api.py`, a FastAPI router mounted at `/api/plugins/live-artifacts/`.
   - Runs in the dashboard process, not the gateway; both open the same SQLite file. The router is a thin wrapper
     over `server/admin.py` and the `tools` module, which hold the logic and are covered by `tests/test_dashboard.py`.
   - The UI polls `/seq` every 2 s (`change_seq` moves on every write) and refetches when it changes.

---

## 2. Process & Hosting Model

Hermes plugins are loaded into every Hermes process (CLI invocations, cron runners, and the messaging gateway). However, network listeners must bind to a specific TCP port (`listen_port`, default 47318).

### In-Gateway Listener (Default)

The HTTP listener runs inside the **messaging gateway process** (`ai.hermes.gateway`):
- **Gating**: The listener checks `gateway.pid` in `HERMES_HOME` matching the current process ID (`in_gateway()`). It never gates on environment variables like `_HERMES_GATEWAY` (which leak into child processes).
- **Non-Gateway Processes**: CLI, cron, and subagents load the tools only. They read and write directly to the shared SQLite database file.
- **Supervision**: When running inside the gateway, the listener is supervised on the gateway's asyncio event loop. If loaded in a non-async process with `listen_process: any`, it falls back to a background daemon thread.

### Configuration

Keys and defaults are listed in the README
(Configuration) and declared in `plugin.yaml`. An invalid `link_secret` or `listen_process` fails plugin load.

---

## 3. Data & Persistence Model

### Schema

Storage lives in `$HERMES_HOME/plugin-data/live-artifacts/artifacts.db`:

1. **`artifacts` Table**:
   - `id`: 22-character URL-safe string from a random UUIDv4.
   - `title`, `html`, `capabilities` (JSON).
   - `origin_session`: Captured from Hermes gateway session context (`current_session_key()`).
   - `wake`: Boolean flag indicating whether a notify should push a notice into the creating session.
   - `status`: `active` or `archived`.
   - `salt`: Random per-artifact salt. The page/API token is derived from it with the installation key, so only the salt is stored and replacing it revokes the token.
   - Timestamps: `created_at`, `updated_at`, `notified_at` (the last notify), `archived_at`.

2. **`docs` Table**:
   - `artifact_id`, `path`, `collection`, `data` (JSON), `version` (optimistic locking counter), `updated_at`.
   - Document paths follow Firestore conventions (`collection/document/collection/document...`).
   - Quotas: 256 KiB maximum per document; 25,000 documents maximum per artifact.

3. **`meta` Table**:
   - `change_seq`: Atomic monotonic counter bumped on every document write to drive Server-Sent Events (`/api/<id>/events`).

---

## 4. Runtime & Claude Compatibility

The client environment aims for source compatibility with **Claude artifact runtime contract 0.2.75**:

Which capabilities are implemented, stubbed or absent, and how they differ from Claude's, is in
[compatibility.md](compatibility.md). `mcp` calls are mapped through Hermes `ctx.call_mcp`.

### Autosave vs. Notify

- **Autosave**: As the user interacts with the artifact, `window.claude.use('db')` writes documents directly to SQLite. The shim queues one page's writes in call order.
- **Notify**: `{notify: true}` on a db set or update, or `claude.notify()`, records `notified_at` in the same transaction as the save. Nothing is reserved: the page's own fields (for example a `status` it chose) are ordinary data.
- **Session Injection**: A notify on an artifact with an `origin_session` and `wake` true invokes Hermes's `ctx.inject_message` with a fixed `[artifact notified]` notice. Notifies closer than 5 seconds apart push once. The notice carries no page data.

---

## 5. Security & Isolation Architecture

All artifacts are hosted under a single origin (the gateway listener's hostname). To provide isolation without requiring unique subdomains:

1. **CSP Sandbox (`sandbox allow-scripts allow-forms allow-downloads allow-modals allow-popups`)**:
   - The link points at a server-owned **shell** page at `/a/<id>` (`frame-src 'self'`; its `frame-ancestors` is derived from `base_url`), whose only content is an iframe of the artifact at `/a/<id>/frame`.
   - The artifact document is served with a `Content-Security-Policy` `sandbox` directive listing those tokens — with `allow-popups` but **no `allow-popups-to-escape-sandbox`**: a popup opened from a user gesture inherits the opaque-origin sandbox, so it cannot read storage, cookies, or navigate the top level (see security.md §3.1).
   - This assigns each page an **opaque origin** (`origin: null`).
   - Pages cannot access `localStorage`, `sessionStorage`, `IndexedDB`, or cookies.
   - A page cannot read another artifact's page: `/a/*` responses carry no CORS headers, and a sandboxed page has no credentials to send.
   - The API answers `Access-Control-Allow-Origin: *`, so the browser does not stop a page calling another artifact's `/api/<id>/*`; the per-artifact token is the only barrier (security.md §3.4).
   - The headers are built in `app.py` (`CSP` + `ARTIFACT_SANDBOX` for the frame, `SHELL_CSP` for the shell).
2. **Capability Token Header (`x-artifact`)**:
   - Every API call to `/api/<id>/*` must include the header `x-artifact: <token>`.
   - The token is derived per artifact (security.md §3.2) and a page contains only its own, so a hostile artifact cannot authenticate to another's endpoints.
3. **Link access** (`link_secret`; the response matrix is security.md §3.3):
   - `always`: Links carry `?k=<token>`. On first open, the token is exchanged for an `HttpOnly`, `SameSite=Lax` cookie scoped strictly to `/a/<id>`, and stripped from the URL.
   - `never`: The 22-character unguessable ID is the bearer grant (reasonable on a private tailnet).
   - `auto` (default): `always` unless both the bind address and `base_url` are loopback.
   - Legacy 12-character ids are guessable, so they always require the secret.
   - There is no sign-in; access is by possession of the link. Revoke by archiving.
4. **MCP Allowlisting**:
   - Double-gated: Artifacts must declare server/tool pairs in `capabilities.mcp`, and the operator must explicitly allow the server under `plugins.entries.live-artifacts.mcp_allowlist`.

---

## 6. Alternatives Considered & Trade-offs

### A. In-Gateway Hosting vs. Decoupled Standalone Daemon
- **Decision**: Host the HTTP listener inside the long-lived Hermes gateway process.
- **Trade-off**: If the gateway restarts, open artifact connections drop momentarily.
- **Rationale**: An in-gateway listener requires zero external daemon management (`launchd`/`systemd`), shares configuration and memory directly with Hermes, and leverages existing gateway supervision. Standalone daemon mode is deferred until public guest sharing is prioritized.

### B. Single Origin with Sandbox vs. Subdomains / Wildcard DNS
- **Decision**: Single origin with strict CSP sandbox and capability tokens.
- **Rationale**: Tailscale Serve and typical local network setups bind a single hostname and port. Requiring wildcard DNS (`*.artifacts.local`) would add immense deployment friction.

### C. Direct SQLite WAL vs. Separate Database Server
- **Decision**: SQLite in WAL mode.
- **Rationale**: Eliminates external database dependencies. Backups: `hermes backup` includes the data directory, so both `artifacts.db` and `.secret` are in the archive (see Backups in `security.md`).
