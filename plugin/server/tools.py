"""Agent-facing tools. Handlers take (args, **kw) and return a JSON string."""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any

from . import mcp as mcp_mod
from .store import DbError, Store, hermes_home

_store: Store | None = None


def store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


_base_url = "http://127.0.0.1:47318"
_link_secret = True
_index = False
_work_dir: Path | None = None  # tests point this elsewhere


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
        from gateway.session_context import get_session_env  # noqa: PLC0415 - Hermes-only module

        key = get_session_env("HERMES_SESSION_KEY", "")
    except ImportError:
        pass
    if not key:
        try:
            from tools.approval_context import get_current_session_key  # noqa: PLC0415 - Hermes-only module

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


_LINK_ID = re.compile(r"/a/([A-Za-z0-9_-]+)")


def artifact_id(value: Any) -> Any:
    """Accept an artifact link (`.../a/<id>`, with or without `/frame` or `?k=`) wherever an id is expected."""
    if isinstance(value, str) and "/a/" in value:
        m = _LINK_ID.search(value.split("?", 1)[0])
        if m:
            return m.group(1)
    return value


def work_dir() -> Path:
    """Where page source is handed to the agent as files: `live-artifacts/` in Hermes' scratch dir, which every
    session is told about and which is pruned when idle. Nothing here is managed state; the store is the source."""
    if _work_dir is not None:
        base = _work_dir
    else:
        try:
            from hermes_constants import get_scratch_dir  # noqa: PLC0415 - Hermes-only module
        except ImportError:  # outside Hermes (e2e fixture): the same layout under hermes_home()
            scratch = hermes_home() / "cache" / "scratch"
        else:
            scratch = get_scratch_dir()
        base = scratch / "live-artifacts"
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    return base


def _read_work_file(name: Any) -> str:
    """Text of `name`, a file name (or relative path) in the work dir. Anything resolving outside it is refused,
    so a tool argument cannot copy an arbitrary local file into a served page."""
    if not isinstance(name, str) or not name:
        raise ValueError("html_file must be a file name in work_dir")
    rel = Path(name)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError("html_file is a file name in work_dir, not a path")
    root = work_dir().resolve()
    target = (root / rel).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise ValueError(f"no file {name!r} in work_dir ({root})")
    return target.read_text(encoding="utf-8")


def _html_arg(args: dict) -> str | None:
    if args.get("html") is not None and args.get("html_file") is not None:
        raise ValueError("pass html or html_file, not both")
    if args.get("html_file") is not None:
        return _read_work_file(args["html_file"])
    return args.get("html")


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
    try:
        title, html = args.get("title"), _html_arg(args)
        if not title or not html:
            return _err("title and html (or html_file) are required")
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
    aid = artifact_id(args.get("id"))
    try:
        caps = _capabilities(args.get("capabilities"), default=None)
        a = store().update_artifact(
            aid,
            title=args.get("title"),
            html=_html_arg(args),
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
    """The page. Its html goes to a fresh file in the work dir rather than into the result: pages are large, and the
    agent edits the file with its own tools and hands the name back to live_artifact_update."""
    aid = artifact_id(args.get("id"))
    a = store().get_artifact(aid)
    if a is None:
        return _err(f"no such artifact {aid}")
    name = f"{aid}-{secrets.token_hex(3)}.html"  # one copy per read, so two sessions never edit the same file
    target = work_dir() / name
    target.write_text(a["html"], encoding="utf-8")
    return _ok(
        **_summary(a),
        link=link(a),
        capabilities=a["capabilities"],
        html_file=name,
        html_chars=len(a["html"]),
    )


def live_artifact_db_read(args: dict, **_kw) -> str:
    aid = artifact_id(args.get("id"))
    st = store()
    a = st.get_artifact(aid)
    if a is None:
        return _err(f"no such artifact {aid}")
    docs = st.dump_docs(aid)
    return _ok(**_summary(a), docs=docs, doc_count=len(docs))


def live_artifact_db_write(args: dict, **_kw) -> str:
    aid, path, data = artifact_id(args.get("id")), args.get("path"), args.get("data")
    try:
        store().set_doc(aid, path, data)
    except DbError as e:
        return _err(str(e))
    return _ok(path=path)


def live_artifact_archive(args: dict, **_kw) -> str:
    try:
        store().archive(aid := artifact_id(args.get("id")))
    except KeyError:
        return _err("no such artifact")
    return _ok(archived=aid)


def live_artifact_delete(args: dict, **_kw) -> str:
    try:
        store().delete(aid := artifact_id(args.get("id")))
    except KeyError:
        return _err("no such artifact")
    return _ok(deleted=aid)


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
            from agent.file_safety import get_write_denied_error, is_write_approval_required  # noqa: PLC0415 - Hermes-only module
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


_ID = {"type": "string", "description": "Artifact id or link"}
_HTML_FILE = {
    "type": "string",
    "description": "Instead of html: a file name in work_dir ({work_dir})",
}
TOOLS = [
    (
        _schema(
            "live_artifact_create",
            "Create an interactive HTML page (form, review list, dashboard) the user opens by link. First load the "
            "`live-artifacts:capabilities` skill (page-side db, notify, what happens on submit) and the "
            "`live-artifacts:design` skill (layout, theming). Returns the link and push_on_notify: when true, the "
            "page's notify pushes a notice into this session, so share the link and end your turn; when false, ask "
            "the user to tell you when they are done. Then read their input with live_artifact_db_read. Call this "
            "tool directly, not through the `tool_call` batch tool, whose JSON-string `calls` breaks on HTML.",
            {
                "title": {"type": "string"},
                "html": {"type": "string", "description": "Body HTML or full document"},
                "html_file": _HTML_FILE,
                "wake": {"type": "boolean", "description": "Push a notice to this session on notify (default true)"},
                "capabilities": {
                    "type": "object",
                    "description": "Default {db:{}, user:{}}. For page MCP calls see the capabilities skill.",
                },
                "pinned": {"type": "boolean"},
                "seed": {"type": "object", "description": "{docPath: body} initial docs"},
            },
            ["title"],
        ),
        live_artifact_create,
    ),
    (
        _schema(
            "live_artifact_read",
            "Read an artifact's page: status, link, capabilities, and html_file, a fresh copy of its HTML in "
            "work_dir to edit and pass back to update or create. Use this, not a fetch, for a link the user gives "
            "you. For what the user entered, use live_artifact_db_read. work_dir: {work_dir}",
            {"id": _ID},
            ["id"],
        ),
        live_artifact_read,
    ),
    (
        _schema(
            "live_artifact_update",
            "Update an artifact in place; its db is kept. html or html_file replaces the whole page. Returns the link.",
            {
                "id": _ID,
                "title": {"type": "string"},
                "html": {"type": "string"},
                "html_file": _HTML_FILE,
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
            "live_artifact_db_read",
            "Read an artifact's status and every db document (what the user entered), keyed by path.",
            {"id": _ID},
            ["id"],
        ),
        live_artifact_db_read,
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
            "Backup: export every artifact and its docs to a JSON file; returns its path. Default location is the "
            "plugin's exports directory; `path` is checked against Hermes write policy.",
            {"path": {"type": "string"}},
            [],
        ),
        live_artifact_export,
    ),
]


def tool_schemas() -> list[tuple[dict, Any]]:
    """TOOLS with this instance's work dir filled in, so the agent is told the path rather than left to derive it."""
    wd = json.dumps(str(work_dir()))[1:-1]
    return [(json.loads(json.dumps(s).replace("{work_dir}", wd)), h) for s, h in TOOLS]
