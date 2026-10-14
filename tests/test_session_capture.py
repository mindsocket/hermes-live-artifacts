"""The creating session is captured dynamically from the gateway's per-call context, never configured."""

import contextvars
import json
import sqlite3

import pytest

import hermes_live_artifacts.server.tools as tools
from hermes_live_artifacts.server.store import Store


def use_store(monkeypatch, tmp_path):
    s = Store(tmp_path)
    monkeypatch.setattr(tools, "_store", s)
    return s


def test_create_records_session_from_gateway_context(monkeypatch, tmp_path):
    s = use_store(monkeypatch, tmp_path)
    monkeypatch.setattr(tools, "current_session_key", lambda: "agent:main:telegram:group:9:4")
    r = json.loads(tools.artifact_create({"title": "t", "html": "<p>x</p>"}))
    assert r["ok"] and r["push_on_submit"] is True
    assert s.get_artifact(r["id"])["origin_session"] == "agent:main:telegram:group:9:4"


def test_two_sessions_two_artifacts_each_follow_their_own_creator(monkeypatch, tmp_path):
    s = use_store(monkeypatch, tmp_path)
    ids = {}
    for key in ("sessA", "sessB"):
        monkeypatch.setattr(tools, "current_session_key", lambda k=key: k)
        ids[key] = json.loads(tools.artifact_create({"title": key, "html": "x"}))["id"]
    assert [s.get_artifact(ids[k])["origin_session"] for k in ("sessA", "sessB")] == ["sessA", "sessB"]


def test_no_session_means_no_push_and_says_so(monkeypatch, tmp_path):
    use_store(monkeypatch, tmp_path)
    monkeypatch.setattr(tools, "current_session_key", lambda: "")
    r = json.loads(tools.artifact_create({"title": "t", "html": "x"}))
    assert r["ok"] and r["push_on_submit"] is False


def test_wake_false_opts_out(monkeypatch, tmp_path):
    use_store(monkeypatch, tmp_path)
    monkeypatch.setattr(tools, "current_session_key", lambda: "sess")
    r = json.loads(tools.artifact_create({"title": "t", "html": "x", "wake": False}))
    assert r["push_on_submit"] is False and r["wake"] is False
    r2 = json.loads(tools.artifact_update({"id": r["id"], "wake": True}))
    assert r2["push_on_submit"] is True


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


def test_existing_database_is_migrated(tmp_path):
    """A database created by the first release (with on_submit, without the push columns) must open."""
    db = sqlite3.connect(tmp_path / "artifacts.db")
    db.executescript("""
    CREATE TABLE artifacts (id TEXT PRIMARY KEY, title TEXT NOT NULL, html TEXT NOT NULL,
      capabilities TEXT NOT NULL DEFAULT '{}', mcp_allowlist TEXT NOT NULL DEFAULT '[]',
      on_submit TEXT NOT NULL DEFAULT 'none', pinned INTEGER NOT NULL DEFAULT 0,
      status TEXT NOT NULL DEFAULT 'active', token TEXT NOT NULL, created_at REAL NOT NULL,
      updated_at REAL NOT NULL, submitted_at REAL, last_write_at REAL, archived_at REAL);
    INSERT INTO artifacts (id,title,html,token,created_at,updated_at) VALUES ('old1','Old','x','tok',1,1);
    """)
    db.commit()
    db.close()
    s = Store(tmp_path)
    a = s.get_artifact("old1")
    assert a["origin_session"] is None and a["wake"] is True and a["woken_at"] is None
    new = s.create_artifact("New", "x", origin_session="sess")  # insert works against the migrated table
    assert s.claim_wake(new["id"]) is True and s.claim_wake(new["id"]) is False
    assert s.claim_wake("old1") is False  # no origin session: nothing to push to
