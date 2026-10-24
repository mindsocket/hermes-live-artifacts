import asyncio
import re
import sqlite3

import pytest
from aiohttp.test_utils import TestClient, TestServer

from live_artifacts.server import mcp as mcp_mod
from live_artifacts.server.app import make_app


@pytest.fixture
def art(store):
    return store.create_artifact("form", "<h1>hi</h1><script>/*user*/</script>", {"db": {}, "user": {}})


def run(coro):
    return asyncio.run(coro)


async def with_client(store, fn, on_notify=None, mcp_call=None, link_secret=True, base_url=""):
    async with TestClient(TestServer(make_app(store, on_notify, mcp_call, link_secret, base_url))) as c:
        return await fn(c)


def test_page_needs_the_token_and_embeds_it(store, art):
    async def go(c):
        assert (await c.get(f"/a/{art['id']}")).status == 404
        assert (await c.get(f"/a/{art['id']}/frame")).status == 404
        assert (await c.get(f"/a/{art['id']}/frame?k=wrong")).status == 404
        # the link URL carries the secret; both shell and frame answer it (same ?k= exchange)
        r = await c.get(f"/a/{art['id']}?k={art['token']}")
        assert r.status == 200
        shell = await r.text()
        # the shell holds the artifact in a frame and carries no artifact HTML or API token
        assert f'src="/a/{art["id"]}/frame"' in shell and "<h1>hi</h1>" not in shell
        assert art["token"] not in shell
        assert "Set-Cookie" not in r.headers
        csp = r.headers["Content-Security-Policy"]
        assert "frame-src 'self'" in csp and "frame-ancestors 'self'" in csp
        assert "*.ts.net" not in csp
        f = await c.get(f"/a/{art['id']}/frame?k={art['token']}")
        assert f.status == 200
        body = await f.text()
        assert "window.__ARTIFACT__" in body and "window.claude" in body and "<h1>hi</h1>" in body
        assert art["token"] in body  # the page carries its own API credential
        fcsp = f.headers["Content-Security-Policy"]
        assert "connect-src 'self'" in fcsp and "https://cdnjs.cloudflare.com" in fcsp
        assert "fonts.googleapis.com" in fcsp and "worker-src 'self' blob:" in fcsp
        assert "allow-same-origin" not in fcsp and "allow-popups-to-escape-sandbox" not in fcsp
        assert "camera=()" in f.headers["Permissions-Policy"]

    run(with_client(store, go))


def test_fragment_is_wrapped_in_doctype_reset_and_body(store):
    a = store.create_artifact("frag", "<title>T</title><p>x</p>", {"db": {}})

    async def go(c):
        body = await (await c.get(f"/a/{a['id']}/frame?k={a['token']}")).text()
        assert "viewport-fit=cover" in body and "[hidden]:not([hidden=until-found i])" in body
        assert body.index("<style>") < body.index("window.__ARTIFACT__") < body.index("<body>")

    run(with_client(store, go))


def test_shell_frame_ancestors_derived_from_base_url(store, art):
    async def go(c):
        r = await c.get(f"/a/{art['id']}")
        assert r.status == 200
        csp = r.headers["Content-Security-Policy"]
        assert "https://node.tail1234.ts.net:*" in csp and "*.ts.net" not in csp
        assert " file:" in csp  # packaged Hermes Desktop renderer is a file:// page

    run(with_client(store, go, link_secret=False, base_url="https://node.tail1234.ts.net"))


