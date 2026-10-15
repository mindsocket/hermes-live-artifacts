# Security Policy and Threat Model

This document outlines the security architecture, threat model, trust boundaries, and verification evidence for `hermes-live-artifacts`.

---

## 1. Trust Boundaries & Core Assets

`hermes-live-artifacts` executes and serves interactive HTML/JavaScript applications authored dynamically by LLM agents. Because artifacts are rendered in operator and guest web browsers, strong security isolation is fundamental to preventing privilege escalation, credential theft, cross-artifact contamination, and unauthorized gateway operations.

### Assets to Protect
1. **Host & Agent Gateway**: Access to operator system shell, Hermes internal sessions, and unrestricted MCP tool execution.
2. **Artifact Persistence & Documents**: Data written by artifacts into SQLite (`artifacts.db`), including form submissions and sensitive state.
3. **Session Integrity**: In-flight chat sessions where submitted artifact data is delivered.
4. **Capability Tokens**: Bearer tokens granting read and write capabilities to artifact APIs.
5. **Private Tailnet / Network Perimeter**: Protection against unauthenticated access and enumeration from outside the intended network scope.

---

## 2. Threat Model Matrix

| Threat | Risk Level | Mitigation Control | Verification Evidence |
| :--- | :--- | :--- | :--- |
| **Cross-Artifact Contamination / Data Theft** | Critical | **Opaque Origin Sandbox & Capability Tokens**: Pages served with a CSP `sandbox` that omits `allow-same-origin` (opaque origin `null`). Custom `x-artifact` header required on all `/api/<id>/*` operations; credentials never accepted. | `test_one_artifacts_token_cannot_reach_another`<br>`test_cross_origin_post_without_the_token_header_is_rejected` |
| **Plaintext Bearer Token Leakage at Rest** | Medium | **HMAC derivation**: the API token is `HMAC-SHA256(installation key, id:salt)`. SQLite stores only a random salt, so a copy of `artifacts.db` alone does not contain working tokens. The installation key is `.secret` (0600, at least 32 bytes, created atomically, refused if short) or `HERMES_LIVE_ARTIFACTS_SECRET`. Limits: the backup includes `.secret` by design (see Backups), and in `never` mode the artifact id in the database opens the page, which embeds the token. | `test_only_a_salt_is_stored_not_the_token`<br>`test_key_is_created_once_private_and_reused`<br>`test_empty_or_short_key_file_is_refused_not_used`<br>`test_concurrent_first_starts_agree_on_one_key` |
| **Artifact ID Enumeration** | Low | **Unguessable ids** (22 characters, 122 random bits) and, for page loads, a uniform 404 for unknown, archived and (in `always` mode) unauthenticated requests. In `never` mode a live id with no `?k=` is served by design: the id is the credential. `/api/*` and `/events` answer 404 for an unknown or archived id and 403 for a wrong token, so they do distinguish a live id; at 122 bits that is not exploitable. | `test_unknown_artifact_404`<br>`test_bare_url_without_cookie_is_forbidden_and_wrong_token_too`<br>`test_non_ascii_token_is_refused_like_any_wrong_token` |
| **Host Compromise via Rogue MCP Calls** | Critical | **Two gates**: this plugin refuses any server/tool the artifact's manifest does not declare. The operator's `mcp_allowlist` is enforced by Hermes inside `ctx.call_mcp`; this repo's tests cover only the plugin's mapping of Hermes' refusal, not Hermes' own check. The allowlist is per server, so an agent-written manifest can declare any tool on an allowed server. | `test_mcp_refuses_undeclared_server_or_tool_without_calling`<br>`test_mcp_operator_allowlist_refusal_maps_to_blocked_by_policy` |
| **Prompt Injection via Artifact Submissions** | High | **Mitigation, not prevention**: the pushed message labels the JSON as user-supplied data, not instructions, and the data is JSON-encoded so it cannot close its code fence. That label is advice to the model, not a boundary. Anyone holding a link can submit, and in `never` mode that is anyone who has seen the URL. | `test_push_goes_to_creating_session_without_names_or_token` |
| **Network Exfiltration via In-Page Scripts** | High | **Partial**: CSP `connect-src 'self'` stops `fetch`, XHR, WebSocket and EventSource to other hosts, and `object-src 'none'` plus the Permissions-Policy close other paths. It does **not** stop navigation: the sandbox allows popups that escape it, so a page can open or navigate to an external URL carrying data, and WebRTC is not covered by CSP. Treat page code as able to send out anything it can read, including MCP results. See Known gaps. | `test_page_needs_the_token_and_embeds_it` (checks the CSP header only) |
| **Token Exposure in Browser History / Referrers** | Medium | **`always` mode**: `?k=<token>` is exchanged for a `SameSite=Lax`, `HttpOnly` cookie scoped to `Path=/a/<id>` (90 days) and removed from the address bar. The page's API token is separate and appears in the SSE URL (`/events?k=`); the listener's access log is off, so it reaches no log (known gap 3). `never` mode has no `?k=` on page links. | `test_link_exchange_redirects_to_clean_url_with_path_scoped_cookie` |

