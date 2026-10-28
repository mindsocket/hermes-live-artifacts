# Hermes Live Artifacts - plugin overview
`live-artifacts` is a Hermes plugin that lets an agent publish small interactive HTML pages (forms, review lists, dashboards), open them
by link, persist what you enter, and have a notification pushed into the
chat session that created the page.

https://github.com/user-attachments/assets/14d957c4-7819-4bc9-8d4a-0bd5d97e4383

This plugin is inspired by Claude artifacts and aims to be compatible with them. The page-side API follows a reduced set of Claude's
artifact runtime (`window.claude.use("db")` and friends), cut down to one user. A page written for Claude should be able to move
to this plugin, with unsupported capabilities degrading gracefully. See [docs/compatibility.md](docs/compatibility.md) for what matches, what differs and what is not covered.

## Installing

    hermes plugins install mindsocket/hermes-live-artifacts#plugin --enable
    hermes gateway restart

Start a new chat session afterwards; tools load when a session starts. To update: `hermes plugins update live-artifacts`,
then restart the gateway.

## How it works

    agent tools (live_artifact_create ...) --> SQLite store <-- aiohttp listener --> your browser
                                                              (inside the gateway process)

- The listener runs inside the Hermes gateway and serves each artifact at `<base_url>/a/<id>`.
- Pages see `window.claude`. A db save can carry `{notify: true}`, and `claude.notify()` stands alone: either tells the
  agent the data is ready for review. When the artifact was created from a chat session, the notify pushes a fixed
  `[artifact notified]` notice into that session; the agent then reads the data with `live_artifact_db_read`.

## Tools

    live_artifact_create(title, html | html_file, capabilities?, pinned?, seed?, wake?)   returns the link and push_on_notify
    live_artifact_read(id)                  the page: status, link, capabilities, html_file (a copy to edit)
    live_artifact_update(id, html? | html_file?, ...)   in place; db contents kept
    live_artifact_db_read(id)               the data: status plus every document
    live_artifact_list / live_artifact_db_write / live_artifact_archive / live_artifact_delete / live_artifact_export

Wherever a tool takes `id`, the artifact's link works too.

The agent is also given two skills, `live-artifacts:capabilities` and `live-artifacts:design`. Over MCP they are the resources
`live-artifacts://skills/capabilities` and `live-artifacts://skills/design`.

## CLI commands

`hermes live-artifacts` manages artifacts from the terminal; add `--json` for machine-readable output.

    hermes live-artifacts list [--status active|archived] [--json]
    hermes live-artifacts show <id> [--data] [--json]
    hermes live-artifacts link <id> [--rotate] [--json]
    hermes live-artifacts open <id>
    hermes live-artifacts archive <id> [--json]
    hermes live-artifacts delete <id> [-y] [--json]
    hermes live-artifacts export <id> [--out <file.json>]
    hermes live-artifacts mcp

## Claude Code

`hermes live-artifacts mcp` serves the artifact tools over stdio MCP, so Claude Code on the same machine can create, read and
update artifacts in the same store as Hermes. The gateway listener must be running to open the links. Add it with:

    claude mcp add live-artifacts -- hermes live-artifacts mcp

Use `hermes -p <profile> live-artifacts mcp` for a non-default profile (the plugin must be installed in that profile). The
skills are served as MCP resources (`live-artifacts://skills/capabilities`, `live-artifacts://skills/design`). Claude Code is not pushed a notice when a page is submitted: tell it you are
done and it re-reads the artifact. Needs `mcp` 2.x, which the Hermes Python provides; the server never starts the HTTP listener,
whatever `listen_process` is set to. `live_artifact_export` over MCP always writes to the exports directory (no custom `path`).

## Dashboard and Desktop support

The plugin adds a **Live artifacts** tab to the Hermes web dashboard and a **Live Artifacts** page to Hermes Desktop: list by status,
a preview of the page, a live view of its documents, and actions: copy link / rotate link / pin / archive / delete (export JSON is
dashboard only). Desktop also adds a command palette entry and renders `::live-artifact{id="..."}` in a chat reply as an inline card.

### Dashboard setup
Only if you've started a Hermes dashboard: after installing or updating the plugin, make it pick up the plugin with
`curl http://127.0.0.1:9119/api/dashboard/plugins/rescan` (replace host/port as appropriate).

### Desktop setup
After installing or updating the plugin, relaunch Hermes Desktop, then open Capabilities → Plugins → Live Artifacts and
switch the **Desktop** half on. Note that popups and modals are blocked in Desktop
guest frames, so use **Open in browser** for anything that needs them.

## Configuration

