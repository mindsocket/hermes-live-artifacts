"""SQLite artifact store + Firestore-style document API (the `db` capability).

Shared by the agent tools (any process) and the HTTP listener, so every write bumps a
global `change_seq` in `meta` inside the same transaction; the listener's SSE loop polls it.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_DOC_BYTES = 256 * 1024
MAX_DEPTH = 32
MAX_DOCS_PER_ARTIFACT = 25_000
MAX_FILTERS = 10
MAX_IN_VALUES = 30
MAX_LIMIT = 1000
_SEG_RE = re.compile(r"^[A-Za-z0-9_\-.~:@+]+$")
SUBMIT_DOC = "meta/status"

SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, html TEXT NOT NULL,
  capabilities TEXT NOT NULL DEFAULT '{}', mcp_allowlist TEXT NOT NULL DEFAULT '[]',
  pinned INTEGER NOT NULL DEFAULT 0, origin_session TEXT, wake INTEGER NOT NULL DEFAULT 1, woken_at REAL,
  status TEXT NOT NULL DEFAULT 'active', token TEXT NOT NULL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL,
  submitted_at REAL, last_write_at REAL, archived_at REAL
);
CREATE TABLE IF NOT EXISTS docs (
  artifact_id TEXT NOT NULL, path TEXT NOT NULL, collection TEXT NOT NULL,
  data TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
  lease_holder TEXT, lease_expires REAL, updated_at REAL NOT NULL,
  PRIMARY KEY (artifact_id, path)
);
CREATE INDEX IF NOT EXISTS docs_coll ON docs (artifact_id, collection);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
INSERT OR IGNORE INTO meta (key, value) VALUES ('change_seq', 0);
"""


# Columns added after the first release: column -> DDL tail. Applied to databases created earlier.
MIGRATIONS = {
    "origin_session": "TEXT",
    "wake": "INTEGER NOT NULL DEFAULT 1",
    "woken_at": "REAL",
}


class DbError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


def default_data_dir() -> Path:
    override = os.environ.get("HERMES_LIVE_ARTIFACTS_DATA")
    if override:
        return Path(override)
    try:
        from hermes_constants import get_hermes_home

        home = Path(get_hermes_home())
    except Exception:  # standalone / tests
        home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    live_dir = home / "plugin-data" / "hermes-live-artifacts"
    return live_dir


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------- path / body validation
def split_path(path: str, *, kind: str) -> list[str]:
    """kind: 'doc' (even segments) or 'collection' (odd)."""
    if not isinstance(path, str) or not path or len(path.encode()) > 1000:
        raise DbError("invalid_argument", "path must be a non-empty string of at most 1000 bytes")
    segs = path.split("/")
    if len(segs) > 16:
        raise DbError("invalid_argument", "path has more than 16 segments")
    for s in segs:
        if s in ("", ".", "..") or not _SEG_RE.match(s) or len(s.encode()) > 200:
            raise DbError("invalid_argument", f"bad path segment {s!r}")
    even = len(segs) % 2 == 0
    if kind == "doc" and not even:
        raise DbError("invalid_argument", f"document path needs an even number of segments (got {len(segs)})")
    if kind == "collection" and even:
        raise DbError("invalid_argument", f"collection path needs an odd number of segments (got {len(segs)})")
    return segs


def _depth(v: Any, d: int = 1) -> int:
    if d > MAX_DEPTH:
        return d
    if isinstance(v, dict):
        return max([d] + [_depth(x, d + 1) for x in v.values()])
    if isinstance(v, list):
        return max([d] + [_depth(x, d + 1) for x in v])
    return d


