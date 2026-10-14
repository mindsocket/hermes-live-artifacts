"""Agent-facing tools. Handlers take (args, **kw) and return a JSON string."""

from __future__ import annotations

import json
import os
from typing import Any

from . import mcp as mcp_mod
from .store import MIN_UNGUESSABLE_ID, DbError, Store

_store: Store | None = None


def store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


_base_url = "http://127.0.0.1:47318"


_link_secret = True


def configure(base_url: str, link_secret: bool = True) -> None:
    """Set once from plugin settings in register(); every process that loads the plugin calls this."""
    global _base_url, _link_secret
    _base_url = base_url.rstrip("/")
    _link_secret = link_secret


def base_url() -> str:
    return os.environ.get("HERMES_LIVE_ARTIFACTS_BASE_URL", _base_url).rstrip("/")


def link(a: dict) -> str:
    # Legacy short ids are guessable, so their links keep the secret whatever the setting.
    if _link_secret or len(a["id"]) < MIN_UNGUESSABLE_ID:
        return f"{base_url()}/a/{a['id']}?k={a['token']}"
    return f"{base_url()}/a/{a['id']}"


def _ok(**kw: Any) -> str:
    return json.dumps({"ok": True, **kw})


def _err(msg: str) -> str:
    return json.dumps({"ok": False, "error": msg})


def current_session_key() -> str:
    """Gateway routing key of the session whose tool call we are inside, or '' (CLI, cron, tests).
    Read per call from the gateway's context, so each artifact follows whichever chat created it."""
    key = ""
    try:
        from gateway.session_context import get_session_env

        key = get_session_env("HERMES_SESSION_KEY", "")
    except Exception:
        pass
    if not key:
        try:
            from tools.approval_context import get_current_session_key

            key = get_current_session_key("")
        except Exception:
            pass
    return key if key and key != "default" else ""


def _summary(a: dict) -> dict:
    out = {
        k: a[k]
        for k in ("id", "title", "status", "pinned", "wake", "created_at", "updated_at", "submitted_at")
        if k in a
    }
    out["push_on_submit"] = bool(a.get("wake") and a.get("origin_session"))
    return out


def artifact_create(args: dict, **_kw) -> str:
    title, html = args.get("title"), args.get("html")
    if not title or not html:
        return _err("title and html are required")
    caps = args.get("capabilities")
    if caps is None:
        caps = {"db": {}, "user": {}}
    if isinstance(caps, list):  # tolerate ["db","user"]
        caps = {c: {} for c in caps}
    try:
        if isinstance(caps, dict) and "mcp" in caps:
            mcp_mod.validate_manifest(caps["mcp"])
        a = store().create_artifact(
            title,
            html,
            caps,
            None,
            bool(args.get("pinned")),
            args.get("seed"),
            origin_session=current_session_key() or None,
            wake=args.get("wake", True) is not False,
        )
    except (ValueError, DbError) as e:
        return _err(str(e))
    return _ok(**_summary(a), link=link(a))


def artifact_update(args: dict, **_kw) -> str:
    aid = args.get("id")
    caps = args.get("capabilities")
    if isinstance(caps, list):
        caps = {c: {} for c in caps}
    try:
        if isinstance(caps, dict) and "mcp" in caps:
            mcp_mod.validate_manifest(caps["mcp"])
        a = store().update_artifact(
            aid,
            title=args.get("title"),
            html=args.get("html"),
            capabilities=caps,
            pinned=args.get("pinned"),
            wake=args.get("wake"),
        )
    except KeyError:
        return _err(f"no such artifact {aid}")
    except ValueError as e:
        return _err(str(e))
    return _ok(**_summary(a), link=link(a))


def artifact_list(args: dict, **_kw) -> str:
    items = store().list_artifacts(args.get("status"))
    return _ok(artifacts=[_summary(a) for a in items])


def artifact_read(args: dict, **_kw) -> str:
    aid = args.get("id")
    a = store().get_artifact(aid)
    if a is None:
        return _err(f"no such artifact {aid}")
    docs = store().dump_docs(aid)
    return _ok(**_summary(a), docs=docs, doc_count=len(docs))