Set under `plugins.entries.live-artifacts.settings` in the Hermes config:

    listen_host      127.0.0.1, an IP, or "tailscale" (bind this node's tailnet IPv4)
    listen_port      47318
    listen_process   gateway (default) | any | none (tools only, no listener)
    base_url         origin used in returned links; empty means http://<listen_host>:<listen_port>
    link_secret      auto (default) | always | never; see Security
    index            false (default) | true: serve /index, a list of active artifacts; see Security

Pages can call MCP tools only after you list the servers under `plugins.entries.live-artifacts.mcp_allowlist`.

## What it does on your machine

- Stores artifacts and their documents in SQLite at `$HERMES_HOME/plugin-data/live-artifacts/artifacts.db` (0600, directory 0700).
  Page data is plain rows on disk. Backups and the `.secret` key file are covered in [docs/security.md](docs/security.md).
- `live_artifact_export` writes a JSON dump (including artifact tokens and entered data) to
  `$HERMES_HOME/plugin-data/live-artifacts/exports/`, or to a `path` the agent supplies if Hermes write policy allows it.
- `live_artifact_read/create/update` tools can read/write page HTML files in Hermes' defined and managed scratch dir.
- Runs a long-lived HTTP listener inside the Hermes gateway process (default `127.0.0.1:47318`; see Configuration for
  binding it to your tailnet). It serves pages and takes input from whoever holds a link.
- `hermes live-artifacts mcp` is a stdio server started by an MCP client such as Claude Code. It opens no port and shares the
  SQLite file with the gateway.
- Runs one subprocess, only when `listen_host` is `tailscale`: `tailscale ip -4`, to learn this node's tailnet address.
- Pages run in a sandboxed frame and can reach MCP tools only through the allowlist above.
- No telemetry, no outbound network calls, no stored credentials, no hooks or middleware.

## Security

There is no sign-in. Access is by possession of the link, so treat the listener as private-network only.

- `link_secret: never`: the link is `/a/<22-char random id>`, protected by obscurity alone. Reasonable on a private
  tailnet you control. Anyone who sees the URL (pasted chat, browser history, screen share) can open that artifact.
- `link_secret: always`: adds `?k=<secret>`, stripped from the address bar after the first open. Same sharing risk, but
  the id alone is not enough. Either way, archiving the artifact revokes its link.
- `auto` (default) picks `always` when the bind address or `base_url` is not loopback. Set `never` explicitly to get clean
  URLs on a tailnet.
- `index: true`: the listener also serves `/index`, a read-only list of active artifacts. It always needs its own token,
  whatever `link_secret` says. Get the link with `hermes live-artifacts index-link` (`--rotate` replaces the token).
  The list links embed each artifact's own `?k`, so the index token opens every listed artifact: guard it like a master key.
- Pages run in a sandbox with no cookies or browser storage, and each carries only its own artifact's API token.
  The browser does not stop a page calling another artifact's API, so the token is the only barrier; see
  [docs/security.md](docs/security.md).
- MCP tools a page declares run with your authority.
- Do not expose the port to the public internet.

## Development

    uv run pytest                      # run tests (pytest with aiohttp; auto-includes hermes-agent if present)
    uv run ruff check                  # lint checks
    uv run ruff format --check         # check code formatting
    uv run python scripts/validate.py  # validate plugin manifest and isolation in clean temp directory
    scripts/deploy_to_hermes           # install the committed tree and restart the gateway (asks first)

## Feature comparison

| Feature | `live-artifacts` (this plugin) | Claude Artifacts (Anthropic) | `dmmeteo/hermes-artifacts` |
| :--- | :--- | :--- | :--- |
| **Primary focus** | **Interactive 2-way apps**: forms, review lists, dashboards | **In-chat interactive widgets**: React apps, code snippets, visual docs | **Static document library**: browse and share finished deliverables |
| **Interaction model** | Bidirectional: user edits data, pushes `notify` back to chat session | Conversational: viewed/edited in chat; no external session pushback | Read-only: static viewing and downloading; no agent pushback |
| **Client persistence** | SQLite-backed document store via `window.claude.use('db')` with realtime SSE | Hosted document store via `window.claude.use('db')` | None (static content only; no client-side database) |
| **Runtime compatibility** | Partial Claude contract 0.2.75 (`db`, `downloads`, `mcp`, constant single `user`) | Native Claude runtime (multi-user/mock users, comments, assets, rooms) | None (no `window.claude` runtime; raw file rendering) |
| **Push to chat session** | Yes (`claude.notify()` / `{notify: true}` sends `[artifact notified]`) | No | No |
| **Supported formats** | Interactive HTML applications (embedded or CDN assets) | React, HTML, SVG, Markdown, code, Mermaid | Multi-format: HTML, Markdown, SVG, PNG, WebP |
| **Hosting architecture** | Embedded aiohttp listener inside Hermes gateway process (zero-setup) | Hosted cloud service on claude.ai | Decoupled standalone FastAPI service (runs outside Hermes gateway) |
| **Sharing & access** | Private network (Tailscale/localhost); random ID + optional link secret (`?k=...`) | Public link sharing (claude.site) or workspace permissions | Expiring 128-bit bearer tokens, SHA-256 hashed at rest, revocable guest links |
| **Management UI** | Hermes Dashboard tab ("Live artifacts"), Hermes Desktop page, CLI commands, agent tools | Claude chat interface / artifact drawer | Standalone web console (`/console/`), Hermes Dashboard tab, CLI |

## More

- [docs/architecture.md](docs/architecture.md): components, data model, hosting and isolation design.
- [docs/security.md](docs/security.md): threat model and known gaps.
- [docs/compatibility.md](docs/compatibility.md): how the page API differs from Claude's.
- [AGENTS.md](AGENTS.md): repository layout, development commands and conventions for contributors.

## License

[MIT](LICENSE)