---

## 3. Security Architecture Details

### 3.1 CSP Sandboxing & Origin Isolation
Unlike Claude's native artifact architecture (which allocates independent subdomains for each artifact frame), `hermes-live-artifacts` runs on a single host origin (typically an internal Tailscale tailnet node IP or DNS name).

To achieve strict isolation across artifacts:
- Every artifact page response carries a `Content-Security-Policy` whose last directive is `sandbox ...` (the flag list is in architecture.md §5 and `app.py`).
- Omission of `allow-same-origin` forces the browser to treat each document as possessing a unique, opaque origin (`null`).
- Sandboxed documents cannot read or write `localStorage`, `sessionStorage`, `IndexedDB`, or browser cookies.
- Cross-artifact DOM navigation and script interaction are blocked by standard browser boundary enforcement.

### 3.2 Capability Tokens & HMAC Derivation
All state mutations and queries against `/api/<id>/*` require the per-artifact bearer capability token.
- **In-Memory Injection**: The derived token is injected directly into `window.__ARTIFACT__.token` at page render time.
- **At-Rest Protection**: The SQLite database stores only a random cryptographic salt in the `artifacts.salt` column. The actual bearer token is derived on demand:
  $$\text{BearerToken} = \text{Base64URL}(\text{HMAC-SHA256}(K_{\text{install}}, \text{aid} \parallel ":" \parallel \text{salt}))$$
  A copy of `artifacts.db` alone therefore holds no working token. The artifact ids and all document data are in it in plaintext, and in `never` mode an id is enough to open the page, which embeds the token. The scheme protects the API token against a database-only leak; it is not encryption at rest.