def check_body(data: Any) -> str:
    if not isinstance(data, dict):
        raise DbError("invalid_argument", "document body must be a JSON object")
    try:
        s = json.dumps(data, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as e:
        raise DbError("invalid_argument", f"body is not plain JSON: {e}") from e
    if len(s.encode()) > MAX_DOC_BYTES:
        raise DbError("invalid_argument", "document exceeds 256 KiB")
    if _depth(data) > MAX_DEPTH:
        raise DbError("invalid_argument", "document nested deeper than 32 levels")
    return s


MIN_UNGUESSABLE_ID = 22  # ids from new_artifact_id(); older 12-char ids are guessable and still need the link secret


def new_artifact_id() -> str:
    """22 URL-safe chars from a random UUID (128 bits)."""
    return base64.urlsafe_b64encode(uuid.uuid4().bytes).rstrip(b"=").decode()


def merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------- query evaluation
def _rank(v: Any) -> tuple:
    if v is None:
        return (0, 0)
    if isinstance(v, bool):
        return (1, int(v))
    if isinstance(v, (int, float)):
        return (2, v)
    if isinstance(v, str):
        return (3, v)
    return (4, json.dumps(v, sort_keys=True))


_MISSING = object()


def _match(doc: dict, field: str, op: str, value: Any) -> bool:
    v = doc.get(field, _MISSING)
    if v is _MISSING:
        return False  # absent field never matches (Firestore semantics), incl. !=
    if op == "==":
        return _rank(v) == _rank(value)
    if op == "!=":
        return _rank(v) != _rank(value)
    if op in ("<", "<=", ">", ">="):
        a, b = _rank(v), _rank(value)
        if a[0] != b[0]:
            return False
        return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]
    if op == "in":
        return any(_rank(v) == _rank(x) for x in value)
    if op == "not-in":
        return all(_rank(v) != _rank(x) for x in value)
    if op == "array-contains":
        return isinstance(v, list) and any(_rank(x) == _rank(value) for x in v)
    raise DbError("invalid_argument", f"unknown operator {op!r}")


def _check_query(filters: list, order_by: list | None, limit: int | None) -> None:
    if len(filters) > MAX_FILTERS:
        raise DbError("invalid_argument", "more than 10 filters")
    for f in filters:
        if not (isinstance(f, (list, tuple)) and len(f) == 3 and isinstance(f[0], str)):
            raise DbError("invalid_argument", "filter must be [field, op, value]")
        if f[1] in ("in", "not-in"):
            if not isinstance(f[2], list) or len(f[2]) > MAX_IN_VALUES:
                raise DbError("invalid_argument", "in/not-in need an array of at most 30 values")
        elif f[1] not in ("==", "!=", "<", "<=", ">", ">=", "array-contains"):
            raise DbError("invalid_argument", f"unknown operator {f[1]!r}")
    if order_by is not None and (
        not isinstance(order_by, list) or len(order_by) != 2 or order_by[1] not in ("asc", "desc")
    ):
        raise DbError("invalid_argument", "orderBy must be [field, 'asc'|'desc']")
    if limit is not None and (not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT):
        raise DbError("invalid_argument", "limit must be 1-1000")


