"""Dashboard backend for the Artifacts tab, mounted at /api/plugins/live-artifacts/.

Runs in the dashboard process, a different process from the gateway that hosts the artifact listener. Both open
the same SQLite file (WAL), so this reads and writes the store directly; the heavy lifting is in
`live_artifacts.server.admin`. Auth is the dashboard's own session auth, applied before any route here.
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

_PLUGIN_DIR = str(Path(__file__).resolve().parent.parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from hermes_cli.config import load_config_readonly  # noqa: E402

try:
    from live_artifacts import read_settings
    from live_artifacts.server import admin, tools
except ImportError:
    import importlib.util

    spec = importlib.util.spec_from_file_location("live_artifacts", str(Path(_PLUGIN_DIR) / "__init__.py"))
    if spec and spec.loader:
        _mod = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("live_artifacts", _mod)
        spec.loader.exec_module(_mod)
        read_settings = _mod.read_settings
    from server import admin, tools

PLUGIN_ID = "live-artifacts"

router = APIRouter()


def _configure() -> None:
    entry = ((load_config_readonly() or {}).get("plugins") or {}).get("entries", {}).get(PLUGIN_ID) or {}
    settings = entry.get("settings") or {}
    cfg = read_settings(lambda key, default: settings.get(key, default))
    tools.configure(base_url=cfg["base_url"], link_secret=cfg["link_secret"])


_configure()


class PinBody(BaseModel):
    pinned: bool


def _missing(aid: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"no such artifact {aid}")


@router.get("/artifacts")
def list_artifacts(status: str | None = None):
    return admin.list_artifacts(tools.store(), status)


@router.get("/seq")
def seq():
    return {"change_seq": tools.store().change_seq()}


@router.get("/artifacts/{aid}")
def artifact_detail(aid: str):
    try:
        return admin.detail(tools.store(), aid)
    except KeyError:
        raise _missing(aid) from None


@router.get("/artifacts/{aid}/export")
def artifact_export(aid: str):
    try:
        return admin.export_one(tools.store(), aid)
    except KeyError:
        raise _missing(aid) from None


@router.post("/artifacts/{aid}/pin")
def artifact_pin(aid: str, body: PinBody):
    try:
        admin.set_pinned(tools.store(), aid, body.pinned)
    except KeyError:
        raise _missing(aid) from None
    return {"ok": True}


@router.post("/artifacts/{aid}/rotate")
def artifact_rotate(aid: str):
    try:
        link = admin.rotate_link(tools.store(), aid)
    except KeyError:
        raise _missing(aid) from None
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    return {"ok": True, "link": link}


@router.post("/artifacts/{aid}/archive")
def artifact_archive(aid: str):
    try:
        tools.store().archive(aid)
    except KeyError:
        raise _missing(aid) from None
    return {"ok": True}


@router.delete("/artifacts/{aid}")
def artifact_delete(aid: str):
    try:
        tools.store().delete(aid)
    except KeyError:
        raise _missing(aid) from None
    return {"ok": True}
