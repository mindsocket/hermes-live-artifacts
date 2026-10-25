# hermes-live-artifacts: notes for coding agents

Hermes plugin: agent-published HTML pages with a Claude-compatible `window.claude` runtime, SQLite persistence, one
aiohttp listener inside the Hermes gateway. Read README.md for the shape. Read
docs/compatibility.md before touching `shim.js` or the skills, and docs/architecture.md §5 before touching the CSP.

## Commands

    uv sync                            # sync dependencies and virtualenv
    uv run pytest                      # run test suite
    uv run ruff check                  # lint checks (configured in pyproject.toml)
    uv run ruff format --check         # check formatting (run `uv run ruff format` to fix)
    uv run python scripts/validate.py  # validate plugin structure and manifest (isolated check)
    bun install && bun run test:e2e    # browser tests (Playwright, installed Chrome); a separate run, not part of pytest
    scripts/test_dashboard             # dashboard backend tests; run in the Hermes agent venv (needs FastAPI)

## Layout and import rules

- Code and plugin assets live in the `plugin/` directory (`plugin/plugin.yaml`, `plugin/__init__.py`, `plugin/cli.py`, `plugin/server/`, `plugin/skills/`, and `plugin/dashboard/`).
- Packaging and dependencies are managed via `uv` and `pyproject.toml` (with `uv.lock`). Dev dependencies (pytest, ruff) live in `[dependency-groups] dev`.
- Inside the package use relative imports (`from .server import tools`).
- `plugin/server/shim.js` is read at import time and served inline to every page. Changing it changes every artifact.
- Skills are data for the agent, registered from `plugin/skills/*/SKILL.md` with description and
  frontmatter parsed by `_skill_frontmatter`. Frontmatter must stay `key: value` lines.

## Rules that exist because of past bugs

- Listener gating uses `gateway.pid` naming the current process (`in_gateway()`). Never gate on the
  `_HERMES_GATEWAY` env marker: it leaks into child processes and is unset in others.
- Never hardcode a person's name, chat id or session. The creating session is captured per call from the
  gateway's context (`current_session_key`); notification targets are not configured.
- A page-supplied value is untrusted: the pushed `[artifact notified]` message frames the JSON as data, not
  instructions. Keep it that way.
- Writes from one page are queued in call order in the shim because the server handles requests on a thread pool.
- Artifacts share one origin, so isolation between them rests on two things that must stay together: the CSP
  `sandbox` header (no `allow-same-origin`) and the per-artifact API token in the `x-artifact` header. Never reintroduce
  cookie-authenticated API calls or `allow-same-origin`. `link_secret` only gates page loads; the API token applies in
  both modes. See docs/architecture.md §5.

## Conventions

- Compatibility target is Claude runtime contract 0.2.75 (its published type definitions, kept outside this repo).
  A page-side deviation is either documented in docs/compatibility.md or a bug. Agent-side tool names are not part
  of the artifact API and need no compatibility.
- Naming in user-facing text: say "live artifacts" where the subject is the plugin or the feature (dashboard tab
  label, plugin description, headings). Where the subject is a single artifact the user is acting on ("Archive
  artifact?", "No artifacts.", "Select an artifact."), plain "artifact" is right. Don't rename identifiers, routes
  or API paths.
- Code style: linting and formatting are enforced by Ruff (Python 3.11 target, line-length 120, configured in `pyproject.toml`). All code must pass `uv run ruff check` and `uv run ruff format --check`.
- Do not vendor Anthropic's runtime JS, and do not copy its source or its captured output into the repo. Public docs
  describe differences and what a page gets; investigation notes, captures and measurements stay in `tmp/`.
- Browser captures (HAR, saved responses) hold org ids and session tokens. They live in `tmp/`, which is gitignored.
  Never commit them.
- Keep claims about behaviour tied to a test or a measurement.
- Do not commit or push unless asked. **Restarting the gateway is a user-approved action** — it drops in-flight
  runs and briefly takes the listener down — so never do it as a side effect of other work; ask first.
- Dev loop for running code (the listener runs the installed copy, not this checkout): after a commit, run
  `scripts/deploy_to_hermes` — it installs the committed tree and restarts the gateway with confirmation first.

## Testing notes

- `tests/test_http.py` drives the real aiohttp app with `TestClient`; `test_store.py` covers the Firestore-style
  semantics; `test_plugin.py` covers registration and listener hosting with a fake `ctx`.
- Page JavaScript (`shim.js`, artifact HTML) is covered by `e2e/` (Playwright, TypeScript, run with `bun run
  test:e2e`): `e2e/serve_fixture.py` starts the real aiohttp app over a throwaway store and each test gets a fresh
  fixture artifact (`e2e/fixtures/form.html`). It covers the happy path: db roundtrip, `onSnapshot` over SSE,
  `add` ids, server-side path errors, notify (with and without a save), a lost event stream being reported, link and
  `window.open` popups, and the storage sandbox. Anything it does not cover (real phone
  behaviour, the dashboard tab, the real Hermes install) still needs a manual check.