def test_frame_intercepts_links_and_sandbox_allows_gestured_popups(store, art):
    """External links work by shim interception: the frame's shim hooks clicks and window.open and opens
    the tab itself from the user's gesture. allow-popups (without allow-popups-to-escape-sandbox) makes
    that possible; any popup inherits the sandbox, so it gains nothing the frame lacks."""

    async def go(c):
        frame = await (await c.get(f"/a/{art['id']}/frame")).text()
        assert "openLink" in frame and '"click"' in frame and "preventDefault" in frame
        r = await c.get(f"/a/{art['id']}/frame")
        csp = r.headers["Content-Security-Policy"]
        assert "allow-popups" in csp and "allow-popups-to-escape-sandbox" not in csp
        shell = await (await c.get(f"/a/{art['id']}")).text()
        assert "sandbox=" in shell and "allow-popups-to-escape-sandbox" not in shell

    run(with_client(store, go, link_secret=False))


def test_cross_origin_post_without_the_token_header_is_rejected(store, art):
    async def go(c):
        r = await c.post(
            f"/api/{art['id']}/db/set", json={"path": "a/b", "data": {}}, headers={"Origin": "https://evil.example"}
        )
        assert r.status == 404

    run(with_client(store, go))


def test_one_artifacts_token_cannot_reach_another(store, art):
    """Regression: with cookies, a browser holding both artifacts' cookies let page A read and overwrite B."""
    other = store.create_artifact("other", "<p>o</p>", {"db": {}})
    store.set_doc(other["id"], "secret/s1", {"v": "private"})

    async def go(c):
        for op, body in (("get", {"path": "secret/s1"}), ("set", {"path": "secret/s1", "data": {"v": "x"}})):
            r = await c.post(f"/api/{other['id']}/db/{op}", json=body, headers={"x-artifact": art["token"]})
            assert r.status == 404
        assert (await c.get(f"/api/{other['id']}/events?k={art['token']}")).status == 404
        assert (await c.get(f"/a/{other['id']}?k={art['token']}")).status == 404

    run(with_client(store, go))
    assert store.get_doc(other["id"], "secret/s1")["data"] == {"v": "private"}


def test_unknown_artifact_404(store):
    async def go(c):
        assert (await c.get("/a/nope?k=x")).status == 404

    run(with_client(store, go))


def hdr(art):
    return {"x-artifact": art["token"]}


def test_api_requires_the_token_header(store, art):
    async def go(c):
        url = f"/api/{art['id']}/db/get"
        assert (await c.post(url, json={"path": "a/b"})).status == 404
        assert (await c.post(url, json={"path": "a/b"}, headers={"x-artifact": "1"})).status == 404
        r = await c.post(url, json={"path": "a/b"}, headers=hdr(art))
        assert r.status == 200 and (await r.json())["doc"]["exists"] is False

    run(with_client(store, go))


def test_db_roundtrip_over_http_and_agent_sees_it(store, art):
    async def go(c):
        p = f"/api/{art['id']}/db"
        assert (
            await c.post(f"{p}/set", json={"path": "items/a", "data": {"done": True, "n": 1}}, headers=hdr(art))
        ).status == 200
        assert (await c.post(f"{p}/update", json={"path": "items/a", "data": {"n": 2}}, headers=hdr(art))).status == 200
        r = await (
            await c.post(f"{p}/query", json={"path": "items", "filters": [["done", "==", True]]}, headers=hdr(art))
        ).json()
        assert [d["data"] for d in r["docs"]] == [{"done": True, "n": 2}]
        bad = await c.post(f"{p}/set", json={"path": "items", "data": {}}, headers=hdr(art))
        assert bad.status == 400 and (await bad.json())["error"]["code"] == "invalid_argument"
        acq = await (
            await c.post(f"{p}/acquire", json={"path": "l/x", "holder": "h", "ttlMs": 1000}, headers=hdr(art))
        ).json()
        assert acq["result"]["acquired"] is True

    run(with_client(store, go))
    assert store.get_doc(art["id"], "items/a")["data"] == {"done": True, "n": 2}


