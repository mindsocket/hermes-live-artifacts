import asyncio
import json
import os
import socket
import types

import aiohttp
import pytest

import hermes_live_artifacts as plug
from hermes_live_artifacts.server import tools as t
from hermes_live_artifacts.server.app import Listener
from hermes_live_artifacts.server.store import Store


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------- settings
def test_settings_defaults():
    cfg = plug.read_settings(lambda k, d: d, env={})
    assert cfg["listen_host"] == "127.0.0.1" and cfg["listen_port"] == 47318
    assert cfg["listen_process"] == "gateway"
    assert cfg["base_url"] == "http://127.0.0.1:47318"


def test_settings_plugin_config_and_env_precedence():
    conf = {"listen_host": "tailscale", "listen_port": "50000", "notify_target": "discord:#ops"}
    cfg = plug.read_settings(lambda k, d: conf.get(k, d), env={})
    assert cfg["listen_port"] == 50000 and cfg["base_url"] == "http://TAILSCALE_IP:50000"
    cfg = plug.read_settings(lambda k, d: conf.get(k, d), env={"HERMES_LIVE_ARTIFACTS_LISTEN_PORT": "1234"})
    assert cfg["listen_port"] == 1234


def test_settings_explicit_base_url():
    conf = {"base_url": "https://h.ts.net:47318"}
    cfg = plug.read_settings(lambda k, d: conf.get(k, d), env={})
    assert cfg["base_url"] == "https://h.ts.net:47318"


# ---------------------------------------------------------------- which process listens
def test_should_listen_modes():
    # 'gateway' and 'any' both start a task; the gate (pid file) decides who binds. 'none' never does.
    assert plug.should_listen("gateway") and plug.should_listen("any") and not plug.should_listen("none")


def test_in_gateway_ignores_the_env_marker_entirely(monkeypatch, tmp_path):
    """Identity comes from gateway.pid alone. An inherited marker must not open the gate (children of the
    gateway carry it), and a missing marker must not close it (it may be unset at plugin-load time)."""
    monkeypatch.setattr(plug, "hermes_home", lambda: tmp_path)
    (tmp_path / "gateway.pid").write_text(json.dumps({"pid": os.getpid()}))
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
    assert plug.in_gateway() is True  # no marker needed
    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    (tmp_path / "gateway.pid").write_text(json.dumps({"pid": os.getpid() + 7}))
    assert plug.in_gateway() is False  # marker present (inherited) but pid file names another process


def test_in_gateway_false_when_pid_file_missing_or_corrupt(monkeypatch, tmp_path):
    """No pid file (gateway between exit and restart) or garbage in it must never open the gate: that is
    how a CLI process would wrongly bind the port."""
    monkeypatch.setattr(plug, "hermes_home", lambda: tmp_path)
    assert plug.in_gateway() is False
    (tmp_path / "gateway.pid").write_text("not json")
    assert plug.in_gateway() is False
    (tmp_path / "gateway.pid").write_text(json.dumps({"kind": "hermes-gateway"}))
    assert plug.in_gateway() is False


# ---------------------------------------------------------------- host resolution
def test_resolve_host_passthrough_and_tailscale(monkeypatch):
    assert plug.resolve_host("127.0.0.1") == "127.0.0.1"
    monkeypatch.setattr(
        plug.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="100.1.2.3\nfd7a::1\n")
    )
    assert plug.resolve_host("tailscale") == "100.1.2.3"
    monkeypatch.setattr(plug.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=1, stdout=""))
    with pytest.raises(OSError):
        plug.resolve_host("tailscale")


# ---------------------------------------------------------------- submit: event + push to the creating session
def make(store, **kw):
    a = store.create_artifact("Trip review", "<p>x</p>", {"db": {}}, **kw)
    store.set_doc(a["id"], "form/state", {"pick": "b", "note": "hello"})
    return a


def test_push_is_a_notice_without_page_chosen_strings(store):
    """The push carries no title and no document data, so nothing from the web page enters the session
    as an injected message; the agent reads it with artifact_read instead."""
    a = make(store, origin_session="agent:main:telegram:group:1:2")
    sent, events = [], []
    plug.make_waker(store, lambda c, **k: sent.append((c, k)) or True, lambda e, p: events.append((e, p)))(a)
    assert events == [("submitted", {"id": a["id"], "title": "Trip review"})]
    ((content, kw),) = sent
    assert kw == {"session_key": "agent:main:telegram:group:1:2"}
    assert a["id"] in content and "1 document" in content and "artifact_read" in content
    assert "Trip review" not in content and '"pick"' not in content and "hello" not in content
    assert a["token"] not in content and "Roger" not in content


def test_push_fires_once_per_round_then_again_after_html_update(store):
    a = make(store, origin_session="s1")
    sent = []
    hook = plug.make_waker(store, lambda c, **k: sent.append(c) or True)
    hook(a)
    hook(a)
    hook(a)  # double tap / retries
    assert len(sent) == 1
    store.update_artifact(a["id"], html="<p>round two</p>")
    hook(a)
    assert len(sent) == 2


