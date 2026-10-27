# Spec: Claude Code access via a stdio MCP server

Status: shipped (PR #18) with the full tool set. Option A (re-read on request) is the notify story; B-F stay open.
Deviations from the draft below: all tools are served, and the skills as resources `live-artifacts://skills/<name>`; `wake` and export `path` are removed from the MCP schemas;
tool and skill text name the exact thing per harness (the `live-artifacts:<name>` skill in Hermes, the resource over MCP); `mcp` >= 2 is checked at start; the process never binds the listener.

## Goal

An artifact created from Claude Code appears in Hermes (dashboard, Desktop, CLI, links) next to every other artifact, and an
artifact created in Hermes can be read and edited from Claude Code. Convenience feature for one user running Claude Code on the
same machine as Hermes. A secondary benefit: a page written once works in both environments, and can be shunted between
claude.ai artifacts and live artifacts.

## Non-goals

- Running without Hermes. The listener stays in the gateway (`listen_process: gateway`); this spec assumes it is up.
- A standalone daemon (previously rejected, architecture.md §6A; still no case).
- Other harnesses (Codex, Cursor...). No demonstrated need. If it works there, fine.
- Provenance columns, schema versioning, harness/session detection. YAGNI; revisit if the schema changes materially.
- Edits to the skills or their descriptions. Ship them as they are and observe.
- HTTP MCP on the listener (new admin-grade credential; see security.md before reconsidering).

## Design

```
Claude Code ──stdio──► hermes live-artifacts mcp ──► Store (artifacts.db, shared WAL) ◄── gateway listener ◄── browser
```

- A stdio MCP server exposing the existing `live_artifact_*` tools, calling the same handlers in `plugin/server/tools.py` with the
  same schemas. No new storage, no new network surface.
- Config (`base_url`, `link_secret`, `index`) comes from the Hermes config, so links match what Hermes would return. Reuse the
  settings resolution in `plugin/__init__.py`; do not add a second config path.
- The store is already multi-process safe (WAL, per-call connections), so the MCP process and gateway share the file.

### Launch command

Decision: the server is a Hermes CLI subcommand, `hermes live-artifacts mcp`, rather than a standalone console script.

| | `hermes live-artifacts mcp` | standalone `live-artifacts-mcp` (uvx / venv python) |
|---|---|---|
| Profile | `hermes -p <profile> live-artifacts mcp` sets the Hermes home, so DB, secret and config follow | must set `HERMES_HOME` by hand; easy to point at the wrong profile |
| Config | Hermes plugin settings resolve as usual | needs its own resolution, or reads Hermes config directly |
| Dependencies | the Hermes venv (`mcp` 2.0.0 and `aiohttp` 3.14.3 are present there; measured with `importlib.metadata`) | own environment; duplicates deps and risks version drift |
| Risk | Hermes startup must keep stdout clean (stdio MCP carries JSON-RPC on stdout); startup latency | none of these |

Recommendation: the subcommand. It matches "runs alongside Hermes" and gets profile handling for free. Tracer must verify the
stdout-cleanliness and startup-time risks (below). Fallback if they fail: a thin console script that sets up the Hermes home and
imports the same handlers.

Claude Code side: `.mcp.json` entry `{"command": "hermes", "args": ["live-artifacts", "mcp"]}`, add `-p <profile>` for a non-default profile.

### Install path

In-repo `.claude-plugin/` (manifest, `.mcp.json`, and the existing skills) is the plausible route. Not decided: a bare
`claude mcp add` line in the README may be enough and avoids a second manifest. Tracer uses the bare line; the Claude Code plugin
packaging is deferred until the tracer shows the skills are worth bundling.

## Tools

Same set as today: create, update, read, list, db_write, archive, delete, export. Open item: `create` from `--html-file` on the
CLI is a general convenience (not required for MCP, where the agent passes `html` inline); do it only if cheap.

### Waiting for the user's input (undecided)

In Hermes the notify is pushed into the chat. Claude Code has no equivalent for a plain MCP server, so the agent needs a way to
learn that the user has submitted. State is already recorded (`notified_at`, `change_seq`), so every option below is only a
delivery mechanism; none needs schema changes.

| Option | How | Fit | Cost |
|---|---|---|---|
| **A. Agent re-reads on request** | The user says "done", the agent calls `live_artifact_read` | Works today, zero code | Manual nudge |
| **B. Blocking wait tool** | `live_artifact_wait(id, since_seq?, timeout_s)` returns when `notified_at` / `change_seq` moves, or on timeout | Simple, works in any MCP client; the agent blocks inside one tool call | Holds a turn open; clients enforce their own tool timeouts, so keep it short (≤ ~60 s) and loop. Emit MCP progress notifications while waiting to keep the call alive where the client honours them |
| **C. MCP Tasks** (spec 2025-11-25, experimental) | Wait tool returns a task handle; client polls `tasks/get` | The protocol-native form of B | Client support unverified in Claude Code; experimental in the spec. Check before building |
| **D. Resource subscriptions** | Expose `artifact://<id>` as a resource, server sends `notifications/resources/updated` | Protocol-native push | Whether Claude Code surfaces updates to the model is unverified; may only refresh a resource the user attached |
| **E. Claude Code channels** | The server declares the channel capability and pushes a `<channel>` event into the running session | Closest to the Hermes behaviour (a notice arrives without polling) | Research preview; needs `--channels` per session and an Anthropic-allowlisted plugin (or `--dangerously-load-development-channels`); Team/Enterprise must enable it |
| **F. Monitor over a CLI `watch`** | A `hermes live-artifacts watch <id>` process prints a line on notify; Claude Code's `Monitor` tool streams it | Works today with existing Claude Code features | Needs a CLI command, which cuts against MCP-only |

Recommendation: start with A (nothing to build), add B if the nudge proves annoying. B is tool-timeout-bounded but needs no
client features beyond tools. Revisit C/D/E as they leave preview; E is the one that reproduces Hermes behaviour.
Sources: [MCP async tasks](https://workos.com/blog/mcp-async-tasks-ai-agent-workflows),
[Claude Code channels](https://code.claude.com/docs/en/channels).

## Tracer bullet

Smallest slice that answers "worth it?":

1. `hermes live-artifacts mcp` serving **create, read, list** over stdio (via the `mcp` SDK in the Hermes venv).
2. `claude mcp add` pointing at it; create a trivial form from Claude Code.
3. Confirm: it appears in `hermes live-artifacts list` and the dashboard tab; the link opens through the running gateway; entering
   data and reading it back from Claude Code works (option A).
4. Measure: Hermes startup time for the subcommand; stdout is clean JSON-RPC only (no banners, no logging to stdout); behaviour
   with `-p <profile>`.

Decision gate after the tracer: keep going (add the remaining tools, optional wait tool B, packaging), or drop.

## Open questions

- Does Hermes plugin CLI startup print anything to stdout before the subcommand runs? (tracer)
- `mcp` 2.0.0 in the Hermes venv: does its server API differ from 1.x enough to matter for pinning?
- Tool-name collision/confusion with Claude Code's built-in `Artifact` tool: observe before touching descriptions.
- Does Claude Code surface MCP resource updates or Tasks to the model? (only if A/B prove insufficient)

## Side idea (separate, not scheduled)

Make claude.ai artifacts visible inside live artifacts without migration: a read-through bridge that shells out to the `claude`
CLI against an authorised account and makes the equivalent Artifact tool calls. Same end result by a different path; would need
its own spec (auth, data direction, how `window.claude` db rows map). Parked as an idea.