def test_save_with_notify_saves_notifies_and_calls_hook_once(store, art):
    calls = []

    async def go(c):
        r = await c.post(
            f"/api/{art['id']}/db/set",
            json={"path": "form/answers", "data": {"status": "submitted"}, "notify": True},
            headers=hdr(art),
        )
        assert r.status == 200
        assert (
            await c.post(f"/api/{art['id']}/db/set", json={"path": "form/x", "data": {}}, headers=hdr(art))
        ).status == 200

    run(with_client(store, go, on_notify=calls.append))
    assert store.get_artifact(art["id"])["notified_at"]
    assert store.get_doc(art["id"], "form/answers")["data"] == {"status": "submitted"}
    assert [a["id"] for a in calls] == [art["id"]]  # the plain save did not notify


def test_standalone_notify_calls_hook_and_sends_no_data(store, art):
    calls = []

    async def go(c):
        r = await c.post(f"/api/{art['id']}/notify", json={}, headers=hdr(art))
        assert r.status == 200 and await r.json() == {}

    run(with_client(store, go, on_notify=calls.append))
    assert store.get_artifact(art["id"])["notified_at"] and store.dump_docs(art["id"]) == {}
    assert [a["id"] for a in calls] == [art["id"]]


def test_notify_flag_must_be_a_boolean_on_a_save(store, art):
    async def go(c):
        bad = await c.post(
            f"/api/{art['id']}/db/set", json={"path": "a/b", "data": {}, "notify": "yes"}, headers=hdr(art)
        )
        assert bad.status == 400 and (await bad.json())["error"]["code"] == "invalid_argument"
        wrong_op = await c.post(f"/api/{art['id']}/db/get", json={"path": "a/b", "notify": True}, headers=hdr(art))
        assert wrong_op.status == 400
        assert (
            await c.post(f"/api/{art['id']}/db/set", json={"path": "a/b", "data": {}}, headers=hdr(art))
        ).status == 200

    run(with_client(store, go))
    assert store.get_artifact(art["id"])["notified_at"] is None  # nothing was written by the rejected calls


def test_notify_on_archived_artifact_is_gone_and_does_not_call_hook(store, art):
    calls = []
    store.archive(art["id"])

    async def go(c):
        assert (await c.post(f"/api/{art['id']}/notify", json={}, headers=hdr(art))).status == 404

    run(with_client(store, go, on_notify=calls.append))
    assert calls == []


def test_revoke_kills_old_link_and_token(store, art):
    async def go(c):
        old = dict(art)
        store.rotate_token(art["id"])
        assert (await c.get(f"/a/{art['id']}?k={old['token']}")).status == 404  # old link dead
        assert (await c.post(f"/api/{art['id']}/db/get", json={"path": "a/b"}, headers=hdr(old))).status == 404

    run(with_client(store, go))


def test_archived_is_404(store, art):
    async def go(c):
        store.archive(art["id"])
        assert (await c.get(f"/a/{art['id']}?k={art['token']}")).status == 404

    run(with_client(store, go))


def test_update_in_place_visible_on_reload(store, art):
    async def go(c):
        store.update_artifact(art["id"], html="<h1>v2</h1>")
        assert "<h1>v2</h1>" in await (await c.get(f"/a/{art['id']}/frame?k={art['token']}")).text()

    run(with_client(store, go))


def test_sse_emits_change_event(store, art):
    async def go(c):
        r = await c.get(f"/api/{art['id']}/events?k={art['token']}")
        assert r.status == 200
        first = await asyncio.wait_for(r.content.readuntil(b"\n\n"), 5)
        assert b"event: change" in first
        store.set_doc(art["id"], "c/d", {"v": 1})
        got = b""
        for _ in range(5):
            got = await asyncio.wait_for(r.content.readuntil(b"\n\n"), 5)
            if b"event: change" in got:
                break
        assert b"event: change" in got
        r.close()

    run(with_client(store, go))


