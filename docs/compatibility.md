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

Claude's artifact tools are not part of the artifact API, so they are not carried over. The equivalents here are
`artifact_create`, `artifact_update`, `artifact_read`, `artifact_list`, `artifact_db_write`, `artifact_archive`,
`artifact_delete` and `artifact_export`.

Migrating an artifact means: copy its HTML into `artifact_create` (or `artifact_update` on an existing artifact),
then seed its database documents with `artifact_db_write`. The skills under `src/hermes_live_artifacts/skills/` teach the
agent this spelling.

## Differences

These follow from the design (single user, self-hosted, one origin), not from missing effort.

- **One user.** `user.id()` is a constant, `can()` and `isOwner()` are true, and `name()` returns a fixed value.
  Pages that branch on a missing user id take a different path here.
- **One origin, sandboxed pages.** All artifacts are served from one hostname, whereas Claude gives each its own
  origin. Here each page is served in a CSP sandbox with an opaque origin, so it cannot read cookies or other
  artifacts' pages, and the API accepts only the calling artifact's own token (sent in a header, embedded in its
  own page). **[tested]** `test_one_artifacts_token_cannot_reach_another`, `test_page_is_sandboxed_with_opaque_origin`.
  The cost: `localStorage`, `sessionStorage`, IndexedDB and cookies are unavailable to pages (they throw
  `SecurityError`, whereas they work in Claude). Pages keep state in `db`, which autosave makes natural.
  Checked in Chrome only; other browsers are untested.
- **Links and security.** Page access is configured with `link_secret`:
  - `never`: the URL is `/a/<id>` with a 22-character random id (128 bits, like Claude's). Whoever has the URL can
    open the page, so this suits a private network such as a tailnet. Revoke by archiving. **[tested]**
    `test_link_secret_off_serves_clean_url_without_k_or_cookie`
  - `always`: the link also carries a secret, `?k=<token>`, exchanged for a path-scoped cookie and removed from the
    address bar. Rotating the token revokes it. **[tested]** `test_link_exchange_redirects_to_clean_url_with_path_scoped_cookie`
  - `auto` (default): `always` unless both the bind address and `base_url` are loopback.
  Artifacts created by early versions have 12-character ids and always keep the secret.
  Not provided: sign-in. The listener has no user authentication, unlike the Hermes dashboard (which requires a
  provider on any non-loopback bind). Anyone who can reach the port and holds a link can read and write that
  artifact; a page's MCP access is limited to its declared tools and the operator's `mcp_allowlist`. Do not expose the
  listener to the public internet (no Funnel).
- **Autosave, not submit.** Pages write to `db` as the user edits. `claude.submit()` only adds the push to the
  creating session. **[tested]** `test_submit_triggers_status_and_hook`
- **Content Security Policy.** Each page is served with a policy that uses the same script, style and font allowlist
  Claude's artifact frame uses, so pages that load common CDN libraries work. Frame-ancestor rules differ because the
  pages are reached over a tailnet, not claude.ai. A page that loads CDN assets needs internet access on the device
  that opens it. Pages still cannot send data to any origin other than their own. **[tested]**
  `test_page_is_sandboxed_with_opaque_origin`
- **Realtime.** Changes are delivered with a server-sent "something changed" event that triggers a refetch, with a
  30-second poll as a fallback.
- **Limits.** Documents are capped at 256 KiB and an artifact holds at most 25,000 documents. These match the
  contract's values. **[tested]** `store.py` enforces both.
- **Error codes.** We emit `invalid_argument`, `quota_exceeded`, `revoked`, `unavailable` and `not_granted`. We do
  not emit `resource_exhausted` or `upstream_error`. Pages should treat any unknown code as a generic failure.
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

## Verifying a page

Page JavaScript has no automated browser tests. Changes to the shim or to a page need a manual check in a real
browser, on a phone and on a desktop. The server side is covered by `make test`.
