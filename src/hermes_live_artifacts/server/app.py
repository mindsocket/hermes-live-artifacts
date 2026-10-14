"""aiohttp listener: serves /a/<id> pages, /api/<id>/* (db, mcp), SSE. Loopback only; tailscale serve fronts it.

Isolation. All artifacts share one origin (Tailscale Serve gives one hostname), so the browser alone would let
page A read page B's data. Two mechanisms replace a separate origin per artifact:
  1. Pages are served with a CSP `sandbox` directive: the document gets an opaque origin, so it cannot read
     cookies, localStorage or IndexedDB, nor fetch another artifact's pages (no CORS, no credentials).
  2. The API is capability-based: each request carries the artifact's own token in the `x-artifact` header (which
     forces a CORS preflight), and a page only ever contains its own token. The API answers CORS with `*`
     and no credentials, since a sandboxed page has origin `null`.
Page access has two modes (`link_secret`). On: the link `/a/<id>?k=<token>` is exchanged for an HttpOnly cookie
scoped to /a/<id>, then redirects to the clean URL; the cookie only authorises loading that one page, and rotating
the token revokes link, cookie and API access. Off: the random id (122 bits) in `/a/<id>` is the only gate (security
by obscurity, for a trusted network); archive is then the way to revoke.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from collections.abc import Callable
from pathlib import Path

from aiohttp import web

from . import mcp as mcp_mod
from .store import MIN_UNGUESSABLE_ID, SUBMIT_DOC, DbError, Store

log = logging.getLogger("hermes_live_artifacts")
SHIM = (Path(__file__).parent / "shim.js").read_text()
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
    "base-uri 'self'; frame-ancestors 'self' http://127.0.0.1:* https://*.ts.net; "
    # Opaque origin (no allow-same-origin). Flags added so ordinary page features work: forms, downloads
    # (<a download>, the downloads capability), modals (confirm() in the downloads shim), and external links
    # opening in a new tab that is not itself sandboxed.
    "sandbox allow-scripts allow-forms allow-downloads allow-modals allow-popups allow-popups-to-escape-sandbox"
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
) -> web.Application:
    """link_secret=True: a page loads only with the artifact's token (`?k=`, then a path-scoped cookie).
    False: the long random id in the URL is the capability, so the URL needs no secret. Artifacts with a
    legacy short id always require the secret. The in-page API token is used in both modes."""
    app = web.Application(middlewares=[cors_middleware])

    background_tasks: set[asyncio.Task] = set()

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

    async def page(request: web.Request) -> web.StreamResponse:
        aid = request.match_info["aid"]
        needs_secret = link_secret or len(aid) < MIN_UNGUESSABLE_ID
        k = request.query.get("k")
        if not needs_secret:
            a = live(aid)
            if k is not None:  # a link that still carries a secret: drop it from the address bar
                raise web.HTTPFound(f"/a/{aid}")
        elif k is not None:
            a = authed(k, aid, fail_status=404)
            resp = web.HTTPFound(f"/a/{aid}")
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
        else:
            a = authed(request.cookies.get(f"ha_{aid}", ""), aid, fail_status=404)
        return web.Response(
            text=render_page(a),
            content_type="text/html",
            headers={
                "Content-Security-Policy": CSP,
                "Permissions-Policy": PERMISSIONS_POLICY,
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def api(request: web.Request) -> web.StreamResponse:
        aid, op = request.match_info["aid"], request.match_info["op"]
        a = authed(request.headers.get("x-artifact", ""), aid)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": {"code": "invalid_argument", "message": "bad json"}}, status=400)
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
                a = store.get_artifact(aid)  # stop streaming once revoked/archived
                if a is None or a["status"] == "archived" or not same_token(k, a["token"]):
                    break
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return resp

    async def health(_r: web.Request) -> web.Response:
        return web.json_response({"ok": True, "change_seq": store.change_seq()})

    app.router.add_get("/healthz", health)
    app.router.add_get("/a/{aid}", page)
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
    ):
        self.store, self.host, self.port, self.on_submit = store, host, port, on_submit
        self.resolve_host = resolve_host or (lambda h: h)
        self.retry_s = retry_s
        self.gate, self.gate_timeout_s = gate, gate_timeout_s
        self.mcp_call, self.link_secret = mcp_call, link_secret
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
            runner = web.AppRunner(make_app(self.store, self.on_submit, self.mcp_call, self.link_secret))
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