# ---------------------------------------------------------------- sandbox, CORS, clean URL
def test_sse_connections_per_artifact_are_capped(store, art):
    async def go(c):
        streams = [await c.get(f"/api/{art['id']}/events?k={art['token']}") for _ in range(8)]
        assert all(s.status == 200 for s in streams)
        extra = await c.get(f"/api/{art['id']}/events?k={art['token']}")
        assert extra.status == 429
        other = store.create_artifact("o", "<p/>", {"db": {}})
        assert (await c.get(f"/api/{other['id']}/events?k={other['token']}")).status == 200  # cap is per artifact
        for s in streams:
            s.close()

    run(with_client(store, go))


def test_non_object_body_is_400_and_oversize_is_413(store, art):
    async def go(c):
        url = f"/api/{art['id']}/db/set"
        for raw in ("[1,2]", '"x"', "3", "null"):
            r = await c.post(url, data=raw, headers={**hdr(art), "content-type": "application/json"})
            assert r.status == 400, (raw, r.status)
        r = await c.post(
            url,
            data='{"path":"a/b","data":{"x":"' + "y" * 400000 + '"}}',
            headers={**hdr(art), "content-type": "application/json"},
        )
        assert r.status == 413

    run(with_client(store, go))


def test_page_is_sandboxed_with_opaque_origin(store, art):
    async def go(c):
        r = await c.get(f"/a/{art['id']}/frame?k={art['token']}")
        csp = r.headers["Content-Security-Policy"]
        assert "sandbox allow-scripts" in csp and "allow-same-origin" not in csp
        for flag in ("allow-forms", "allow-downloads", "allow-modals", "allow-popups"):
            assert flag in csp
        assert "allow-popups-to-escape-sandbox" not in csp

    run(with_client(store, go))


def test_link_exchange_redirects_to_clean_url_with_path_scoped_cookie(store, art):
    async def go(c):
        r = await c.get(f"/a/{art['id']}?k={art['token']}", allow_redirects=False)
        assert r.status == 302 and r.headers["Location"] == f"/a/{art['id']}"
        cookie = r.headers["Set-Cookie"]
        assert "HttpOnly" in cookie and "SameSite=Lax" in cookie and f"Path=/a/{art['id']}" in cookie
        # the cookie authorises only this page: bare URL works with it, another artifact's does not
        other = store.create_artifact("o", "<p>o</p>", {"db": {}})
        assert (await c.get(f"/a/{art['id']}")).status == 200
        assert (await c.get(f"/a/{other['id']}")).status == 404

    run(with_client(store, go))


def test_bare_url_without_cookie_is_forbidden_and_wrong_token_too(store, art):
    async def go(c):
        assert (await c.get(f"/a/{art['id']}")).status == 404
        assert (await c.get(f"/a/{art['id']}?k=nope", allow_redirects=False)).status == 404

    run(with_client(store, go))


def test_api_answers_cors_preflight_and_errors_carry_cors(store, art):
    async def go(c):
        r = await c.options(
            f"/api/{art['id']}/db/get",
            headers={
                "Origin": "null",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "x-artifact,content-type",
            },
        )
        assert r.status == 204 and r.headers["Access-Control-Allow-Origin"] == "*"
        assert "x-artifact" in r.headers["Access-Control-Allow-Headers"]
        assert "Access-Control-Allow-Credentials" not in r.headers
        bad = await c.post(f"/api/{art['id']}/db/get", json={"path": "a/b"}, headers={"Origin": "null"})
        assert bad.status == 404 and bad.headers["Access-Control-Allow-Origin"] == "*"  # readable by the page
        ok = await c.post(f"/api/{art['id']}/db/get", json={"path": "a/b"}, headers={**hdr(art), "Origin": "null"})
        assert ok.status == 200 and ok.headers["Access-Control-Allow-Origin"] == "*"

    run(with_client(store, go))


# ---------------------------------------------------------------- mcp
def mcp_art(store, servers=None):
    return store.create_artifact(
        "m", "<p>m</p>", {"db": {}, "mcp": {"servers": servers or [{"server": "todo", "tools": ["list"]}]}}
    )


