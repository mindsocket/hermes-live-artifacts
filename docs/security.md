# Security Policy and Threat Model

This document outlines the security architecture, threat model, trust boundaries, and verification evidence for `hermes-live-artifacts`.

---

## 1. Trust Boundaries & Core Assets

`hermes-live-artifacts` executes and serves interactive HTML/JavaScript applications authored dynamically by LLM agents. Because artifacts are rendered in operator and guest web browsers, strong security isolation is fundamental to preventing privilege escalation, credential theft, cross-artifact contamination, and unauthorized gateway operations.

### Assets to Protect
1. **Host & Agent Gateway**: Access to operator system shell, Hermes internal sessions, and unrestricted MCP tool execution.
2. **Artifact Persistence & Documents**: Data written by artifacts into SQLite (`artifacts.db`), including form submissions and sensitive state.
3. **Session Integrity**: In-flight chat sessions where notify notices are delivered.
4. **Capability Tokens**: Bearer tokens granting read and write capabilities to artifact APIs.
5. **Private Tailnet / Network Perimeter**: Protection against unauthenticated access and enumeration from outside the intended network scope.

---

## 2. Threat Model Matrix

| Threat | Risk Level | Mitigation Control | Verification Evidence |
| :--- | :--- | :--- | :--- |
| **Cross-Artifact Contamination / Data Theft** | Critical | **Opaque Origin Sandbox & Capability Tokens**: Pages are framed by a server-owned shell and served with a CSP `sandbox` that omits `allow-same-origin` (opaque origin `null`). Custom `x-artifact` header required on all `/api/<id>/*` operations; credentials never accepted. | `test_one_artifacts_token_cannot_reach_another`<br>`test_cross_origin_post_without_the_token_header_is_rejected` |
| **Plaintext Bearer Token Leakage at Rest** | Medium | **HMAC derivation**: the API token is `HMAC-SHA256(installation key, id:salt)`. SQLite stores only a random salt, so a copy of `artifacts.db` alone does not contain working tokens. The installation key is `.secret` (0600, at least 32 bytes, created atomically, refused if short). Limits: the backup includes `.secret` by design (see Backups), and in `never` mode the artifact id in the database opens the page, which embeds the token. | `test_only_a_salt_is_stored_not_the_token`<br>`test_key_is_created_once_private_and_reused`<br>`test_empty_or_short_key_file_is_refused_not_used`<br>`test_concurrent_first_starts_agree_on_one_key` |
| **Artifact ID Enumeration** | Low | **Unguessable ids** (22 random characters) and, for page loads, a uniform 404 for unknown, archived and (in `always` mode) unauthenticated requests. In `never` mode a live id with no `?k=` is served by design: the id is the credential. `/api/*` and `/events` answer 404 for an unknown or archived id and for a wrong token alike (§3.3), so they do not distinguish a live id. | `test_unknown_artifact_404`<br>`test_bare_url_without_cookie_is_forbidden_and_wrong_token_too`<br>`test_api_requires_the_token_header`<br>`test_non_ascii_token_is_refused_like_any_wrong_token` |
| **Host Compromise via Rogue MCP Calls** | Critical | **Two gates**: this plugin refuses any server/tool the artifact's manifest does not declare. The operator's `mcp_allowlist` is enforced by Hermes inside `ctx.call_mcp`; this repo's tests cover only the plugin's mapping of Hermes' refusal, not Hermes' own check. The allowlist is per server, so an agent-written manifest can declare any tool on an allowed server. | `test_mcp_refuses_undeclared_server_or_tool_without_calling`<br>`test_mcp_operator_allowlist_refusal_maps_to_blocked_by_policy` |
| **Prompt Injection via Artifact Notifications** | Low | **Eliminated channel, residual risk**: the notify push is a fixed-template notice carrying only the artifact id and a document count — no title, no page-chosen strings — so nothing from the web page reaches the session as an injected message. The agent reads the data with `live_artifact_read`, as a tool result. A hostile page can still shape what the data *says* when read; treat it as untrusted input. Anyone holding a link can save and notify, and in `never` mode that is anyone who has seen the URL. | `test_push_is_a_notice_without_page_chosen_strings` |
| **Network Exfiltration via In-Page Scripts** | Medium | **Opaque-origin frame, popups stay sandboxed**: `/a/<id>` is a server-owned shell (`frame-src 'self'`) that frames the artifact, whose sandbox has `allow-popups` but not `allow-popups-to-escape-sandbox`, so a popup inherits the opaque origin and cannot navigate the top level — anything it opens can do nothing the frame itself cannot. The navigation path `connect-src 'self'` does not cover is thus contained. External links work only through the shim's interception (clicks and `window.open` are scheme-checked — script-bearing schemes refused outright, other non-http(s) schemes need per-scheme user confirmation — and the open happens inside the user's click, since activation does not survive a postMessage hop: measured). A page can still attempt to navigate **itself** to an external URL (measured: every mechanism does this page-destroyingly; see Known gaps 6). `connect-src` still blocks `fetch`, XHR, WebSocket and EventSource to other hosts; `object-src 'none'` and the Permissions-Policy close other paths. Residual: WebRTC is not covered by CSP (accepted, see Known gaps). Residual from `allow-popups`: an artifact may open sandboxed popups on user clicks (gesture-gated, never on load) — popup spam or look-alike content is possible. Treat page code as able to send out anything it can read, including MCP results. | `test_page_needs_the_token_and_embeds_it`<br>`test_page_is_sandboxed_with_opaque_origin`<br>`test_frame_intercepts_links_and_sandbox_allows_gestured_popups` |
| **Token Exposure in Browser History / Referrers** | Medium | **`always` mode**: `?k=<token>` is exchanged for a `SameSite=Lax`, `HttpOnly` cookie scoped to `Path=/a/<id>` (90 days) and removed from the address bar. The page's API token is separate and appears in the SSE URL (`/events?k=`); the listener's access log is off, so it reaches no log (accepted gap 3). `never` mode has no `?k=` on page links. `Referrer-Policy: no-referrer` on both shell and frame. | `test_link_exchange_redirects_to_clean_url_with_path_scoped_cookie` |

