"""Agent-facing tools. Handlers take (args, **kw) and return a JSON string."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from . import mcp as mcp_mod
from .store import DbError, Store

_store: Store | None = None


def store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


_base_url = "http://127.0.0.1:47318"
_link_secret = True
_index = False


def configure(base_url: str, link_secret: bool = True, index: bool = False) -> None:
    """Set once from plugin settings in register(); every process that loads the plugin calls this."""
    global _base_url, _link_secret, _index
    _base_url = base_url.rstrip("/")
    _link_secret = link_secret
    _index = index


def index_enabled() -> bool:
    return _index


def base_url() -> str:
    return _base_url.rstrip("/")


def link_secret_enabled() -> bool:
    return _link_secret


def link(a: dict) -> str:
    if _link_secret:
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
    except ImportError:
        pass
    if not key:
        try:
            from tools.approval_context import get_current_session_key

            key = get_current_session_key("")
        except ImportError:
            pass
    return key if key and key != "default" else ""


def _summary(a: dict) -> dict:
    out = {
        k: a[k]
        for k in ("id", "title", "status", "pinned", "wake", "created_at", "updated_at", "notified_at")
        if k in a
    }
    out["push_on_notify"] = bool(a.get("wake") and a.get("origin_session"))
    return out


def _capabilities(caps: Any, *, default: dict | None) -> dict | None:
    """Accept a capability map or a bare list of names, and validate any `mcp` manifest."""
    if caps is None:
        return default
    if isinstance(caps, list):
        caps = {c: {} for c in caps}
    if "mcp" in caps:
        mcp_mod.validate_manifest(caps["mcp"])
    return caps


def live_artifact_create(args: dict, **_kw) -> str:
    title, html = args.get("title"), args.get("html")
    if not title or not html:
        return _err("title and html are required")
    try:
        caps = _capabilities(args.get("capabilities"), default={"db": {}, "user": {}})
        a = store().create_artifact(
            title,
            html,
            caps,
            pinned=bool(args.get("pinned")),
            seed=args.get("seed"),
            origin_session=current_session_key() or None,
            wake=args.get("wake", True) is not False,
        )
    except (ValueError, DbError) as e:
        return _err(str(e))
    return _ok(**_summary(a), link=link(a))


def live_artifact_update(args: dict, **_kw) -> str:
    aid = args.get("id")
    try:
        caps = _capabilities(args.get("capabilities"), default=None)
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


def live_artifact_list(args: dict, **_kw) -> str:
    items = store().list_artifacts(args.get("status"))
    return _ok(artifacts=[_summary(a) for a in items])


def live_artifact_read(args: dict, **_kw) -> str:
    aid = args.get("id")
    st = store()
    a = st.get_artifact(aid)
    if a is None:
        return _err(f"no such artifact {aid}")
    docs = st.dump_docs(aid)
    return _ok(**_summary(a), docs=docs, doc_count=len(docs))


def live_artifact_db_write(args: dict, **_kw) -> str:
    aid, path, data = args.get("id"), args.get("path"), args.get("data")
    try:
        store().set_doc(aid, path, data)
    except DbError as e:
        return _err(str(e))
    return _ok(path=path)


def live_artifact_archive(args: dict, **_kw) -> str:
    try:
        store().archive(args.get("id"))
    except KeyError:
        return _err("no such artifact")
    return _ok(archived=args.get("id"))


def live_artifact_delete(args: dict, **_kw) -> str:
    try:
        store().delete(args.get("id"))
    except KeyError:
        return _err("no such artifact")
    return _ok(deleted=args.get("id"))


def _write_json_atomic(target: Path, dump: dict) -> None:
    """Write via a 0600 temp sibling and os.replace, so a failure never leaves a truncated target."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(dump, f, indent=2)
        os.replace(tmp, target)
    except BaseException:
        os.unlink(tmp)
        raise


