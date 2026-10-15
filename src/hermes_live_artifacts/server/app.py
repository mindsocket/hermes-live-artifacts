"""aiohttp listener: serves /a/<id> pages, /api/<id>/* (db, mcp), SSE. Loopback only; tailscale serve fronts it.

Isolation. All artifacts share one origin (Tailscale Serve gives one hostname), so the browser alone would let
page A read page B's data. Three mechanisms replace a separate origin per artifact:
  1. `/a/<id>` is a server-owned shell (its own origin, normal CSP) whose only content is an iframe of
     `/a/<id>/frame`, the artifact itself. The shell's `frame-ancestors` says who may embed the shell, and its
     `frame-src 'self'` keeps the inner frame pointed at us. The artifact document is served with a CSP
     `sandbox` that omits `allow-same-origin` (opaque origin: it cannot read cookies, localStorage or
     IndexedDB) and omits both popup flags, so it cannot navigate the top level or open an escaping popup
     (it can navigate its own frame; see docs/security.md known gap 6) —
     navigation is the one exfiltration path `connect-src 'self'` does not cover.
  2. The API is capability-based: each request carries the artifact's own token in the `x-artifact` header (which
     forces a CORS preflight), and a page only ever contains its own token. The API answers CORS with `*`
     and no credentials, since a sandboxed page has origin `null`.
  3. `frame_ancestors_from(base_url)` derives who may frame the shell: `'self'`, the `base_url` host on any port
     (the Hermes dashboard lives on the same tailnet host, unknown port), and loopback dev origins.
Page access has two modes (`link_secret`). On: the link `/a/<id>?k=<token>` is exchanged for an HttpOnly cookie
scoped to /a/<id>, then redirects to the clean URL; the cookie only authorises loading that one page, and rotating
the token revokes link, cookie and API access. Off: the unguessable random id in `/a/<id>` is the only gate (security
by obscurity, for a trusted network); archive is then the way to revoke.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

from . import mcp as mcp_mod
from .store import SUBMIT_DOC, DbError, Store, needs_link_secret

log = logging.getLogger("hermes_live_artifacts")
SHIM = (Path(__file__).parent / "shim.js").read_text()
# POST bodies: one API call carries a path plus one document (256 KiB cap in the validator); anything
# much larger is a mistake or an attack, so answer 413 before reading it.
MAX_BODY_BYTES = 300 * 1024
# Open SSE streams per artifact. Each stream wakes once a second, so an unbounded page (or a hostile
# one holding its token) could pile up loops; 8 is well above any real page's listener count.
MAX_SSE_PER_ARTIFACT = 8
# The reset every page fragment is wrapped in. Pages rely on it: 14px system font, zero body margin, safe-area padding on :root, and a
# [hidden] rule that beats author display values. We add no design tokens of our own.
RESET_CSS = (
    (
        """