def test_no_push_without_origin_session_or_when_wake_off(store):
    sent = []
    hook = plug.make_waker(store, lambda c, **k: sent.append(c) or True)
    hook(make(store))  # created from CLI/cron: no session
    hook(make(store, origin_session="s1", wake=False))  # explicit opt-out
    assert sent == []


def test_failed_delivery_releases_claim_so_retry_can_succeed(store):
    a = make(store, origin_session="s1")
    ok = {"v": False}
    sent = []
    hook = plug.make_waker(store, lambda c, **k: sent.append(c) or ok["v"])
    hook(a)
    ok["v"] = True
    hook(a)
    assert len(sent) == 2


def test_inject_exception_is_contained_and_releases_claim(store):
    a = make(store, origin_session="s1")

    def boom(*_a, **_k):
        raise RuntimeError("gateway gone")

    plug.make_waker(store, boom)(a)  # must not raise
    assert store.claim_wake(a["id"]) is True


def test_no_inject_available_still_emits_event(store):
    a = make(store, origin_session="s1")
    events = []
    plug.make_waker(store, None, lambda e, p: events.append(e))(a)
    assert events == ["submitted"]


def test_emit_failure_does_not_block_push(store):
    a = make(store, origin_session="s1")
    sent = []

    def bad_emit(*_a, **_k):
        raise RuntimeError("no subscribers")

    plug.make_waker(store, lambda c, **k: sent.append(c) or True, bad_emit)(a)
    assert len(sent) == 1


def test_large_payload_does_not_reach_the_push(store):
    """Old behaviour truncated big payloads; the notice carries no data at all, whatever the size."""
    a = make(store, origin_session="s1")
    store.set_doc(a["id"], "big/x", {"blob": "y" * 50000})
    sent = []
    plug.make_waker(store, lambda c, **k: sent.append(c) or True)(a)
    assert "artifact_read" in sent[0] and "y" * 100 not in sent[0] and len(sent[0]) < 500


def test_register_wires_inject_message(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_LIVE_ARTIFACTS_DATA", str(tmp_path))
    seen = {}
    real = plug.make_waker
    monkeypatch.setattr(
        plug, "make_waker", lambda s, inject, emit=None: seen.update(inject=inject) or real(s, inject, emit)
    )
    ctx = FakeCtx({})
    ctx.inject_message = lambda *a, **k: True

    async def go():
        plug.register(ctx)

    asyncio.run(go())
    assert seen["inject"] is ctx.inject_message


# ---------------------------------------------------------------- listener bind/retry
def test_listener_retries_until_host_resolves(tmp_path):
    port = free_port()
    attempts = []

    def resolver(h):
        attempts.append(h)
        if len(attempts) < 3:
            raise OSError("tailscale not up")
        return "127.0.0.1"

    async def go():
        lst = Listener(Store(tmp_path), "tailscale", port, resolve_host=resolver, retry_s=0.01)
        task = asyncio.create_task(lst.run_forever())
        for _ in range(100):
            await asyncio.sleep(0.05)
            if lst.bound:
                break
        assert lst.bound == f"127.0.0.1:{port}" and len(attempts) == 3
        async with aiohttp.ClientSession() as s:
            assert (await (await s.get(f"http://127.0.0.1:{port}/healthz")).json())["ok"] is True
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())


def test_listener_retries_when_port_taken_then_wins(tmp_path):
    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    port = blocker.getsockname()[1]

    async def go():
        lst = Listener(Store(tmp_path), "127.0.0.1", port, retry_s=0.05)
        task = asyncio.create_task(lst.run_forever())
        await asyncio.sleep(0.3)
        assert lst.bound is None  # still retrying, did not crash
        blocker.close()
        for _ in range(100):
            await asyncio.sleep(0.05)
            if lst.bound:
                break
        assert lst.bound
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())


# ---------------------------------------------------------------- register()
class FakeCtx:
    def __init__(self, conf):
        self.conf, self.tools, self.skills, self.spawned = conf, [], [], []
        self.skill_meta = {}

    def get_config(self, k, d=None):
        return self.conf.get(k, d)

    def register_tool(self, **kw):
        self.tools.append(kw["name"])

    def register_skill(self, name, path, description="", frontmatter=None):
        self.skills.append(name)
        self.skill_meta[name] = (description, frontmatter or {})

    def spawn_task(self, coro, name=None):
        self.spawned.append(name)
        coro.close()

    def on_unload(self, cb):
        pass