def with_mcp(store, fn, mcp_call):
    return run(with_client(store, fn, mcp_call=mcp_call))


def test_mcp_call_success_returns_payload_and_content(store):
    a = mcp_art(store)
    seen = []

    def call(server, tool, args, timeout=30):
        seen.append((server, tool, args))
        return {"ok": True, "result": '{"n": 2}'}

    async def go(c):
        r = await c.post(
            f"/api/{a['id']}/mcp/call", json={"server": "todo", "tool": "list", "input": {"q": 1}}, headers=hdr(a)
        )
        return r.status, await r.json()

    status, body = with_mcp(store, go, call)
    assert status == 200 and body["result"]["payload"] == {"n": 2}
    assert body["result"]["content"][0]["type"] == "text"
    assert seen == [("todo", "list", {"q": 1})]


def test_mcp_structured_content_wins_for_payload(store):
    a = mcp_art(store)

    def call(s, t, args, timeout=30):
        return {"ok": True, "result": "text", "structuredContent": {"k": 1}}

    async def go(c):
        return await (
            await c.post(f"/api/{a['id']}/mcp/call", json={"server": "todo", "tool": "list"}, headers=hdr(a))
        ).json()

    body = with_mcp(store, go, call)
    assert body["result"]["payload"] == {"k": 1} and body["result"]["structuredContent"] == {"k": 1}


def test_mcp_refuses_undeclared_server_or_tool_without_calling(store):
    a = mcp_art(store)
    calls = []

    def call(*x, **k):
        calls.append(x)
        return {"ok": True, "result": "x"}

    async def go(c):
        out = []
        for body in ({"server": "todo", "tool": "delete_everything"}, {"server": "other", "tool": "list"}):
            r = await c.post(f"/api/{a['id']}/mcp/call", json=body, headers=hdr(a))
            out.append((r.status, (await r.json())["error"]["code"]))
        return out

    assert with_mcp(store, go, call) == [(403, "not_in_manifest")] * 2
    assert calls == []


def test_mcp_artifact_without_mcp_capability_cannot_call(store, art):
    called = []

    async def go(c):
        r = await c.post(f"/api/{art['id']}/mcp/call", json={"server": "todo", "tool": "list"}, headers=hdr(art))
        return r.status, (await r.json())["error"]["code"]

    assert with_mcp(store, go, lambda *a, **k: called.append(1)) == (403, "not_in_manifest") and not called


def test_mcp_operator_allowlist_refusal_maps_to_blocked_by_policy(store):
    a = mcp_art(store)

    def call(*x, **k):
        raise PermissionError(
            "Plugin 'live-artifacts' is not allowed to call MCP server 'todo'. Add it to plugins.entries.x.mcp_allowlist"
        )

    async def go(c):
        r = await c.post(f"/api/{a['id']}/mcp/call", json={"server": "todo", "tool": "list"}, headers=hdr(a))
        return r.status, (await r.json())["error"]

    status, err = with_mcp(store, go, call)
    assert status == 403 and err["code"] == "blocked_by_policy" and "mcp_allowlist" in err["message"]


@pytest.mark.parametrize(
    "msg,code",
    [
        ("MCP server 'todo' is not connected", "server_not_connected"),
        ("MCP server 'todo' is unreachable after 3 consecutive failures", "server_unavailable"),
        ("The user did not approve running write-capable MCP tool", "approval_required"),
        ("boom", "tool_error"),
    ],
)
def test_mcp_error_classification(store, msg, code):
    a = mcp_art(store)

    async def go(c):
        r = await c.post(f"/api/{a['id']}/mcp/call", json={"server": "todo", "tool": "list"}, headers=hdr(a))
        return (await r.json())["error"]["code"]

    assert with_mcp(store, go, lambda *x, **k: {"ok": False, "error": msg}) == code