---

## 3. Security Architecture Details

### 3.1 Shell, CSP Sandboxing & Origin Isolation
Unlike Claude's native artifact architecture (which allocates independent subdomains for each artifact frame), `hermes-live-artifacts` runs on a single host origin (typically a Tailscale tailnet node).

To achieve strict isolation across artifacts:
- The link `/a/<id>` serves a **server-owned shell** whose only content is an iframe of the artifact at `/a/<id>/frame`. The shell sets `frame-src 'self'`, carries `frame-ancestors` derived from `base_url` (`'self'`, the base_url host on any port, loopback dev origins, and `file:` for the packaged Hermes Desktop renderer — never a wildcard; any local `file://` page can therefore frame the shell, which still needs the link secret or unguessable id), and may itself be framed by those origins only.
- The artifact document carries a `Content-Security-Policy` whose `sandbox` directive is `allow-scripts allow-forms allow-downloads allow-modals allow-popups` — no `allow-same-origin`, and **no `allow-popups-to-escape-sandbox`**: a popup created by the page inherits the opaque-origin sandbox, so it cannot read storage, cookies or the opener's DOM, and cannot navigate the top level. The popup flag exists so the shim can open an external link inside the user's click (activation does not survive a postMessage hop — measured). The shell's `frame-src` keeps the frame pointed at this host.
- Omission of `allow-same-origin` forces the browser to treat each document as possessing a unique, opaque origin (`null`).
- Sandboxed documents cannot read or write `localStorage`, `sessionStorage`, `IndexedDB`, or browser cookies.
- Cross-artifact DOM navigation and script interaction are blocked by standard browser boundary enforcement.

