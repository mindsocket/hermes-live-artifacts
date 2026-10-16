"""Playwright's webServer: the real aiohttp app over a throwaway store holding one fixture artifact.

Usage: uv run python e2e/serve_fixture.py <port>

Test-only routes live here, not in the product: POST /__e2e/artifact (a fresh fixture artifact per test: id, token), GET
/__e2e/artifact/<id> (status and notified_at) and /__e2e/docs/<id> (what the store holds, to assert persistence independently of
the page) and /__e2e/landing (a same-host popup target, so
link tests need no network).
"""

import sys
import tempfile
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "plugin"))

from live_artifacts.server.app import make_app  # noqa: E402
from live_artifacts.server.store import Store  # noqa: E402


def build(port: int) -> web.Application:
    store = Store(Path(tempfile.mkdtemp(prefix="hla-e2e-")))
    html = (ROOT / "fixtures" / "form.html").read_text()
    # link_secret off: the page loads from the bare id, as it does on the real tailnet install.
    app = make_app(store, link_secret=False, base_url=f"http://127.0.0.1:{port}")

    async def create(_r: web.Request) -> web.Response:
        a = store.create_artifact("E2E fixture", html, {"db": {}, "user": {}})
        return web.json_response({"id": a["id"], "token": a["token"]})

    async def status(r: web.Request) -> web.Response:
        a = store.get_artifact(r.match_info["aid"])
        return web.json_response({"status": a["status"], "notified_at": a["notified_at"]})

    async def docs(r: web.Request) -> web.Response:
        return web.json_response(store.dump_docs(r.match_info["aid"]))

    async def landing(_r: web.Request) -> web.Response:
        return web.Response(text="<title>landing</title><p>landed</p>", content_type="text/html")

    app.router.add_post("/__e2e/artifact", create)
    app.router.add_get("/__e2e/artifact/{aid}", status)
    app.router.add_get("/__e2e/docs/{aid}", docs)
    app.router.add_get("/__e2e/landing", landing)
    return app


if __name__ == "__main__":
    port = int(sys.argv[1])
    web.run_app(build(port), host="127.0.0.1", port=port, access_log=None, print=None)