def test_mcp_without_bridge_is_upstream_error(store):
    a = mcp_art(store)

    async def go(c):
        r = await c.post(f"/api/{a['id']}/mcp/call", json={"server": "todo", "tool": "list"}, headers=hdr(a))
        return (await r.json())["error"]["code"]

    assert with_mcp(store, go, None) == "upstream_error"


def test_shim_config_carries_mcp_manifest_but_no_other_artifacts_token(store):
    a = mcp_art(store)
    other = store.create_artifact("o", "<p>o</p>", {"db": {}})

    async def go(c):
        return await (await c.get(f"/a/{a['id']}/frame?k={a['token']}")).text()

    body = with_mcp(store, go, None)
    assert '"mcp": [{"server": "todo"' in body and other["token"] not in body


def test_manifest_validation():
    mcp_mod.validate_manifest({"servers": [{"server": "a", "tools": ["x"]}]})
    for bad in (
        None,
        {},
        {"servers": []},
        {"servers": [{"server": "a", "tools": []}]},
        {"servers": [{"server": "a"}]},
        {"servers": [{"server": "", "tools": ["x"]}]},
        {"servers": [{"server": "a", "tools": ["bad name!"]}]},
        {"servers": [{"server": "a", "tools": ["x"]}, {"server": "a", "tools": ["y"]}]},
    ):
        with pytest.raises(ValueError):
            mcp_mod.validate_manifest(bad)


# ---------------------------------------------------------------- link_secret off: the long id is the gate
def with_mode(store, fn, link_secret):
    return run(with_client(store, fn, link_secret=link_secret))


def test_new_ids_are_22_url_safe_chars_and_unique(store):
    ids = {store.create_artifact("x", "<p/>", {"db": {}})["id"] for _ in range(50)}
    assert len(ids) == 50 and all(re.fullmatch(r"[A-Za-z0-9_-]{22}", i) for i in ids)


def test_link_secret_off_serves_clean_url_without_k_or_cookie(store, art):
    async def go(c):
        r = await c.get(f"/a/{art['id']}", allow_redirects=False)
        assert r.status == 200 and "Set-Cookie" not in r.headers
        assert "frame-src 'self'" in r.headers["Content-Security-Policy"]  # the shell
        f = await c.get(f"/a/{art['id']}/frame")
        assert f.status == 200 and "sandbox allow-scripts" in f.headers["Content-Security-Policy"]
        assert (await c.get("/a/" + "A" * 22)).status == 404  # unknown long id
        assert (await c.get(f"/a/{'A' * 22}/frame")).status == 404

    with_mode(store, go, False)


def test_link_secret_off_still_strips_a_stale_k(store, art):
    async def go(c):
        r = await c.get(f"/a/{art['id']}?k=whatever", allow_redirects=False)
        assert r.status == 302 and r.headers["Location"] == f"/a/{art['id']}"

    with_mode(store, go, False)


def test_link_secret_off_keeps_the_api_token_so_artifacts_stay_isolated(store, art):
    other = store.create_artifact("o", "<p/>", {"db": {}})

    async def go(c):
        assert (await c.post(f"/api/{art['id']}/db/get", json={"path": "a/b"})).status == 404
        assert (await c.post(f"/api/{other['id']}/db/get", json={"path": "a/b"}, headers=hdr(art))).status == 404
        assert (await c.post(f"/api/{art['id']}/db/get", json={"path": "a/b"}, headers=hdr(art))).status == 200

    with_mode(store, go, False)


def test_legacy_short_id_keeps_the_secret_even_when_off(store):
    old = store.create_artifact("old", "<p/>", {"db": {}})
    c = sqlite3.connect(store.path)
    c.execute("UPDATE artifacts SET id='old123456789' WHERE id=?", (old["id"],))
    c.commit()
    c.close()
    old = store.get_artifact("old123456789")

    async def go(cl):
        assert (await cl.get("/a/old123456789")).status == 404
        r = await cl.get(f"/a/old123456789?k={old['token']}", allow_redirects=False)
        assert r.status == 302 and "Set-Cookie" in r.headers

    with_mode(store, go, False)