- **Installation key**: `.secret` in the data directory (or `HERMES_LIVE_ARTIFACTS_SECRET`). It is created with mode 0600 by writing a private temp file and hard-linking it into place, so concurrent first starts agree on one key and no reader sees a partial file. A key under 32 bytes is refused with an error instead of being used or replaced, because replacing it changes every token. To recover from a damaged file, delete it; every link and page token then changes.
- **Revocation**: archiving an artifact replaces its salt, which invalidates its links, cookies and API token at once. `Store.rotate_token` does the same without archiving, but no agent tool exposes it yet (issue #6), so archiving is the only revocation available today.

### 3.3 Not-found and forbidden responses
What the code does, measured with `link_secret` set both ways (unknown id, archived id, existing id with no / wrong / non-ASCII `k`):

| Request | `always` | `never` |
| :--- | :--- | :--- |
| Unknown or archived id | 404 | 404 |
| Existing id, no `k`, no cookie | 404 | **200** (the id is the credential) |
| Existing id, wrong or non-ASCII `k` | 404 | 302 to the clean URL (`k` is ignored) |
| Existing id, right `k` | 302 and cookie | 302 |
| `/api/<id>/*`, wrong or non-ASCII token | 403 | 403 |
| `/api/<id>/*`, unknown or archived id | 404 | 404 |

So "a request without the secret gets a 404" is true only in `always` mode. In `never` mode there is no secret to lack: a live id serves the page. Legacy 12-character ids always behave as `always`, because they are guessable.

Tokens are compared as bytes in constant time (`hmac.compare_digest`); a non-ASCII token is simply a wrong token and gives the same status as any other.

API routes return JSON errors with CORS headers, because a sandboxed page (origin `null`) would otherwise see only an opaque network error.

### 3.4 CORS on the API
The API answers `Access-Control-Allow-Origin: *` and answers every preflight with 204 without inspecting it. A page's origin is `null`, so no narrower value is possible and no credentials are involved. The browser therefore does not stop a page from calling another artifact's `/api/<id>/*`; the per-artifact token in `x-artifact` is the only barrier, and a page only ever contains its own.

### 3.5 Backups
`hermes backup` archives the whole Hermes directory, which includes `plugin-data/hermes-live-artifacts/`. The operator has tested that the archive contains both `artifacts.db` and `.secret`. This is a deliberate design choice: the same archive already carries Hermes' auth tokens and other secrets, so it has to be protected as a secret store, and keeping the key with the database is what makes a restore work (a restore without `.secret` would change every token). Do not copy the backup, or `artifacts.db` on its own, to a less-trusted place expecting the HMAC scheme to protect it.

---

## 4. Known gaps

From an independent audit on 2026-10-15. Items 1–2 were fixed the same day; items 3–8 are known and accepted. Evidence labels: measured = run against the code, observed = read in the code, hypothesis = not tested.

1. ~~**Data can leave by navigation or popup**~~ **Fixed**: `/a/<id>` is now a server-owned shell with the artifact in an iframe (`frame-src 'self'`) whose sandbox has no popup flags, so a page cannot navigate the top level or open an escaping popup. WebRTC remains outside CSP's reach (see the exfiltration row). Observed for the CSP; the browser behaviour is a hypothesis — verify manually in a browser.
2. ~~**A link holder can inject text into the agent session**~~ **Fixed**: the submit push is now a notice carrying only the artifact id and a document count — no title, no page-chosen strings. The agent reads the data with `artifact_read`, as a tool result.
3. **The API token is in the SSE URL** (accepted): `EventSource` cannot send headers, so the token rides in the `/events?k=` query. The listener's aiohttp access log is disabled (`AppRunner(access_log=None)`), so the token reaches no log; the only remaining exposure is a proxy in front of the listener, which the single-user, loopbound deployment does not have. Changing `shim.js` to a hand-parsed fetch stream was deferred (issue #9's territory, needs manual phone/desktop checks).
4. **The database and data directory are created with default permissions** (0644 database measured), so other local users can read artifact data. Single-user machines are not affected in practice.
5. **No connection or size caps on the event stream and storage** (observed): a page holding a token can load the gateway process.
6. **No tool rotates a link**; archive is the only revocation (issue #6).
7. **The event stream's change counter is global** (measured), so one artifact sees when others are written. `/healthz` exposes it without auth. Accepted: no data leaks, only write-activity timing.
8. **No `Host` header check** (measured: a foreign `Host` returns 200). Accepted: tailnet hostnames vary and the token is the real gate. The old `https://*.ts.net` frame-ancestors wildcard was removed with fix 1; allowed framers are now derived from `base_url` (see issue #2's comment).

## 5. Reporting Security Vulnerabilities

If you discover a security vulnerability in `hermes-live-artifacts`, please report it privately:
- Do not disclose issues publicly in GitHub Issues or pull requests.
- Contact the project maintainers directly via private repository disclosure or email.
- Include a reproducible proof-of-concept demonstrating the vulnerability and impact.
