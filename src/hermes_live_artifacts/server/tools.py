"""Agent-facing tools. Handlers take (args, **kw) and return a JSON string."""

from __future__ import annotations

import json
import os
from typing import Any

from . import mcp as mcp_mod
from .store import DbError, Store, needs_link_secret

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
    if needs_link_secret(a["id"], _link_secret):
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
        for k in ("id", "title", "status", "pinned", "wake", "created_at", "updated_at", "submitted_at")
        if k in a
    }
    out["push_on_submit"] = bool(a.get("wake") and a.get("origin_session"))
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


def artifact_create(args: dict, **_kw) -> str:
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


def artifact_update(args: dict, **_kw) -> str:
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


def artifact_list(args: dict, **_kw) -> str:
    items = store().list_artifacts(args.get("status"))
    return _ok(artifacts=[_summary(a) for a in items])


def artifact_read(args: dict, **_kw) -> str:
    aid = args.get("id")
    st = store()
    a = st.get_artifact(aid)
    if a is None:
        return _err(f"no such artifact {aid}")
    docs = st.dump_docs(aid)
    return _ok(**_summary(a), docs=docs, doc_count=len(docs))


def artifact_db_write(args: dict, **_kw) -> str:
    aid, path, data = args.get("id"), args.get("path"), args.get("data")
    try:
        store().set_doc(aid, path, data)
    except DbError as e:
        return _err(str(e))
    return _ok(path=path)


def artifact_rotate_link(args: dict, **_kw) -> str:
    """Rotate the artifact's token: existing links (?k=), cookies and open event streams die; the agent
    gets a fresh link. In `never` mode the id in the URL is the credential, so rotating does not revoke
    the page link — refuse rather than imply a revocation that did not happen."""
    aid = args.get("id")
    if not _link_secret:
        return _err("link rotation is meaningless with link_secret=never: the id in the URL is the "
                    "capability and rotating the token does not revoke the link. Use artifact_archive to revoke.")
    try:
        store().rotate_token(aid)
    except KeyError:
        return _err(f"no such artifact {aid}")
    a = store().get_artifact(aid)
    return _ok(link=link(a), rotated=aid)


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
            "artifact_rotate_link",
            "Rotate an artifact's link secret: old links, page cookies and open sessions stop working; "
            "returns the new link. Refuses with an error when link_secret is `never` (the URL id is the "
            "capability) — archive instead to revoke.",
            {"id": _ID},
            ["id"],
        ),
        artifact_rotate_link,
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
