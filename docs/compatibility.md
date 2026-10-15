# Compatibility with Claude artifacts

hermes-live-artifacts is inspired by Claude artifacts and aims to be compatible with them: a page written for Claude's
artifact runtime (`window.claude.use(name)`, contract 0.2.75) should be able to move here unchanged, provided it only
uses capabilities implemented here. Where it does not, the page degrades rather than breaking: `use()` resolves
`null` and the page is expected to handle that, as the contract already requires.

This document lists what matches, what differs, and what a page gets for parts that are not covered. It is a
statement of intent plus the differences we know about. It is not a description of Claude's internals.

Evidence labels: **[tested]** covered by a test in `tests/`, **[measured]** observed in a real browser run,
**[unknown]** not checked.

## Page side

Pages call `window.claude.use(name)` and get the same shape of object the contract describes for these names.

| Name | Here | Notes |
|---|---|---|
| `db` | Implemented | Firestore-style document API backed by SQLite. Path validation, limits and most error codes follow the contract; see differences. **[tested]** `test_db_roundtrip_over_http_and_agent_sees_it` |
| `user` | Implemented, constant | One user. See differences. |
| `downloads` | Implemented | File save from the page. |
| `permissions` | Stub | Reports declared capabilities as granted and never prompts. |
| `mcp` | Implemented, allowlisted | `callTool` and `server(name)` only. Calls outside the artifact's declared manifest are refused. **[tested]** `test_mcp_refuses_undeclared_server_or_tool_without_calling` |
| `artifact`, `self` | Stub | Every method rejects `not_granted`. Changes go through the agent tools. |
| `assets`, `sample`, `room`, `comments`, `files` | Not implemented | Resolve `null`. |

Page-side extensions: `claude.submit()` marks the artifact `submitted` and, when the artifact was created from a chat
session, pushes the submitted data back into that session. Claude pages do not use this and need not.

## Agent side: not compatible, by design

Claude's artifact tools are not part of the artifact API, so they are not carried over. The equivalents here are the
`artifact_*` tools listed in the README.

Migrating an artifact means: copy its HTML into `artifact_create` (or `artifact_update` on an existing artifact),
then seed its database documents with `artifact_db_write`. The skills under `src/hermes_live_artifacts/skills/` teach the
agent this spelling.

## Differences

These follow from the design (single user, self-hosted, one origin), not from missing effort.

- **One user.** `user.id()` is a constant, `can()` and `isOwner()` are true, and `name()` returns a fixed value.
  Pages that branch on a missing user id take a different path here.
- **One origin, sandboxed pages.** All artifacts are served from one hostname, whereas Claude gives each its own
  origin. Here each page runs inside a server-owned shell (`/a/<id>`) that frames the artifact (`/a/<id>/frame`),
  and the artifact itself is served in a CSP sandbox with an opaque origin and calls the API with a per-artifact
  token (design in architecture.md). **[tested]** `test_one_artifacts_token_cannot_reach_another`,
  `test_page_is_sandboxed_with_opaque_origin`.
  The cost: `localStorage`, `sessionStorage`, IndexedDB and cookies are unavailable to pages (they throw
  `SecurityError`, whereas they work in Claude). Pages keep state in `db`, which autosave makes natural.
  The sandbox also omits the popup flags, so `window.open` and `target=_blank` links do nothing (Claude's
  frame allows them); pages cannot open external sites. Checked in Chrome only; other browsers are untested.
  The shell/iframe structure itself is not browser-verified yet — a manual check that a page cannot navigate
  the top level is still owed (SECURITY.md known gap 1).
- **No sign-in, access by link.** Page access depends on possession of a link rather than a user account. Modes and
  limits are in the README (Security) and architecture.md.
- **Autosave, not submit.** Pages write to `db` as the user edits. `claude.submit()` only adds the push to the
  creating session. **[tested]** `test_submit_triggers_status_and_hook`
- **Content Security Policy.** Each page is served with a policy that uses the same script, style and font allowlist
  Claude's artifact frame uses, so pages that load common CDN libraries work. Frame-ancestor rules differ because the
  pages are reached over a tailnet, not claude.ai. A page that loads CDN assets needs internet access on the device
  that opens it. Pages still cannot send data to any origin other than their own. **[tested]**
  `test_page_is_sandboxed_with_opaque_origin`
- **Realtime.** Changes are delivered with a server-sent "something changed" event that triggers a refetch, with a
  30-second poll as a fallback.
- **Limits.** Documents are capped at 256 KiB, an artifact holds at most 25,000 documents totalling 50 MiB, and a
  page opens at most 8 event streams (429 beyond that). The first two match the contract's values. **[tested]**
  `store.py` enforces the caps; `test_sse_connections_per_artifact_are_capped`.
- **Error codes.** Errors reject with a `code`. The `db` API emits `invalid_argument`, `quota_exceeded` and `revoked`;
  the shim adds `unavailable` (the request failed), `not_granted` and `capability_removed`; `mcp` adds `bad_request`,
  `not_in_manifest`, `blocked_by_policy`, `approval_required`, `server_not_connected`, `server_unavailable`,
  `tool_error` and `upstream_error`. We do not emit `resource_exhausted`. Pages should treat any unknown code as a
  generic failure.
- **Update in place.** `artifact_update` replaces the HTML and keeps the database. There is no version history.
  Pages must tolerate documents written by an older version of themselves. **[tested]**
  `test_update_in_place_visible_on_reload`
- **Hot reload.** Not implemented. An updated page is seen on the next load.

## Not covered: what a page gets

- A `use()` call for a name not listed above resolves `null`.
- Calls on the `mcp` object that are not implemented (`listTools`, `describeTool`, `invalidate`, `watchTool`) fail with
  `capability_removed`.
- Calls on `artifact` or `self` reject with `not_granted`.
- Anything listed as not implemented resolves `null`, so a page that checks before use degrades cleanly.