def test_register_tools_only_when_not_listening(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_LIVE_ARTIFACTS_DATA", str(tmp_path))
    ctx = FakeCtx({"listen_process": "none"})
    plug.register(ctx)
    assert len(ctx.tools) == 8 and "capabilities" in ctx.skills and ctx.spawned == []


def test_register_all_skills_with_description(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_LIVE_ARTIFACTS_DATA", str(tmp_path))
    ctx = FakeCtx({"listen_process": "none"})
    plug.register(ctx)
    assert sorted(ctx.skills) == ["capabilities", "design"]
    for name in ctx.skills:
        desc, fm = ctx.skill_meta[name]
        assert desc.startswith("Use ") and fm["name"] == name


def test_register_listens_in_gateway_on_running_loop(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_LIVE_ARTIFACTS_DATA", str(tmp_path))
    ctx = FakeCtx({})

    async def go():
        plug.register(ctx)

    asyncio.run(go())
    assert ctx.spawned == ["hermes-live-artifacts-listener"]


def test_register_none_never_listens(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_LIVE_ARTIFACTS_DATA", str(tmp_path))
    ctx = FakeCtx({"listen_process": "none"})
    plug.register(ctx)
    assert ctx.spawned == []


def test_listener_waits_for_gate_then_binds(tmp_path):
    port = free_port()
    state = {"ok": False}

    async def go():
        lst = Listener(Store(tmp_path), "127.0.0.1", port, gate=lambda: state["ok"], gate_timeout_s=5)
        task = asyncio.create_task(lst.run_forever())
        await asyncio.sleep(0.6)
        assert lst.bound is None  # gate closed: must not have bound
        state["ok"] = True  # gateway.pid rewritten
        for _ in range(100):
            await asyncio.sleep(0.05)
            if lst.bound:
                break
        assert lst.bound == f"127.0.0.1:{port}"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())


def test_listener_gives_up_when_gate_never_opens(tmp_path):
    async def go():
        lst = Listener(Store(tmp_path), "127.0.0.1", free_port(), gate=lambda: False, gate_timeout_s=0.5)
        await asyncio.wait_for(lst.run_forever(), 5)  # returns cleanly, does not hang
        assert lst.bound is None

    asyncio.run(go())


def test_artifact_create_rejects_a_bad_mcp_manifest_and_accepts_a_good_one(tools_store):
    bad = json.loads(
        t.artifact_create(
            {"title": "x", "html": "<p/>", "capabilities": {"db": {}, "mcp": {"servers": [{"server": "a"}]}}}
        )
    )
    assert bad["ok"] is False and "tools" in bad["error"]
    good = json.loads(
        t.artifact_create(
            {
                "title": "x",
                "html": "<p/>",
                "capabilities": {"db": {}, "mcp": {"servers": [{"server": "a", "tools": ["t"]}]}},
            }
        )
    )
    assert good["ok"] is True
    upd = json.loads(t.artifact_update({"id": good["id"], "capabilities": {"mcp": {"servers": []}}}))
    assert upd["ok"] is False


# ---------------------------------------------------------------- link_secret setting
@pytest.mark.parametrize(
    "value,host,base,expected",
    [
        ("auto", "127.0.0.1", "", False),  # loopback only
        ("auto", "127.0.0.1", "http://127.0.0.1:47318", False),
        ("auto", "127.0.0.1", "https://m.tail1.ts.net:47318", True),  # loopback bind fronted by tailscale serve
        ("auto", "tailscale", "", True),
        ("auto", "0.0.0.0", "", True),
        ("on", "127.0.0.1", "", True),
        ("off", "0.0.0.0", "https://x.ts.net", False),
        ("always", "127.0.0.1", "", True),
        ("never", "0.0.0.0", "https://x.ts.net", False),
        (True, "127.0.0.1", "", True),  # YAML / `hermes config set` turn bare on/off into booleans
        (False, "0.0.0.0", "https://x.ts.net", False),
        ("false", "tailscale", "", False),
    ],
)
def test_link_secret_resolution(value, host, base, expected):
    cfg = plug.read_settings(
        lambda k, d: {"link_secret": value, "listen_host": host, "base_url": base}.get(k, d), env={}
    )
    assert cfg["link_secret"] is expected


def test_invalid_settings_raise():
    with pytest.raises(ValueError, match="link_secret"):
        plug.read_settings(lambda k, d: {"link_secret": "bogus"}.get(k, d), env={})
    with pytest.raises(ValueError, match="listen_process"):
        plug.read_settings(lambda k, d: {"listen_process": "bogus"}.get(k, d), env={})


def test_links_follow_the_setting_and_legacy_ids_keep_the_secret(tools_store):
    t.configure("https://h.ts.net", link_secret=False)
    a = json.loads(t.artifact_create({"title": "x", "html": "<p/>"}))
    assert a["link"] == f"https://h.ts.net/a/{a['id']}" and "?k=" not in a["link"]
    t.configure("https://h.ts.net", link_secret=True)
    assert json.loads(t.artifact_list({}))["artifacts"] and "?k=" in t.link(t.store().get_artifact(a["id"]))
    t.configure("https://h.ts.net", link_secret=False)
    legacy = {"id": "old123456789", "token": "tok"}
    assert t.link(legacy).endswith("?k=tok")
