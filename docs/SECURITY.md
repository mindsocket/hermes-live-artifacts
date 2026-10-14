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
| **Cross-Artifact Contamination / Data Theft** | Critical | **Opaque Origin Sandbox & Capability Tokens**: Pages served with CSP `sandbox allow-scripts` (opaque origin `null`). Custom `x-artifact` header required on all `/api/<id>/*` operations; credentials never accepted. | `test_one_artifacts_token_cannot_reach_another`<br>`test_cross_origin_post_without_the_token_header_is_rejected` |
| **Plaintext Bearer Token Leakage at Rest** | High | **HMAC Key Derivation at Rest**: Bearer capability tokens are dynamically derived using HMAC-SHA256 from a local installation secret (`.secret` mode 0600 or `HERMES_LIVE_ARTIFACTS_SECRET`) combined with artifact ID and salt. SQLite stores only random salts; plaintext tokens are never persisted. | `test_token_at_rest_is_not_plaintext`<br>`test_rotate_token` |
| **Artifact ID Enumeration / Timing Oracles** | Medium | **Uniform Fail-Closed 404 Responses**: Unauthorized page link requests (`/a/<id>`) return an identical `404 Not Found` response whether the ID is non-existent, archived, unauthenticated, or supplied with an invalid token. | `test_bare_url_without_cookie_is_forbidden_and_wrong_token_too`<br>`test_unknown_artifact_404` |
| **Host Compromise via Rogue MCP Calls** | Critical | **Double-Gated Allowlist**: Artifacts must explicitly declare allowed MCP servers and tools in `capabilities.mcp`. The operator must additionally configure `mcp_allowlist`. Tool execution is refused prior to gateway dispatch if not dual-approved. | `test_mcp_refuses_undeclared_server_or_tool_without_calling`<br>`test_mcp_operator_allowlist_refusal_maps_to_blocked_by_policy` |
| **Prompt Injection via Artifact Submissions** | High | **Strict Data Framing**: Artifact submissions (`claude.submit()`) are framed purely as passive data envelopes (`[artifact submitted]`) with JSON payloads, rather than raw instruction prompts, preventing prompt escape into agent contexts. | `test_push_goes_to_creating_session_without_names_or_token` |
| **Network Exfiltration via In-Page Scripts** | High | **Restricted Content Security Policy**: CSP enforces `connect-src 'self' blob: ...` and blocks arbitrary network calls. `object-src 'none'` and restrictive `Permissions-Policy` disables camera, microphone, geolocation, and USB APIs. | `test_page_needs_the_token_and_embeds_it` |
| **Token Exposure in Browser History / Referrers** | Medium | **Path-Scoped Ephemeral Cookies**: URL tokens (`?k=<token>`) are exchanged on first navigation for a `SameSite=Lax`, `HttpOnly` cookie scoped strictly to `Path=/a/<id>`, and stripped immediately from the browser location bar. | `test_link_exchange_redirects_to_clean_url_with_path_scoped_cookie` |

---

## 3. Security Architecture Details

### 3.1 CSP Sandboxing & Origin Isolation
Unlike Claude's native artifact architecture (which allocates independent subdomains for each artifact frame), `hermes-live-artifacts` runs on a single host origin (typically an internal Tailscale tailnet node IP or DNS name).

To achieve strict isolation across artifacts:
- Every artifact page response carries:
  ```http
  Content-Security-Policy: sandbox allow-scripts allow-forms allow-downloads allow-modals allow-popups allow-popups-to-escape-sandbox; default-src 'self'; ...
  ```
- Omission of `allow-same-origin` forces the browser to treat each document as possessing a unique, opaque origin (`null`).
- Sandboxed documents cannot read or write `localStorage`, `sessionStorage`, `IndexedDB`, or browser cookies.
- Cross-artifact DOM navigation and script interaction are blocked by standard browser boundary enforcement.

### 3.2 Capability Tokens & HMAC Derivation
All state mutations and queries against `/api/<id>/*` require the per-artifact bearer capability token.
- **In-Memory Injection**: The derived token is injected directly into `window.__ARTIFACT__.token` at page render time.
- **At-Rest Protection**: The SQLite database stores only a random cryptographic salt in the `artifacts.token` column. The actual bearer token is derived on demand:
  $$\text{BearerToken} = \text{Base64URL}(\text{HMAC-SHA256}(K_{\text{install}}, \text{aid} \parallel ":" \parallel \text{salt}))$$
  Even if an operator's `artifacts.db` backup is extracted or shared, bearer tokens cannot be derived or replayed without access to the host's `.secret` file.
- **Revocation**: Rotating an artifact's token or archiving the artifact updates the salt, instantly invalidating all issued links, browser cookies, and active page sessions.

### 3.3 Fail-Closed Uniform Responses
To prevent enumeration of artifact identifiers across the local network:
- Missing token query parameter (`/a/<id>`) $\to$ `404 Not Found`.
- Malformed or invalid token query parameter (`/a/<id>?k=invalid`) $\to$ `404 Not Found`.
- Archived or non-existent artifact $\to$ `404 Not Found`.
- Missing or invalid path cookie on direct load $\to$ `404 Not Found`.

API routes (`/api/<id>/*`) return structured JSON error payloads with `403 Forbidden` when bearer capabilities are omitted or mismatched, accompanied by CORS headers so clients can handle authorization failures gracefully.

---

## 4. Reporting Security Vulnerabilities

If you discover a security vulnerability in `hermes-live-artifacts`, please report it privately:
- Do not disclose issues publicly in GitHub Issues or pull requests.
- Contact the project maintainers directly via private repository disclosure or email.
- Include a reproducible proof-of-concept demonstrating the vulnerability and impact.
