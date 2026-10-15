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
- Pages see `window.claude` (injected shim). `claude.submit()` marks the artifact submitted and, when it was created
  from a chat session, pushes the entered data back into that session as an `[artifact submitted]` message.

## Tools

    artifact_create(title, html, capabilities?, pinned?, seed?, wake?)   returns the link and push_on_submit
    artifact_update(id, html?, ...)    in place; db contents kept
    artifact_read(id)                  status plus every document
    artifact_list / artifact_db_write / artifact_rotate_link / artifact_archive / artifact_delete / artifact_export

## Skills

Loaded by the agent as `hermes-live-artifacts:capabilities` and `:design`. Inspired by Claude's artifact capabilities and
design guidance; written for this plugin. Sources: `src/hermes_live_artifacts/skills/`.

## Installing

Directory plugin (what is used today):

    hermes plugins install /path/to/hermes-live-artifacts      # or: hermes plugins update hermes-live-artifacts
    hermes gateway restart                                     # the listener lives in the gateway

The repo root holds `plugin.yaml` and a small `__init__.py` shim that puts `src/` on the path; the code is the
`hermes_live_artifacts` package under `src/`.

Pip route (declared, not yet exercised): `pip install .` registers the `hermes_agent.plugins` entry point
`hermes-live-artifacts = hermes_live_artifacts:register`.

## Configuration

Set under the plugin's config (see `plugin.yaml` `config_schema`), or env `HERMES_LIVE_ARTIFACTS_<KEY>`:

    listen_host      127.0.0.1, an IP, or "tailscale" (bind this node's tailnet IPv4)
    listen_port      47318
    listen_process   gateway (default) | any | none
    base_url         origin used in returned links; empty means http://<listen_host>:<listen_port>
    link_secret      auto (default) | always | never; see Security

MCP from pages is off until the operator lists servers under `plugins.entries.hermes-live-artifacts.mcp_allowlist`.

Data lives in `$HERMES_HOME/plugin-data/hermes-live-artifacts/` (override with `HERMES_LIVE_ARTIFACTS_DATA`). `hermes backup` includes it, together with the
`.secret` key file; treat that archive as a secret store (see `docs/SECURITY.md`).

## Security

There is no sign-in. Access is by possession of the link, so treat the listener as private-network only.

- `link_secret: never`: the link is `/a/<22-char random id>`, protected by obscurity (122 random bits). Reasonable on a private
  tailnet you control. Anyone who sees the URL (pasted chat, browser history, screen share) can open that artifact.
- `link_secret: always`: adds `?k=<secret>`, stripped from the address bar after the first open. Same sharing risk, but
  the id alone is not enough. Either way, archiving the artifact revokes its link.
- `auto` (default) picks `always` when the bind address or `base_url` is not loopback. Set `never` explicitly to get clean
  URLs on a tailnet.
- Pages run in a sandbox with no cookies or browser storage, and each carries only its own artifact's API token.
  The browser does not stop a page calling another artifact's API, so the token is the only barrier; see
  [docs/SECURITY.md](docs/SECURITY.md).
- A page may call MCP tools only if it declares them and the operator has allowed that server. Declared tools run with
  your authority.
- Do not expose the port to the public internet. Tailscale Funnel is out of scope.
- Possible later option: sign in with Hermes' own dashboard credentials. Not implemented.

## Development

    uv run pytest                      # run tests (pytest with aiohttp; auto-includes hermes-agent if present)
    uv run ruff check                  # lint checks
    uv run ruff format --check         # check code formatting
    uv run python scripts/validate.py  # validate plugin manifest and isolation in clean temp directory

## More

- [docs/architecture.md](docs/architecture.md): components, data model, hosting and isolation design.
- [docs/SECURITY.md](docs/SECURITY.md): threat model and known gaps.
- [docs/compatibility.md](docs/compatibility.md): how the page API differs from Claude's.
- [AGENTS.md](AGENTS.md): repository layout, conventions and pitfalls for contributors.