def artifact_db_write(args: dict, **_kw) -> str:
    aid, path, data = args.get("id"), args.get("path"), args.get("data")
    if store().get_artifact(aid) is None:
        return _err(f"no such artifact {aid}")
    try:
        store().set_doc(aid, path, data)
    except DbError as e:
        return _err(str(e))
    return _ok(path=path)


def artifact_archive(args: dict, **_kw) -> str:
    try:
        store().archive(args.get("id"))
    except KeyError:
        return _err("no such artifact")
    return _ok(archived=args.get("id"))


def artifact_delete(args: dict, **_kw) -> str:
    try:
        store().delete(args.get("id"))
    except KeyError:
        return _err("no such artifact")
    return _ok(deleted=args.get("id"))


def artifact_export(args: dict, **_kw) -> str:
    dump = store().export_all()
    path = args.get("path")
    if path:
        with open(os.path.expanduser(path), "w") as f:
            json.dump(dump, f, indent=2)
        return _ok(path=path, artifacts=len(dump["artifacts"]))
    return _ok(**dump)


def _schema(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required},
    }


_ID = {"type": "string", "description": "Artifact id"}
TOOLS = [
    (
        _schema(
            "artifact_create",
            "Create an interactive HTML artifact (form, review list, dashboard) the user opens by deep link. Input "
            "is persisted to a per-artifact document DB readable via artifact_read. Page code uses "
            "`const db = await claude.use('db')`; call `claude.submit()` to mark it submitted. Returns the deep "
            "link. When created from a chat session, the entered data is pushed back into that same session as a "
            "message on first submit (push_on_submit=true in the result), so do not poll: share the link and end "
            "your turn. Pass wake=false to opt out.",
            {
                "title": {"type": "string"},
                "html": {"type": "string", "description": "Body HTML or full document"},
                "wake": {
                    "type": "boolean",
                    "description": "Push the submitted data back to this session (default true)",
                },
                "capabilities": {
                    "type": "object",
                    "description": "Capability manifest: "
                    "{db:{}, user:{}} by default. To let the page call MCP tools add "
                    "mcp:{servers:[{server:'<configured MCP server>', tools:['<tool>']}]}; the operator must "
                    "also list the server under plugins.entries.hermes-live-artifacts.mcp_allowlist.",
                },
                "pinned": {"type": "boolean"},
                "seed": {"type": "object", "description": "{docPath: body} initial docs"},
            },
            ["title", "html"],
        ),
        artifact_create,
    ),
    (
        _schema(
            "artifact_update",
            "Update an artifact in place (db contents are preserved). Returns the link.",
            {
                "id": _ID,
                "title": {"type": "string"},
                "html": {"type": "string"},
                "capabilities": {"type": "object"},
                "pinned": {"type": "boolean"},
                "wake": {"type": "boolean"},
            },
            ["id"],
        ),
        artifact_update,
    ),
    (
        _schema(
            "artifact_list",
            "List artifacts (no html/token).",
            {"status": {"type": "string", "enum": ["active", "submitted", "archived"]}},
            [],
        ),
        artifact_list,
    ),
    (
        _schema(
            "artifact_read",
            "Read an artifact's status and all db documents (what the user entered).",
            {"id": _ID},
            ["id"],
        ),
        artifact_read,
    ),
    (
        _schema(
            "artifact_db_write",
            "Seed or amend one db document of an artifact (full replace).",
            {
                "id": _ID,
                "path": {"type": "string", "description": "even-segment doc path, e.g. items/a1"},
                "data": {"type": "object"},
            },
            ["id", "path", "data"],
        ),
        artifact_db_write,
    ),
    (
        _schema(
            "artifact_archive",
            "Archive an artifact: hides it and revokes the link; data retained.",
            {"id": _ID},
            ["id"],
        ),
        artifact_archive,
    ),
    (_schema("artifact_delete", "Permanently delete an artifact and its data.", {"id": _ID}, ["id"]), artifact_delete),
    (
        _schema(
            "artifact_export",
            "Dump all artifacts and docs to JSON (to `path` if given).",
            {"path": {"type": "string"}},
            [],
        ),
        artifact_export,
    ),
]