def test_non_ascii_token_is_refused_like_any_wrong_token(store, art):
    """hmac.compare_digest raises on a non-ASCII str; that must not become a 500 that tells live ids from dead ones."""

    async def go(c):
        assert (await c.get(f"/a/{art['id']}?k=%C3%A9", allow_redirects=False)).status == 404
        assert (
            await c.get(f"/a/{art['id']}", headers={"Cookie": f"ha_{art['id']}=é"}, allow_redirects=False)
        ).status == 404
        r = await c.post(f"/api/{art['id']}/db/get", json={"path": "a/b"}, headers={"x-artifact": "é"})
        assert r.status == 404
        assert (await c.get(f"/api/{art['id']}/events?k=%C3%A9")).status == 404

    run(with_client(store, go))


# ---------------------------------------------------------------- index page
def with_index(store, fn, link_secret=True):
    async def go():
        async with TestClient(TestServer(make_app(store, link_secret=link_secret, index=True))) as c:
            return await fn(c)

    return run(go())


def test_index_route_absent_unless_enabled(store, art):
    async def go(c):
        assert (await c.get("/index")).status == 404
        assert (await c.get(f"/index?k={store.index_token()}")).status == 404

    run(with_client(store, go))


def test_index_needs_its_token_in_every_link_secret_mode(store, art):
    async def go(c):
        assert (await c.get("/index")).status == 404
        assert (await c.get("/index?k=wrong")).status == 404

    for mode in (True, False):
        with_index(store, go, link_secret=mode)


def test_index_token_exchanges_for_a_scoped_cookie_then_lists(store, art):
    async def go(c):
        r = await c.get(f"/index?k={store.index_token()}", allow_redirects=False)
        assert r.status == 302 and r.headers["Location"] == "/index"
        cookie = r.headers["Set-Cookie"]
        assert "HttpOnly" in cookie and "Path=/index" in cookie and "SameSite=Lax" in cookie
        page = await c.get("/index")  # the client keeps the cookie
        assert page.status == 200
        body = await page.text()
        assert "form" in body and f"/a/{art['id']}?k={art['token']}" in body
        h = page.headers
        assert "default-src 'none'" in h["Content-Security-Policy"]
        assert h["Referrer-Policy"] == "no-referrer" and h["Cache-Control"] == "no-store"

    with_index(store, go)


def test_index_links_drop_k_when_the_artifact_needs_none(store, art):
    async def go(c):
        await c.get(f"/index?k={store.index_token()}")
        body = await (await c.get("/index")).text()
        assert f'href="/a/{art["id"]}"' in body and art["token"] not in body

    with_index(store, go, link_secret=False)


def test_index_lists_active_only_pinned_first_and_escapes_titles(store):
    store.create_artifact("old", "<p>x</p>", {})
    store.create_artifact("<b>new</b>", "<p>x</p>", {})
    pinned = store.create_artifact("pinned", "<p>x</p>", {})
    gone = store.create_artifact("gone", "<p>x</p>", {})
    store.update_artifact(pinned["id"], pinned=True)
    store.archive(gone["id"])

    async def go(c):
        await c.get(f"/index?k={store.index_token()}")
        body = await (await c.get("/index")).text()
        assert "gone" not in body and "<b>new</b>" not in body and "&lt;b&gt;new&lt;/b&gt;" in body
        order = [body.index(t) for t in (">★ pinned<", "&lt;b&gt;new", ">old<")]
        assert order == sorted(order)

    with_index(store, go)


def test_rotating_the_index_token_revokes_the_cookie(store, art):
    async def go(c):
        await c.get(f"/index?k={store.index_token()}")
        assert (await c.get("/index")).status == 200
        fresh = store.rotate_index_token()
        assert (await c.get("/index")).status == 404
        assert (await c.get(f"/index?k={fresh}")).status == 200

    with_index(store, go)