def live_artifact_export(args: dict, **_kw) -> str:
    """Write every artifact and its docs to JSON. Without `path` the file lands in the plugin's exports directory;
    a model-supplied `path` must pass Hermes write policy (the export holds artifact tokens and user input)."""
    dump = store().export_all()
    path = args.get("path")
    if not path:
        target = store().dir / "exports" / f"export-{time.strftime('%Y%m%dT%H%M%S')}.json"
    else:
        try:
            from agent.file_safety import get_write_denied_error, is_write_approval_required
        except ImportError:
            return _err("custom export paths need Hermes write policy, which is unavailable here; omit `path`")
        target = Path(path).expanduser().resolve()
        denied = get_write_denied_error(str(target))
        if denied:
            return _err(denied)
        if is_write_approval_required(str(target)):
            return _err(f"Write denied: '{target}' needs interactive approval; omit `path` or choose another location")
    _write_json_atomic(target, dump)
    return _ok(path=str(target), artifacts=len(dump["artifacts"]))


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
            "live_artifact_create",
            "Create an interactive HTML artifact (form, review list, dashboard) the user opens by deep link. Before "
            "writing the html, load the `live-artifacts:capabilities` skill (the page-side db API) and, for layout and "
            "theming, the `live-artifacts:design` skill. Input is persisted to a per-artifact document DB readable via live_artifact_read. Page code uses "
            "`const db = await claude.use('db')`. A save that should tell the agent the data is ready for review "
            "passes `{notify: true}`, or the page calls `claude.notify()`. Returns the deep link. When the result "
            "has push_on_notify=true (created from a chat session), a notify pushes a notice into that same session, so "
            "do not poll: share the link and end your turn. Pass wake=false to opt out. When push_on_notify is false "
            "nothing is pushed: share the link, ask the user to tell you when they have submitted, then call "
            "live_artifact_read. Call this tool directly with the html "
            "as a normal string argument; do not wrap it in the generic `tool_call` batch tool, whose `calls` argument is a "
            "JSON string and breaks on HTML with nested quotes.",
            {
                "title": {"type": "string"},
                "html": {"type": "string", "description": "Body HTML or full document"},
                "wake": {
                    "type": "boolean",
                    "description": "Push a notice to this session when the page notifies (default true)",
                },
                "capabilities": {
                    "type": "object",
                    "description": "Capability manifest: "
                    "{db:{}, user:{}} by default. To let the page call MCP tools add "
                    "mcp:{servers:[{server:'<configured MCP server>', tools:['<tool>']}]}; the operator must "
                    "also list the server under plugins.entries.live-artifacts.mcp_allowlist.",
                },
                "pinned": {"type": "boolean"},
                "seed": {"type": "object", "description": "{docPath: body} initial docs"},
            },
            ["title", "html"],
        ),
        live_artifact_create,
    ),
    (
        _schema(
            "live_artifact_update",
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
        live_artifact_update,
    ),
    (
        _schema(
            "live_artifact_list",
            "List artifacts (no html/token).",
            {"status": {"type": "string", "enum": ["active", "archived"]}},
            [],
        ),
        live_artifact_list,
    ),
    (
        _schema(
            "live_artifact_read",
            "Read an artifact's status and all db documents (what the user entered).",
            {"id": _ID},
            ["id"],
        ),
        live_artifact_read,
    ),
    (
        _schema(
            "live_artifact_db_write",
            "Seed or amend one db document of an artifact (full replace).",
            {
                "id": _ID,
                "path": {"type": "string", "description": "even-segment doc path, e.g. items/a1"},
                "data": {"type": "object"},
            },
            ["id", "path", "data"],
        ),
        live_artifact_db_write,
    ),
    (
        _schema(
            "live_artifact_archive",
            "Archive an artifact: hides it and revokes the link; data retained.",
            {"id": _ID},
            ["id"],
        ),
        live_artifact_archive,
    ),
    (
        _schema("live_artifact_delete", "Permanently delete an artifact and its data.", {"id": _ID}, ["id"]),
        live_artifact_delete,
    ),
    (
        _schema(
            "live_artifact_export",
            "Export all artifacts and docs to a JSON file; returns its path. Default location is the plugin's exports directory; `path` is checked against Hermes write policy.",
            {"path": {"type": "string"}},
            [],
        ),
        live_artifact_export,
    ),
]