# ---------------------------------------------------------------- store
class Store:
    def __init__(self, data_dir: Path | None = None):
        self.dir = Path(data_dir) if data_dir else default_data_dir()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "artifacts.db"
        self._key = self._get_installation_key()
        # executescript() commits implicitly, so it can't run inside _tx()'s BEGIN IMMEDIATE.
        c = sqlite3.connect(self.path, timeout=10)
        try:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)
            have = {r[1] for r in c.execute("PRAGMA table_info(artifacts)")}
            for col, ddl in MIGRATIONS.items():  # CREATE IF NOT EXISTS never alters an existing table
                if col not in have:
                    c.execute(f"ALTER TABLE artifacts ADD COLUMN {col} {ddl}")
            c.commit()
        finally:
            c.close()

    def _get_installation_key(self) -> bytes:
        env_secret = os.environ.get("HERMES_LIVE_ARTIFACTS_SECRET")
        if env_secret:
            return env_secret.encode()
        key_file = self.dir / ".secret"
        if key_file.exists():
            try:
                return key_file.read_bytes()
            except Exception:
                pass
        secret = secrets.token_bytes(32)
        try:
            fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with open(fd, "wb") as f:
                f.write(secret)
        except Exception:
            pass
        return secret

    def derive_token(self, aid: str, seed: str) -> str:
        h = hmac.new(self._key, f"{aid}:{seed}".encode(), "sha256").digest()
        return base64.urlsafe_b64encode(h).rstrip(b"=").decode()

    # connection per call: cheap, thread/process safe under WAL
    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=10000")
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        finally:
            c.close()

    def _bump(self, c: sqlite3.Connection) -> None:
        c.execute("UPDATE meta SET value = value + 1 WHERE key='change_seq'")

    def change_seq(self) -> int:
        c = sqlite3.connect(self.path, timeout=10)
        try:
            return c.execute("SELECT value FROM meta WHERE key='change_seq'").fetchone()[0]
        finally:
            c.close()

    # ------------------------------------------------ artifacts
    def create_artifact(
        self,
        title: str,
        html: str,
        capabilities: dict | None = None,
        mcp_allowlist: list | None = None,
        pinned: bool = False,
        seed: dict | None = None,
        origin_session: str | None = None,
        wake: bool = True,
    ) -> dict:
        aid = new_artifact_id()
        now = time.time()
        with self._tx() as c:
            c.execute(
                "INSERT INTO artifacts (id,title,html,capabilities,mcp_allowlist,pinned,origin_session,wake,"
                "status,token,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    aid,
                    title,
                    html,
                    json.dumps(capabilities or {}),
                    json.dumps(mcp_allowlist or []),
                    int(pinned),
                    origin_session or None,
                    int(wake),
                    "active",
                    secrets.token_urlsafe(24),
                    now,
                    now,
                ),
            )
            self._bump(c)
        for path, data in (seed or {}).items():
            self.set_doc(aid, path, data)
        return self.get_artifact(aid)

    def get_artifact(self, aid: str) -> dict | None:
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            r = c.execute("SELECT * FROM artifacts WHERE id=?", (aid,)).fetchone()
        finally:
            c.close()
        if r is None:
            return None
        d = dict(r)
        d["capabilities"] = json.loads(d["capabilities"])
        d["mcp_allowlist"] = json.loads(d["mcp_allowlist"])
        d["pinned"] = bool(d["pinned"])
        d["wake"] = bool(d["wake"])
        d["token"] = self.derive_token(aid, d["token"])
        return d

    def list_artifacts(self, status: str | None = None) -> list[dict]:
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            q = "SELECT id FROM artifacts" + (" WHERE status=?" if status else "") + " ORDER BY updated_at DESC"
            ids = [r["id"] for r in c.execute(q, (status,) if status else ())]
        finally:
            c.close()
        out = []
        for i in ids:
            a = self.get_artifact(i)
            a.pop("html"), a.pop("token")
            out.append(a)
        return out

    def update_artifact(self, aid: str, **fields: Any) -> dict:
        allowed = {"title", "html", "capabilities", "mcp_allowlist", "pinned", "wake"}
        sets, vals = [], []
        for k, v in fields.items():
            if v is None:
                continue
            if k not in allowed:
                raise ValueError(f"cannot update {k}")
            sets.append(f"{k}=?")
            vals.append(
                json.dumps(v) if k in ("capabilities", "mcp_allowlist") else (int(v) if k in ("pinned", "wake") else v)
            )
            if k == "html":
                sets.append("woken_at=NULL")  # a revised page is a new round: its next submit may wake again
        if not sets:
            return self.get_artifact(aid)
        sets.append("updated_at=?")
        vals += [time.time(), aid]
        with self._tx() as c:
            if c.execute(f"UPDATE artifacts SET {', '.join(sets)} WHERE id=?", vals).rowcount == 0:
                raise KeyError(aid)
            self._bump(c)
        return self.get_artifact(aid)

    def claim_wake(self, aid: str) -> bool:
        """Atomically take the one-per-round wake for this artifact. True only for the caller that wins it,
        and only when the artifact has an origin session and wake is on, so duplicate submits (double tap,
        retried request, two tabs) cannot start duplicate agent turns."""
        with self._tx() as c:
            return (
                c.execute(
                    "UPDATE artifacts SET woken_at=? WHERE id=? AND woken_at IS NULL AND wake=1 "
                    "AND origin_session IS NOT NULL AND status!='archived'",
                    (time.time(), aid),
                ).rowcount
                == 1
            )

    def release_wake(self, aid: str) -> None:
        """Give the wake back when delivery failed, so a later submit can try again."""
        with self._tx() as c:
            c.execute("UPDATE artifacts SET woken_at=NULL WHERE id=?", (aid,))

    def rotate_token(self, aid: str) -> str:
        tok = secrets.token_urlsafe(24)
        with self._tx() as c:
            if c.execute("UPDATE artifacts SET token=? WHERE id=?", (tok, aid)).rowcount == 0:
                raise KeyError(aid)
        return self.derive_token(aid, tok)

    def archive(self, aid: str) -> None:
        with self._tx() as c:
            if (
                c.execute(
                    "UPDATE artifacts SET status='archived', archived_at=?, token=? WHERE id=?",
                    (time.time(), secrets.token_urlsafe(24), aid),
                ).rowcount
                == 0
            ):
                raise KeyError(aid)
            self._bump(c)

    def delete(self, aid: str) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM docs WHERE artifact_id=?", (aid,))
            if c.execute("DELETE FROM artifacts WHERE id=?", (aid,)).rowcount == 0:
                raise KeyError(aid)
            self._bump(c)

    def archive_due(self, days: int = 14, now: float | None = None) -> list[str]:
        """Archive unpinned artifacts idle `days` after submit (or last write). Returns ids."""
        now = now or time.time()
        cutoff = now - days * 86400
        with self._tx() as c:
            rows = c.execute(
                "SELECT id FROM artifacts WHERE status!='archived' AND pinned=0 AND "
                "COALESCE(submitted_at, last_write_at, created_at) < ?",
                (cutoff,),
            ).fetchall()
            for r in rows:
                c.execute(
                    "UPDATE artifacts SET status='archived', archived_at=?, token=? WHERE id=?",
                    (now, secrets.token_urlsafe(24), r["id"]),
                )
            if rows:
                self._bump(c)
        return [r["id"] for r in rows]

    # ------------------------------------------------ documents
    def _row_doc(self, r: sqlite3.Row) -> dict:
        return {
            "id": r["path"].rsplit("/", 1)[-1],
            "path": r["path"],
            "exists": True,
            "data": json.loads(r["data"]),
            "version": r["version"],
        }

    def get_doc(self, aid: str, path: str) -> dict:
        split_path(path, kind="doc")
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            r = c.execute("SELECT * FROM docs WHERE artifact_id=? AND path=?", (aid, path)).fetchone()
        finally:
            c.close()
        if r is None:
            return {"id": path.rsplit("/", 1)[-1], "path": path, "exists": False, "data": None}
        return self._row_doc(r)

    def _write(self, c: sqlite3.Connection, aid: str, path: str, data: dict, *, mode: str) -> None:
        segs = split_path(path, kind="doc")
        coll = "/".join(segs[:-1])
        row = c.execute("SELECT * FROM docs WHERE artifact_id=? AND path=?", (aid, path)).fetchone()
        now = time.time()
        if mode == "update":
            if row is None:
                raise DbError("invalid_argument", "update requires the document to exist")
            data = merge(json.loads(row["data"]), data)
        body = check_body(data)
        if row is None:
            n = c.execute("SELECT COUNT(*) FROM docs WHERE artifact_id=?", (aid,)).fetchone()[0]
            if n >= MAX_DOCS_PER_ARTIFACT:
                raise DbError("quota_exceeded", "artifact database holds the maximum of 25,000 documents")
            c.execute(
                "INSERT INTO docs (artifact_id,path,collection,data,version,updated_at) VALUES (?,?,?,?,1,?)",
                (aid, path, coll, body, now),
            )
        else:
            c.execute(
                "UPDATE docs SET data=?, version=version+1, updated_at=? WHERE artifact_id=? AND path=?",
                (body, now, aid, path),
            )
        self._after_write(c, aid, path, data, now)

    def _after_write(self, c: sqlite3.Connection, aid: str, path: str, data: dict, now: float) -> None:
        sets, vals = "last_write_at=?", [now]
        if path == SUBMIT_DOC and data.get("status") == "submitted":
            sets += ", status=CASE WHEN status='archived' THEN status ELSE 'submitted' END, "
            sets += "submitted_at=COALESCE(submitted_at, ?)"
            vals.append(now)
        c.execute(f"UPDATE artifacts SET {sets} WHERE id=?", [*vals, aid])
        self._bump(c)

    def _require_artifact(self, c: sqlite3.Connection, aid: str) -> None:
        r = c.execute("SELECT status FROM artifacts WHERE id=?", (aid,)).fetchone()
        if r is None:
            raise DbError("revoked", "artifact does not exist")
        if r["status"] == "archived":
            raise DbError("revoked", "artifact is archived")

    def set_doc(self, aid: str, path: str, data: dict) -> None:
        with self._tx() as c:
            self._require_artifact(c, aid)
            self._write(c, aid, path, data, mode="set")

    def update_doc(self, aid: str, path: str, data: dict) -> None:
        with self._tx() as c:
            self._require_artifact(c, aid)
            self._write(c, aid, path, data, mode="update")

    def delete_doc(self, aid: str, path: str) -> None:
        split_path(path, kind="doc")
        with self._tx() as c:
            self._require_artifact(c, aid)
            c.execute("DELETE FROM docs WHERE artifact_id=? AND path=?", (aid, path))
            self._bump(c)

    def query(
        self, aid: str, path: str, filters: list | None = None, order_by: list | None = None, limit: int | None = None
    ) -> list[dict]:
        split_path(path, kind="collection")
        filters = filters or []
        _check_query(filters, order_by, limit)
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            rows = c.execute("SELECT * FROM docs WHERE artifact_id=? AND collection=?", (aid, path)).fetchall()
        finally:
            c.close()
        docs = [self._row_doc(r) for r in rows]
        docs = [d for d in docs if all(_match(d["data"], f, o, v) for f, o, v in filters)]
        docs.sort(key=lambda d: d["id"])  # default order: doc id asc
        if order_by:
            field, direction = order_by
            have = [d for d in docs if field in d["data"]]
            missing = [d for d in docs if field not in d["data"]]
            have.sort(key=lambda d: _rank(d["data"][field]), reverse=(direction == "desc"))
            docs = have + missing  # missing field sorts last either way
        return docs[:limit] if limit else docs

    def acquire(self, aid: str, path: str, holder: str, ttl_ms: int = 0, data: dict | None = None) -> dict:
        split_path(path, kind="doc")
        if not isinstance(holder, str) or not holder:
            raise DbError("invalid_argument", "holder is required")
        ttl = 30000 if not ttl_ms else min(max(int(ttl_ms), 1000), 600000)
        now = time.time()
        with self._tx() as c:
            self._require_artifact(c, aid)
            row = c.execute("SELECT * FROM docs WHERE artifact_id=? AND path=?", (aid, path)).fetchone()
            busy = (
                row is not None
                and row["lease_holder"]
                and row["lease_holder"] != holder
                and (row["lease_expires"] or 0) > now
            )
            if busy:
                return {"acquired": False, "expiresAt": _iso(row["lease_expires"])}
            body = merge(json.loads(row["data"]) if row else {}, data or {})
            self._write(c, aid, path, body, mode="set")
            exp = now + ttl / 1000
            c.execute(
                "UPDATE docs SET lease_holder=?, lease_expires=? WHERE artifact_id=? AND path=?",
                (holder, exp, aid, path),
            )
            ver = c.execute("SELECT version FROM docs WHERE artifact_id=? AND path=?", (aid, path)).fetchone()[0]
            return {"acquired": True, "version": ver, "expiresAt": _iso(exp), "holder": holder}

    # ------------------------------------------------ bulk (read/export)
    def dump_docs(self, aid: str) -> dict[str, dict]:
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            rows = c.execute("SELECT path,data FROM docs WHERE artifact_id=? ORDER BY path", (aid,)).fetchall()
        finally:
            c.close()
        return {r["path"]: json.loads(r["data"]) for r in rows}

    def export_all(self) -> dict:
        out = []
        for a in self.list_artifacts():
            full = self.get_artifact(a["id"])
            full.pop("token")
            full["docs"] = self.dump_docs(a["id"])
            out.append(full)
        return {"exported_at": _iso(time.time()), "artifacts": out}
