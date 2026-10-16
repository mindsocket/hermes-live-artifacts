# hermes-live-artifacts

A Hermes plugin that lets an agent publish small interactive HTML pages (forms, review lists, dashboards), open them
by link on your phone or desktop over a tailnet, persist what you enter, and read it back, or have it pushed into the
chat session that created the page.

This plugin is inspired by Claude artifacts and aims to be compatible with them. The page-side API follows Claude's
artifact runtime (`window.claude.use("db")` and friends), cut down to one user, so a page written for Claude can move
here when it only uses the capabilities implemented. Pages that use anything else degrade: `use()` resolves `null`.
See [docs/compatibility.md](docs/compatibility.md) for what matches, what differs and what is not covered.

Status: working spike, being hardened.

## How it works

    agent tools (artifact_create ...) --> SQLite store <-- aiohttp listener --> your browser
                                                              (inside the gateway process)

- The plugin registers `artifact_*` tools and skills.
- One aiohttp listener runs inside the long-lived messaging gateway (other processes load tools only and share the
  SQLite file). It serves `/a/<id>` pages and `/api/<id>/*`.
- A link is `<base_url>/a/<id>` (22-character random id). With `link_secret: always` it also carries `?k=<token>`, which is
  exchanged for a cookie and dropped from the URL. See [Security](#security).
- Pages see `window.claude` (injected shim). A db save can carry `{notify: true}`, and `claude.notify()` stands alone:
  either tells the agent the data is ready for review. When the artifact was created from a chat session, the notify
  pushes a fixed `[artifact notified]` notice into that session; the agent reads the data with `artifact_read`.

## Tools

    artifact_create(title, html, capabilities?, pinned?, seed?, wake?)   returns the link and push_on_notify
    artifact_update(id, html?, ...)    in place; db contents kept
    artifact_read(id)                  status plus every document
    artifact_list / artifact_db_write / artifact_archive / artifact_delete / artifact_export

## CLI commands

The plugin registers `hermes live-artifacts` for managing artifacts from the terminal:

    hermes live-artifacts list [--status active|archived] [--json]
    hermes live-artifacts show <id> [--data] [--json]
    hermes live-artifacts link <id> [--rotate] [--json]
    hermes live-artifacts open <id>
    hermes live-artifacts archive <id> [--json]
    hermes live-artifacts delete <id> [-y] [--json]
    hermes live-artifacts export <id> [--out <file.json>]

Output renders as Rich tables/panels in interactive terminals, or clean JSON with `--json`.

## Dashboard tab

`plugin/dashboard/` adds an **Artifacts** tab to the Hermes dashboard: list by status, a device-sized preview of the page, a
live view of its documents (refreshes as the page autosaves), and copy link / rotate link / pin / archive / delete /
export JSON. It needs no build step. The backend (`plugin/dashboard/plugin_api.py`) runs in the dashboard process and reads
the same SQLite file as the gateway. Rotate is not offered (and the API refuses it) when `link_secret` is `never`. Restart the dashboard after
updating the plugin.

## Skills

Loaded by the agent as `live-artifacts:capabilities` and `:design`. Inspired by Claude's artifact capabilities and
design guidance; written for this plugin. Sources: `plugin/skills/`.

## Installing

Directory plugin:

    hermes plugins install /path/to/hermes-live-artifacts#plugin      # or: hermes plugins update live-artifacts
    hermes gateway restart                                           # the listener lives in the gateway

The plugin assets sit in the `plugin/` directory (`plugin/plugin.yaml`).

Pip route (declared, not yet exercised): `pip install .` registers the `hermes_agent.plugins` entry point
`live-artifacts = live_artifacts:register`.

## Configuration

Set under the plugin's config (see `plugin.yaml` `config_schema`):

    listen_host      127.0.0.1, an IP, or "tailscale" (bind this node's tailnet IPv4)
    listen_port      47318
    listen_process   gateway (default) | any | none
    base_url         origin used in returned links; empty means http://<listen_host>:<listen_port>
    link_secret      auto (default) | always | never; see Security

MCP from pages is off until the operator lists servers in Hermes config under
`plugins.entries.live-artifacts.mcp_allowlist` (not a plugin config key).

Data lives in `$HERMES_HOME/plugin-data/live-artifacts/`; backups and the `.secret` key file are covered in
`docs/security.md`.

## Security

There is no sign-in. Access is by possession of the link, so treat the listener as private-network only.

- `link_secret: never`: the link is `/a/<22-char random id>`, protected by obscurity alone. Reasonable on a private
  tailnet you control. Anyone who sees the URL (pasted chat, browser history, screen share) can open that artifact.
- `link_secret: always`: adds `?k=<secret>`, stripped from the address bar after the first open. Same sharing risk, but
  the id alone is not enough. Either way, archiving the artifact revokes its link.
- `auto` (default) picks `always` when the bind address or `base_url` is not loopback. Set `never` explicitly to get clean
  URLs on a tailnet.
- Pages run in a sandbox with no cookies or browser storage, and each carries only its own artifact's API token.
  The browser does not stop a page calling another artifact's API, so the token is the only barrier; see
  [docs/security.md](docs/security.md).
- A page may call MCP tools only if it declares them and the operator has allowed that server. Declared tools run with
  your authority.
- Do not expose the port to the public internet. Tailscale Funnel is out of scope.
- Possible later option: sign in with Hermes' own dashboard credentials. Not implemented.

## Development

    uv run pytest                      # run tests (pytest with aiohttp; auto-includes hermes-agent if present)
    uv run ruff check                  # lint checks
    uv run ruff format --check         # check code formatting
    uv run python scripts/validate.py  # validate plugin manifest and isolation in clean temp directory
    scripts/deploy_to_hermes           # install the committed tree and restart the gateway (asks first)

## More

- [docs/architecture.md](docs/architecture.md): components, data model, hosting and isolation design.
- [docs/security.md](docs/security.md): threat model and known gaps.
- [docs/compatibility.md](docs/compatibility.md): how the page API differs from Claude's.
- [AGENTS.md](AGENTS.md): repository layout, conventions and pitfalls for contributors.