### 3.2 Capability Tokens & HMAC Derivation
All state mutations and queries against `/api/<id>/*` require the per-artifact bearer capability token.
- **In-Memory Injection**: The derived token is injected directly into `window.__ARTIFACT__.token` at page render time.
- **At-Rest Protection**: The SQLite database stores only a random cryptographic salt in the `artifacts.salt` column. The actual bearer token is derived on demand:
  $$\text{BearerToken} = \text{Base64URL}(\text{HMAC-SHA256}(K_{\text{install}}, \text{aid} \parallel ":" \parallel \text{salt}))$$
  A copy of `artifacts.db` alone therefore holds no working token. The artifact ids and all document data are in it in plaintext, and in `never` mode an id is enough to open the page, which embeds the token. The scheme protects the API token against a database-only leak; it is not encryption at rest.
- **Installation key**: `.secret` in the data directory. It is created with mode 0600 by writing a private temp file and hard-linking it into place, so concurrent first starts agree on one key and no reader sees a partial file. A key under 32 bytes is refused with an error instead of being used or replaced, because replacing it changes every token. To recover from a damaged file, delete it; every link and page token then changes.
- **Data directory permissions**: created 0700 (existing directories are tightened on start); a new database is created 0600. Sidecar files (WAL/SHM) inherit the directory's protection.
- **Index token**: with `index: true`, `/index` is gated by its own token (same derivation, salt in the `secrets` table), exchanged for an `HttpOnly`, `SameSite=Lax` cookie scoped to `/index`. It opens every active artifact, since the list embeds their `?k`. The cookie is `Secure` only over https (or `x-forwarded-proto: https`), so on a plain-http tailnet it, like `?k=` links, crosses the wire in cleartext; front the listener with `tailscale serve` (https) if that matters. `hermes live-artifacts index-link --rotate` revokes it.
- **Revocation**: rotating (dashboard tab only; no agent tool) replaces the salt, which invalidates the link (`?k=`), the page cookie, the API token and open event streams at once, and yields a fresh link. It is refused in `link_secret: never` mode, where the URL id is the credential and rotation cannot revoke the link — archiving is the revocation there. Archiving always replaces the salt too.

### 3.2a Dashboard tab
The Artifacts tab (`dashboard/plugin_api.py`) is an operator surface inside the Hermes dashboard. It runs in the dashboard process and is gated by the dashboard's own session auth, applied before any route here; the plugin adds no second scheme.
- It can read every artifact's documents and mint a link (token included) for the one artifact opened, so it is exactly as trusted as the dashboard session. The list endpoint and export never include a token.
- The preview embeds the shell (`/a/<id>`) in an iframe whose `sandbox` has no `allow-same-origin`; the shell is already a different origin from the dashboard (different port), and the shell's `frame-ancestors` admits the `base_url` host on any port (§3.1). Rotation is not offered in `never` mode and the dashboard API refuses it with 409 (`test_http_rotate_refused_in_never_mode`).
- After a rotation, pages already open keep their old API token and get 404 from the API (as for any wrong token, §3.3) and a closed event stream (`test_revoke_kills_old_link_and_token`). There is no in-page "link rotated" message; the user reopens the new link.
- The Hermes Desktop half (`plugin/desktop/plugin.js`) calls this same backend, so it has the same trust level and the same preview embedding; the only difference is that the embedder is the packaged renderer, a `file://` page, admitted by `file:` in `frame-ancestors` (§3.1).

### 3.3 Not-found and forbidden responses
What the code does, measured with `link_secret` set both ways (unknown id, archived id, existing id with no / wrong / non-ASCII `k`):

| Request | `always` | `never` |
| :--- | :--- | :--- |
| Unknown or archived id | 404 | 404 |
| Existing id, no `k`, no cookie | 404 | **200** (the id is the credential) |
| Existing id, wrong or non-ASCII `k` | 404 | 302 to the clean URL (`k` is ignored) |
| Existing id, right `k` | 302 and cookie | 302 |
| `/api/<id>/*`, wrong or non-ASCII token | 404 | 404 |
| `/api/<id>/*`, unknown or archived id | 404 | 404 |

So "a request without the secret gets a 404" is true only in `always` mode. In `never` mode there is no secret to lack: a live id serves the page.