:root{color-scheme:light;box-sizing:border-box;padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}
html{scroll-padding-top:env(safe-area-inset-top,0px)}
body{margin:0;padding:0;font:14px -apple-system,BlinkMacSystemFont,sans-serif;background:#faf9f5;color:#141413}
img{max-width:100%}
[hidden]:not([hidden=until-found i]){display:none!important}
"""
    )
    .strip()
    .replace("\n", "")
)


def render_page(a: dict) -> str:
    mcp = (a["capabilities"].get("mcp") or {}).get("servers") or []
    cfg = json.dumps(
        {"id": a["id"], "caps": sorted(a["capabilities"].keys()), "token": a["token"], "mcp": mcp}
    ).replace("</", "<\\/")
    inject = f"<script>window.__ARTIFACT__={cfg};</script><script>{SHIM}</script>"
    html = a["html"]
    low = html.lower()
    # A complete document keeps its own head; the runtime is injected at the top of it.
    i = low.find("<head>")
    if i >= 0:
        return html[: i + 6] + inject + html[i + 6 :]
    j = low.find("<html")
    if j >= 0:
        k = html.find(">", j)
        return html[: k + 1] + "<head>" + inject + "</head>" + html[k + 1 :]
    # A fragment gets charset + viewport-fit=cover + reset, with its content in <body>.
    return (
        "<!doctype html><html><head><meta charset=utf8>"
        '<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">'
        f"<style>{RESET_CSS}</style>{inject}</head><body>{html}</body></html>"
    )


# Script and style allowlist for CDN libraries (React, Chart.js, Tailwind, Google Fonts). connect-src stays 'self': page code cannot send data
# anywhere else, which is what keeps the looser script/style sources from being an exfiltration path.
CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval' blob: https://cdnjs.cloudflare.com "
    "https://cdn.jsdelivr.net/pyodide/ https://cdn.jsdelivr.net/gh/python-visualization/ "
    "https://cdn.jsdelivr.net/npm/ https://cdn.tailwindcss.com https://code.jquery.com https://unpkg.com "
    "https://cdn.plot.ly; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "img-src 'self' data: blob:; font-src 'self' data: blob: https://fonts.gstatic.com; "
    "media-src 'self' data: blob:; connect-src 'self' blob: https://fonts.googleapis.com https://fonts.gstatic.com; "
    "worker-src 'self' blob:; form-action 'self'; frame-src 'self' blob: data:; object-src 'none'; "
    "base-uri 'self'"
)
# The artifact document: opaque origin (no allow-same-origin) and NO popup flags — `allow-popups` /
# `allow-popups-to-escape-sandbox` are deliberately absent, so a sandboxed popup (which could navigate
# itself to an external URL carrying data) cannot be created. Flags kept for ordinary page features:
# forms, downloads (<a download>, the downloads capability) and modals (confirm() in the downloads shim).
ARTIFACT_SANDBOX = "sandbox allow-scripts allow-forms allow-downloads allow-modals"
# The shell is a top-level document we own: it frames only 'self' (the artifact iframe) and may itself be
# framed by the origins derived from base_url (see frame_ancestors_from).
SHELL_CSP = "default-src 'self'; style-src 'unsafe-inline'; frame-src 'self'; object-src 'none'; base-uri 'self'"


def frame_ancestors_from(base_url: str) -> str:
    """Who may embed the shell: 'self', the base_url host on any port (the Hermes dashboard shares the
    tailnet host; its port is not known to the plugin), and loopback dev origins. Derived, not configured."""
    hosts = ["'self'", "http://127.0.0.1:*", "http://localhost:*"]
    pub = (urlsplit(base_url).hostname or "") if base_url else ""
    if pub and pub not in ("127.0.0.1", "localhost"):
        scheme = urlsplit(base_url).scheme or "https"
        hosts.append(f"{scheme}://{pub}:*")
    return "frame-ancestors " + " ".join(hosts)


def render_shell(a: dict, ancestors: str) -> str:
    aid = a["id"]
    return (
        "<!doctype html><html><head><meta charset=utf8>"
        '<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">'
        f"<title>{a['title']}</title>"
        "<style>html,body{margin:0;height:100%}iframe{display:block;width:100%;height:100%;border:0}"
        "</style></head><body>"
        f'<iframe src="/a/{aid}/frame" title="{a["title"]}"'
        ' sandbox="allow-scripts allow-forms allow-downloads allow-modals"></iframe>'
        f"</body></html><!-- {aid} {ancestors} -->"
    )


# The sandboxed page has origin `null`; no credentials are ever sent, so `*` is safe: the token header is the auth.
CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "x-artifact, content-type",
    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
    "Access-Control-Max-Age": "600",
}
PERMISSIONS_POLICY = (
    "camera=(), microphone=(), geolocation=(), display-capture=(), usb=(), hid=(), serial=(), "
    "bluetooth=(), midi=(), payment=(), idle-detection=()"
)


def same_token(given: str, real: str) -> bool:
    """Constant-time compare on bytes: compare_digest raises TypeError on a non-ASCII str, which would be a 500."""
    return hmac.compare_digest((given or "").encode(), real.encode())


@web.middleware
async def cors_middleware(request: web.Request, handler):
    if not request.path.startswith("/api/"):
        return await handler(request)
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=CORS)
    try:
        resp = await handler(request)
    except web.HTTPException as e:  # 403/404 must carry CORS too, or the page only sees an opaque network error
        e.headers.update(CORS)
        raise
    if not resp.prepared:
        resp.headers.update(CORS)
    return resp


def make_app(
    store: Store,
    on_submit: Callable[[dict], None] | None = None,
    mcp_call: Callable[..., dict] | None = None,
    link_secret: bool = True,
    base_url: str = "",
) -> web.Application:
    """link_secret=True: a page loads only with the artifact's token (`?k=`, then a path-scoped cookie).
    False: the long random id in the URL is the capability, so the URL needs no secret. Artifacts with a
    legacy short id always require the secret. The in-page API token is used in both modes.
    base_url feeds the shell's frame-ancestors (dashboard host)."""
    app = web.Application(middlewares=[cors_middleware])
    ancestors = frame_ancestors_from(base_url)

    background_tasks: set[asyncio.Task] = set()
    sse_counts: dict[str, int] = {}  # open event streams per artifact

    def live(aid: str) -> dict:
        a = store.get_artifact(aid)
        if a is None or a["status"] == "archived":
            raise web.HTTPNotFound(text="not found")
        return a

    def authed(token: str, aid: str, fail_status: int = 403) -> dict:
        a = live(aid)
        if not same_token(token, a["token"]):
            if fail_status == 404:
                raise web.HTTPNotFound(text="not found")
            raise web.HTTPForbidden(text="forbidden")
        return a

    def auth_from_request(request: web.Request, aid: str, needs_secret: bool) -> dict:
        k = request.query.get("k")
        here = request.path  # the ?k strip redirect must stay on the current path (shell or frame)
        if not needs_secret:
            a = live(aid)
            if k is not None:  # a link that still carries a secret: drop it from the address bar
                raise web.HTTPFound(here)
            return a
        if k is not None:
            a = authed(k, aid, fail_status=404)
            resp = web.HTTPFound(here)
            # Cookie scoped to this one page path: it authorises reloading the page and nothing else. Lax, not
            # Strict: the link is opened from another app (Telegram), and Strict is withheld on that navigation.
            resp.set_cookie(
                f"ha_{aid}",
                a["token"],
                httponly=True,
                samesite="Lax",
                path=f"/a/{aid}",
                max_age=86400 * 90,
                secure=request.secure or request.headers.get("x-forwarded-proto") == "https",
            )
            raise resp
        return authed(request.cookies.get(f"ha_{aid}", ""), aid, fail_status=404)

    async def shell(request: web.Request) -> web.StreamResponse:
        """Top-level page the link points at. Own origin, so it may hold the frame; the artifact runs inside."""
        aid = request.match_info["aid"]
        a = auth_from_request(request, aid, needs_link_secret(aid, link_secret))
        return web.Response(
            text=render_shell(a, ancestors),
            content_type="text/html",
            headers={
                "Content-Security-Policy": f"{SHELL_CSP}; {ancestors}",
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def page(request: web.Request) -> web.StreamResponse:
        """The artifact document itself, framed by `shell` (frame-src 'self' there keeps it that way)."""
        aid = request.match_info["aid"]
        a = auth_from_request(request, aid, needs_link_secret(aid, link_secret))
        return web.Response(
            text=render_page(a),
            content_type="text/html",
            headers={
                "Content-Security-Policy": f"{CSP}; {ARTIFACT_SANDBOX}",
                "Permissions-Policy": PERMISSIONS_POLICY,
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def api(request: web.Request) -> web.StreamResponse:
        aid, op = request.match_info["aid"], request.match_info["op"]
        a = authed(request.headers.get("x-artifact", ""), aid)
        if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
            return web.json_response(
                {"error": {"code": "payload_too_large", "message": f"request body exceeds {MAX_BODY_BYTES} bytes"}},
                status=413,
            )
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": {"code": "invalid_argument", "message": "bad json"}}, status=400)
        if not isinstance(body, dict):
            # A non-object body (list, string, number) would 500 later in _dispatch; reject it here.
            return web.json_response(
                {"error": {"code": "invalid_argument", "message": "request body must be a JSON object"}}, status=400
            )
        if op == "mcp/call":
            try:
                return web.json_response({"result": await asyncio.to_thread(mcp_mod.call_tool, a, body, mcp_call)})
            except mcp_mod.McpError as e:
                return web.json_response({"error": e.to_dict()}, status=e.status)
        try:
            res = await asyncio.to_thread(_dispatch, store, aid, op, body)
        except DbError as e:
            status = {"invalid_argument": 400, "quota_exceeded": 413, "revoked": 410}.get(e.code, 500)
            return web.json_response({"error": e.to_dict()}, status=status)
        except (KeyError, TypeError, ValueError) as e:
            return web.json_response({"error": {"code": "invalid_argument", "message": str(e)}}, status=400)
        is_submit = (
            op in ("db/set", "db/update")
            and body.get("path") == SUBMIT_DOC
            and (body.get("data") or {}).get("status") == "submitted"
        )
        if is_submit and on_submit:

            async def _fire() -> None:
                # Off the event loop: the hook may shell out (`hermes send`). Never fails the request.
                try:
                    await asyncio.to_thread(on_submit, store.get_artifact(aid))
                except Exception:
                    log.exception("on_submit hook failed")

            task = asyncio.create_task(_fire())
            background_tasks.add(task)
            task.add_done_callback(background_tasks.discard)
        return web.json_response(res)

    async def events(request: web.Request) -> web.StreamResponse:
        aid, k = request.match_info["aid"], request.query.get("k", "")
        authed(k, aid)
        n = sse_counts.get(aid, 0)
        if n >= MAX_SSE_PER_ARTIFACT:
            raise web.HTTPTooManyRequests(text="too many event streams for this artifact")
        sse_counts[aid] = n + 1
        try:
            return await _stream_events(request, aid, k)
        finally:
            sse_counts[aid] -= 1
            if sse_counts[aid] <= 0:
                del sse_counts[aid]

    async def _stream_events(request: web.Request, aid: str, k: str) -> web.StreamResponse:
        resp = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                **CORS,
            }
        )
        await resp.prepare(request)
        last = -1
        try:
            while True:
                seq = await asyncio.to_thread(store.change_seq)
                if seq != last:
                    last = seq
                    await resp.write(b"event: change\ndata: %d\n\n" % seq)
                else:
                    await resp.write(b": ping\n\n")
                await asyncio.sleep(1.0)
                a = await asyncio.to_thread(store.get_artifact, aid)  # stop streaming once revoked/archived
                if a is None or a["status"] == "archived" or not same_token(k, a["token"]):
                    break
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return resp

    async def health(_r: web.Request) -> web.Response:
        return web.json_response({"ok": True, "change_seq": store.change_seq()})

    app.router.add_get("/healthz", health)
    app.router.add_get("/a/{aid}", shell)
    app.router.add_get("/a/{aid}/frame", page)
    app.router.add_get("/api/{aid}/events", events)
    app.router.add_post("/api/{aid}/{op:.+}", api)
    return app


def _dispatch(store: Store, aid: str, op: str, b: dict) -> dict:
    if op == "db/get":
        return {"doc": store.get_doc(aid, b.get("path"))}
    if op == "db/set":
        store.set_doc(aid, b.get("path"), b.get("data"))
        return {}
    if op == "db/update":
        store.update_doc(aid, b.get("path"), b.get("data"))
        return {}
    if op == "db/delete":
        store.delete_doc(aid, b.get("path"))
        return {}
    if op == "db/query":
        return {"docs": store.query(aid, b.get("path"), b.get("filters"), b.get("orderBy"), b.get("limit"))}
    if op == "db/acquire":
        return {"result": store.acquire(aid, b.get("path"), b.get("holder"), b.get("ttlMs") or 0, b.get("data"))}
    raise DbError("invalid_argument", f"unknown op {op}")


class Listener:
    """Owns the aiohttp runner; start/stop are coroutines so ctx.spawn_task can supervise it."""

    RETRY_MAX_S = 60.0

    def __init__(
        self,
        store: Store,
        host: str = "127.0.0.1",
        port: int = 47318,
        on_submit=None,
        resolve_host: Callable[[str], str] | None = None,
        retry_s: float = 2.0,
        gate: Callable[[], bool] | None = None,
        gate_timeout_s: float = 120.0,
        mcp_call: Callable[..., dict] | None = None,
        link_secret: bool = True,
        base_url: str = "",
    ):
        self.store, self.host, self.port, self.on_submit = store, host, port, on_submit
        self.resolve_host = resolve_host or (lambda h: h)
        self.retry_s = retry_s
        self.gate, self.gate_timeout_s = gate, gate_timeout_s
        self.mcp_call, self.link_secret = mcp_call, link_secret
        self.base_url = base_url
        self.runner: web.AppRunner | None = None
        self.bound: str | None = None

    async def _wait_gate(self) -> bool:
        """Wait (bounded) until `gate()` is true. Used so a process only binds once it has proven it is the
        right one, e.g. the gateway after it rewrites its pid file. Returns False on timeout."""
        if self.gate is None:
            return True
        waited = 0.0
        while not self.gate():
            if waited >= self.gate_timeout_s:
                log.info("hermes-live-artifacts: gate not satisfied after %.0fs; this process will not listen", waited)
                return False
            await asyncio.sleep(0.5)
            waited += 0.5
        log.info("hermes-live-artifacts: gate open after %.1fs; binding", waited)
        return True

    async def run_forever(self) -> None:
        """Bind (retrying with backoff: tailscaled may not be up at boot, or another process may hold the
        port), then serve until cancelled. Never raises on a failed bind, so a plugin cannot crash its host."""
        if not await self._wait_gate():
            return
        delay = self.retry_s
        while True:
            runner = web.AppRunner(
                make_app(self.store, self.on_submit, self.mcp_call, self.link_secret, self.base_url),
                access_log=None,
            )
            try:
                host = await asyncio.to_thread(self.resolve_host, self.host)
                await runner.setup()
                await web.TCPSite(runner, host, self.port, reuse_address=True).start()
                self.runner, self.bound = runner, f"{host}:{self.port}"
                break
            except OSError as e:
                await runner.cleanup()
                log.warning(
                    "hermes-live-artifacts cannot bind %s:%s (%s); retrying in %.0fs", self.host, self.port, e, delay
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.RETRY_MAX_S)
        log.info("hermes-live-artifacts listening on %s", self.bound)
        try:
            await asyncio.Event().wait()
        finally:
            await self.runner.cleanup()
            log.info("hermes-live-artifacts listener stopped")
