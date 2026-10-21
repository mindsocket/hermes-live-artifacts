"""The creating session is captured dynamically from the gateway's per-call context, never configured."""

import contextvars
import json

import pytest

import live_artifacts.server.tools as tools


def test_create_records_session_from_gateway_context(monkeypatch, tools_store):
    s = tools_store
    monkeypatch.setattr(tools, "current_session_key", lambda: "agent:main:telegram:group:9:4")
    r = json.loads(tools.live_artifact_create({"title": "t", "html": "<p>x</p>"}))
    assert r["ok"] and r["push_on_notify"] is True
    assert s.get_artifact(r["id"])["origin_session"] == "agent:main:telegram:group:9:4"


def test_two_sessions_two_artifacts_each_follow_their_own_creator(monkeypatch, tools_store):
    s = tools_store
    ids = {}
    for key in ("sessA", "sessB"):
        monkeypatch.setattr(tools, "current_session_key", lambda k=key: k)
        ids[key] = json.loads(tools.live_artifact_create({"title": key, "html": "x"}))["id"]
    assert [s.get_artifact(ids[k])["origin_session"] for k in ("sessA", "sessB")] == ["sessA", "sessB"]


def test_no_session_means_no_push_and_says_so(monkeypatch, tools_store):
    monkeypatch.setattr(tools, "current_session_key", lambda: "")
    r = json.loads(tools.live_artifact_create({"title": "t", "html": "x"}))
    assert r["ok"] and r["push_on_notify"] is False


def test_wake_false_opts_out(monkeypatch, tools_store):
    monkeypatch.setattr(tools, "current_session_key", lambda: "sess")
    r = json.loads(tools.live_artifact_create({"title": "t", "html": "x", "wake": False}))
    assert r["push_on_notify"] is False and r["wake"] is False
    r2 = json.loads(tools.live_artifact_update({"id": r["id"], "wake": True}))
    assert r2["push_on_notify"] is True


def test_current_session_key_reads_gateway_contextvar(monkeypatch):
    sc = pytest.importorskip("gateway.session_context")
    ctx = contextvars.copy_context()

    def inside():
        toks = sc.set_session_vars(session_key="agent:main:telegram:group:1:2")
        try:
            return tools.current_session_key()
        finally:
            sc.clear_session_vars(toks)

    assert ctx.run(inside) == "agent:main:telegram:group:1:2"


def test_current_session_key_empty_outside_a_session(monkeypatch):
    sc = pytest.importorskip("gateway.session_context")
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    ctx = contextvars.copy_context()

    def inside():
        sc.reset_session_vars()
        return tools.current_session_key()

    assert ctx.run(inside) == ""
