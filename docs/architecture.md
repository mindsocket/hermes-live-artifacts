# Architecture: hermes-live-artifacts

This document describes the technical architecture, runtime model, security boundaries, and design decisions of `hermes-live-artifacts`.

---

## 1. System Overview

`hermes-live-artifacts` allows Hermes agents to publish small, interactive HTML applications (review lists, forms, dashboards, checklists) that users open on phone or desktop over a private network (such as a Tailscale tailnet), enter data, and have that data persisted and pushed back into the originating chat session.

```
Agent Tools (artifact_create, ...) ──┐
                                     ├─► SQLite Store (plugin-data/hermes-live-artifacts/artifacts.db)
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
   - The `artifact_*` tools listed in the README, plus the skills under `src/hermes_live_artifacts/skills/`.
2. **SQLite Document Store (`store.py`)**:
   - Manages artifact metadata and a Firestore-style hierarchical document database (`docs` table).
   - Operates in WAL mode with short immediate transactions and per-call connections for multi-process safety.
3. **aiohttp HTTP Listener (`app.py`)**:
   - Runs inside the long-lived Hermes gateway process.
   - Serves rendered artifact HTML at `/a/<id>` and runtime APIs at `/api/<id>/*` (`db`, `mcp`, `events`).
4. **Client Runtime Shim (`shim.js`)**:
   - Injected into artifact HTML at load time.
   - Emulates Claude's artifact runtime contract 0.2.75 (`window.claude.use('db')`, `downloads`, `mcp`).
   - Queues each page's writes in call order and provides `claude.submit()`.

---

## 2. Process & Hosting Model

Hermes plugins are loaded into every Hermes process (CLI invocations, cron runners, and the messaging gateway). However, network listeners must bind to a specific TCP port (`listen_port`, default 47318).

### In-Gateway Listener (Default)

The HTTP listener runs inside the **messaging gateway process** (`ai.hermes.gateway`):
- **Gating**: The listener checks `gateway.pid` in `HERMES_HOME` matching the current process ID (`in_gateway()`). It never gates on environment variables like `_HERMES_GATEWAY` (which leak into child processes).
- **Non-Gateway Processes**: CLI, cron, and subagents load the tools only. They read and write directly to the shared SQLite database file.
- **Supervision**: When running inside the gateway, the listener is supervised on the gateway's asyncio event loop. If loaded in a non-async process with `listen_process: any`, it falls back to a background daemon thread.

### Configuration

Keys, defaults and the `HERMES_LIVE_ARTIFACTS_<KEY>` environment overrides are listed in the README
(Configuration) and declared in `plugin.yaml`. An invalid `link_secret` or `listen_process` fails plugin load.

---

## 3. Data & Persistence Model

### Schema

Storage lives in `$HERMES_HOME/plugin-data/hermes-live-artifacts/artifacts.db` (or overridden by `HERMES_LIVE_ARTIFACTS_DATA`):

1. **`artifacts` Table**:
   - `id`: 22-character URL-safe string from a random UUIDv4.
   - `title`, `html`, `capabilities` (JSON).
   - `origin_session`: Captured from Hermes gateway session context (`current_session_key()`).
   - `wake`: Boolean flag indicating whether submit events should trigger an agent turn.
   - `status`: `active`, `submitted`, or `archived`.
   - `salt`: Random per-artifact salt. The page/API token is derived from it with the installation key, so only the salt is stored and replacing it revokes the token.
   - Timestamps: `created_at`, `updated_at`, `submitted_at`, `last_write_at`, `archived_at`.

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

### Autosave vs. Submit

- **Autosave**: As the user interacts with the artifact, `window.claude.use('db')` writes documents directly to SQLite. The shim queues one page's writes in call order.
- **Submit**: Calling `claude.submit()` sets a reserved document `meta/status` with `status: "submitted"`. 
- **Session Injection**: When an artifact with an `origin_session` is submitted and `wake` is true, the server invokes Hermes's `ctx.inject_message` to push an `[artifact submitted]` block directly into the creating session.

---

## 5. Security & Isolation Architecture

All artifacts are hosted under a single origin (the gateway listener's hostname). To provide isolation without requiring unique subdomains:

1. **CSP Sandbox (`sandbox allow-scripts allow-forms allow-downloads allow-modals allow-popups allow-popups-to-escape-sandbox`)**:
   - Pages are served with a `Content-Security-Policy` `sandbox` directive listing those tokens.
   - This assigns each page an **opaque origin** (`origin: null`).
   - Pages cannot access `localStorage`, `sessionStorage`, `IndexedDB`, or cookies.
   - A page cannot read another artifact's page: `/a/*` responses carry no CORS headers, and a sandboxed page has no credentials to send.
   - The API answers `Access-Control-Allow-Origin: *`, so the browser does not stop a page calling another artifact's `/api/<id>/*`; the per-artifact token is the only barrier (SECURITY.md §3.4).
   - The header is built in `app.py` (`CSP`); `sandbox` is the last directive.
2. **Capability Token Header (`x-artifact`)**:
   - Every API call to `/api/<id>/*` must include the header `x-artifact: <token>`.
   - The token is derived per artifact (SECURITY.md §3.2) and a page contains only its own, so a hostile artifact cannot authenticate to another's endpoints.
3. **Link access** (`link_secret`; the response matrix is SECURITY.md §3.3):
   - `always`: Links carry `?k=<token>`. On first open, the token is exchanged for an `HttpOnly`, `SameSite=Lax` cookie scoped strictly to `/a/<id>`, and stripped from the URL.
   - `never`: The 22-character unguessable ID is the bearer grant (reasonable on a private tailnet).
   - `auto` (default): `always` unless both the bind address and `base_url` are loopback.
   - Legacy 12-character ids are guessable, so they always require the secret.
   - There is no sign-in; access is by possession of the link. Revoke by archiving.
4. **MCP Allowlisting**:
   - Double-gated: Artifacts must declare server/tool pairs in `capabilities.mcp`, and the operator must explicitly allow the server under `plugins.entries.hermes-live-artifacts.mcp_allowlist`.

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
- **Rationale**: Eliminates external database dependencies. Backups: `hermes backup` includes the data directory, so both `artifacts.db` and `.secret` are in the archive (see Backups in `SECURITY.md`).