Tokens are compared as bytes in constant time (`hmac.compare_digest`); a non-ASCII token is simply a wrong token and gives the same status as any other.

API routes return JSON errors with CORS headers, because a sandboxed page (origin `null`) would otherwise see only an opaque network error. Request bodies must be JSON objects (400 otherwise) and are capped at 300 KiB (413 beyond).

### 3.4 CORS on the API
The API answers `Access-Control-Allow-Origin: *` and answers every preflight with 204 without inspecting it. A page's origin is `null`, so no narrower value is possible and no credentials are involved. The browser therefore does not stop a page from calling another artifact's `/api/<id>/*`; the per-artifact token in `x-artifact` is the only barrier, and a page only ever contains its own.

### 3.5 Resource Limits
- **Event streams**: at most 8 concurrent SSE connections per artifact; further requests get 429.
- **Documents**: one document at most 256 KiB; an artifact holds at most 25,000 documents totalling 50 MiB (`quota_exceeded` beyond).
- **API request bodies**: capped at 300 KiB before reading (413).
- **Access log**: disabled on the listener (`AppRunner(access_log=None)`), so query-string tokens are never logged.

### 3.6 Backups
`hermes backup` archives the whole Hermes directory, which includes `plugin-data/live-artifacts/`. The operator has tested that the archive contains both `artifacts.db` and `.secret`. This is a deliberate design choice: the same archive already carries Hermes' auth tokens and other secrets, so it has to be protected as a secret store, and keeping the key with the database is what makes a restore work (a restore without `.secret` would change every token). Do not copy the backup, or `artifacts.db` on its own, to a less-trusted place expecting the HMAC scheme to protect it.

---

## 4. Known, accepted gaps

Accepted residual risks, reviewed 2026-10-15. Not fixed, by design.

1. **The API token is in the SSE URL**: `EventSource` cannot send headers, so the token rides in the `/events?k=` query. The access log is off, so it reaches no log; the only exposure is a proxy in front of the listener, which the single-user, loopbound deployment does not have. Moving to a fetch-based stream would rewrite `shim.js` for every artifact and needs manual phone/desktop checks — not changed.
2. **WebRTC is not covered by CSP**: a page with a token could establish WebRTC connections whose candidates carry data. Not closed; treated as covered by "treat page code as able to send out anything it can read".
3. **The event stream's change counter is global** (measured): `change_seq` is one number for all artifacts, so every page's stream sees writes to *any* artifact, and `/healthz` exposes it without auth. No data leaks — only write-activity timing and re-sync traffic.
4. **No `Host` header check** (measured: a foreign `Host` returns 200). Accepted: tailnet hostnames vary and the token is the real gate.
5. **Query filtering runs in Python**: `store.query` reads all of a collection's rows before filtering and limiting in memory, so a large collection costs proportional work per query. Filtering and `limit` are not pushed into SQL.
6. **Self-navigation of the artifact frame** (measured, live probe): a page can attempt to navigate itself to an external URL, carrying data out in the URL. Top-level navigation throws (sandbox), and CSP directives (`frame-src`, `form-action`) plus the shell's `frame-ancestors` refuse cross-origin targets — but every attempted navigation destroys the artifact page whether it is blocked or commits, and the two are not distinguishable in practice. Blast radius is contained to the frame. Distinguishing blocked from escaped would need a nested-iframe observer harness (not built). Separately, the shim's link interception is shim-enforced, not page-proof: a page that bypasses it (e.g. calling `parent.postMessage` directly) reaches only the shell, and the shim's own `window.open` path is available only to pages using the runtime — the enforcement is best-effort at the DOM boundary.

## 5. Reporting Security Vulnerabilities

If you discover a security vulnerability in `hermes-live-artifacts`, please report it privately:
- Do not disclose issues publicly in GitHub Issues or pull requests.
- Contact the project maintainers directly via private repository disclosure or email.
- Include a reproducible proof-of-concept demonstrating the vulnerability and impact.
