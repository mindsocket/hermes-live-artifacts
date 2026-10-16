"""Dashboard tab backend: admin.py (pure) and dashboard/plugin_api.py (FastAPI router over it)."""

import importlib.util
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_live_artifacts.server import admin, tools

API_FILE = Path(__file__).resolve().parents[2] / "plugin" / "dashboard" / "plugin_api.py"


@pytest.fixture
def art(tools_store):
    tools.configure("https://h.ts.net:47318", link_secret=True)
    a = tools_store.create_artifact("Form", "<p>x</p>", {"db": {}}, origin_session="sess-1")
    tools_store.set_doc(a["id"], "items/a1", {"n": 1})
    return a


@pytest.fixture
def client(tools_store, monkeypatch):
    """plugin_api imported from its file, with Hermes' config loader stubbed (hermes_cli is not a test dep)."""
    cfg = {"plugins": {"entries": {"hermes-live-artifacts": {"settings": {"base_url": "https://h.ts.net:47318"}}}}}
    pkg = types.ModuleType("hermes_cli")
    mod = types.ModuleType("hermes_cli.config")
    mod.load_config_readonly = lambda: cfg
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", mod)
    spec = importlib.util.spec_from_file_location("hla_plugin_api", API_FILE)
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def test_list_has_doc_count_session_and_never_a_token(art, tools_store):
    out = admin.list_artifacts(tools_store)
    [row] = out["artifacts"]
    assert row["id"] == art["id"] and row["doc_count"] == 1 and row["origin_session"] == "sess-1"
    assert "token" not in row and "html" not in row
    assert out["rotation_available"] is True


def test_list_says_when_rotation_is_offered(art, tools_store):
    tools.configure("https://h.ts.net:47318", link_secret=False)
    assert admin.list_artifacts(tools_store)["rotation_available"] is False


def test_list_filters_by_status(art, tools_store):
    tools_store.archive(art["id"])
    assert admin.list_artifacts(tools_store, "active")["artifacts"] == []
    assert len(admin.list_artifacts(tools_store, "archived")["artifacts"]) == 1


def test_detail_carries_docs_and_link(art, tools_store):
    d = admin.detail(tools_store, art["id"])
    assert d["docs"] == {"items/a1": {"n": 1}}
    assert d["link"] == f"https://h.ts.net:47318/a/{art['id']}?k={art['token']}"


def test_detail_offers_rotation_only_when_links_carry_a_secret(art, tools_store):
    assert admin.detail(tools_store, art["id"])["rotation_available"] is True
    tools.configure("https://h.ts.net:47318", link_secret=False)
    assert admin.detail(tools_store, art["id"])["rotation_available"] is False


def test_detail_has_no_link_once_archived(art, tools_store):
    tools_store.archive(art["id"])
    assert admin.detail(tools_store, art["id"])["link"] is None


def test_detail_missing_raises_keyerror(tools_store):
    with pytest.raises(KeyError):
        admin.detail(tools_store, "nope")


def test_export_one_includes_docs_and_html_but_no_token(art, tools_store):
    out = admin.export_one(tools_store, art["id"])
    assert out["docs"] == {"items/a1": {"n": 1}} and out["html"] == "<p>x</p>" and "token" not in out


def test_http_list_and_seq(art, client):
    assert client.get("/artifacts").json()["artifacts"][0]["id"] == art["id"]
    assert isinstance(client.get("/seq").json()["change_seq"], int)


def test_http_pin_and_unpin(art, client, tools_store):
    assert client.post(f"/artifacts/{art['id']}/pin", json={"pinned": True}).status_code == 200
    assert tools_store.get_artifact(art["id"])["pinned"] is True
    client.post(f"/artifacts/{art['id']}/pin", json={"pinned": False})
    assert tools_store.get_artifact(art["id"])["pinned"] is False


def test_http_rotate_revokes_old_token_and_returns_new_link(art, client, tools_store):
    out = client.post(f"/artifacts/{art['id']}/rotate").json()
    new = tools_store.get_artifact(art["id"])["token"]
    assert new != art["token"] and out["link"].endswith(f"?k={new}")


def test_http_rotate_refused_in_never_mode(art, client, tools_store):
    tools.configure("https://h.ts.net:47318", link_secret=False)
    r = client.post(f"/artifacts/{art['id']}/rotate")
    assert r.status_code == 409 and "link_secret=never" in r.json()["detail"]
    assert tools_store.get_artifact(art["id"])["token"] == art["token"]


def test_http_archive_then_delete(art, client, tools_store):
    assert client.post(f"/artifacts/{art['id']}/archive").status_code == 200
    assert tools_store.get_artifact(art["id"])["status"] == "archived"
    assert client.delete(f"/artifacts/{art['id']}").status_code == 200
    assert tools_store.get_artifact(art["id"]) is None


def test_http_unknown_id_is_404_everywhere(client):
    for call in (
        lambda: client.get("/artifacts/nope"),
        lambda: client.get("/artifacts/nope/export"),
        lambda: client.post("/artifacts/nope/pin", json={"pinned": True}),
        lambda: client.post("/artifacts/nope/rotate"),
        lambda: client.post("/artifacts/nope/archive"),
        lambda: client.delete("/artifacts/nope"),
    ):
        assert call().status_code == 404
