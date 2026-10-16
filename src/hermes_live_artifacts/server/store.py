"""SQLite artifact store + Firestore-style document API (the `db` capability).

Shared by the agent tools (any process) and the HTTP listener, so every write bumps a
global `change_seq` in `meta` inside the same transaction; the listener's SSE loop polls it.
"""

from __future__ import annotations

import base64
import hmac
import json
import operator
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

MIN_KEY_BYTES = 32  # minimum installation HMAC key length
MAX_DOC_BYTES = 256 * 1024
MAX_DEPTH = 32
MAX_DOCS_PER_ARTIFACT = 25_000
MAX_TOTAL_DOC_BYTES = 50 * 1024 * 1024  # per-artifact sum of document bodies
MAX_FILTERS = 10
MAX_IN_VALUES = 30
MAX_LIMIT = 1000
DEFAULT_LEASE_MS, MIN_LEASE_MS, MAX_LEASE_MS = 30_000, 1_000, 600_000
_SEG_RE = re.compile(r"^[A-Za-z0-9_\-.~:@+]+$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, html TEXT NOT NULL,
  capabilities TEXT NOT NULL DEFAULT '{}',
  pinned INTEGER NOT NULL DEFAULT 0, origin_session TEXT, wake INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'active', salt TEXT NOT NULL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL,
  notified_at REAL, archived_at REAL
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


class DbError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


def hermes_home() -> Path:
    """Hermes' home directory; outside a Hermes process (tests, standalone) `$HERMES_HOME` or ~/.hermes."""
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return Path(get_hermes_home())


def default_data_dir() -> Path:
    return hermes_home() / "plugin-data" / "hermes-live-artifacts"


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _new_salt() -> str:
    """Random per-artifact seed; the page token is derive_token(id, salt), so replacing the salt revokes it."""
    return secrets.token_urlsafe(24)


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


def needs_link_secret(aid: str, link_secret: bool) -> bool:
    """Page loads and links carry the secret when configured to, and always for guessable legacy ids."""
    return link_secret or len(aid) < MIN_UNGUESSABLE_ID


def new_artifact_id() -> str:
    """22 URL-safe chars from a random UUIDv4."""
    return _b64(uuid.uuid4().bytes)


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
_ORDERING = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge}
_OPERATORS = {*_ORDERING, "==", "!=", "in", "not-in", "array-contains"}


def _match(doc: dict, field: str, op: str, value: Any) -> bool:
    v = doc.get(field, _MISSING)
    if v is _MISSING:
        return False  # absent field never matches (Firestore semantics), incl. !=
    if op == "==":
        return _rank(v) == _rank(value)
    if op == "!=":
        return _rank(v) != _rank(value)
    if op in _ORDERING:
        a, b = _rank(v), _rank(value)
        return a[0] == b[0] and _ORDERING[op](a, b)
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
        elif f[1] not in _OPERATORS:
            raise DbError("invalid_argument", f"unknown operator {f[1]!r}")
    if order_by is not None and (
        not isinstance(order_by, list) or len(order_by) != 2 or order_by[1] not in ("asc", "desc")
    ):
        raise DbError("invalid_argument", "orderBy must be [field, 'asc'|'desc']")
    if limit is not None and (not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT):
        raise DbError("invalid_argument", "limit must be 1-1000")


# ---------------------------------------------------------------- store
CONTENT_FIELDS = frozenset({"title", "html", "capabilities"})
_UPDATE_ENCODERS = {"title": str, "html": str, "capabilities": json.dumps, "pinned": int, "wake": int}


class Store:
    def __init__(self, data_dir: Path | None = None):
        self.dir = Path(data_dir) if data_dir else default_data_dir()
        # 0700: artifact data (forms, db contents) is private to the operating user, so other local
        # accounts cannot read the database or key file through the directory.
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.dir, 0o700)  # an existing dir may predate the mode
        except OSError:
            pass
        self.path = self.dir / "artifacts.db"
        self._key = self._get_installation_key()
        # executescript() commits implicitly, so it can't run inside _tx()'s BEGIN IMMEDIATE.
        try:
            # A fresh db gets 0600 from the start; an existing one keeps whatever it had (re-chmodding a
            # live multi-connection db is racy, and the 0700 directory already gates it). O_EXCL so two
            # processes starting together cannot clobber each other's file.
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        c = sqlite3.connect(self.path, timeout=10)
        try:
            # First-start races (every Hermes process loads the plugin) can hit "database is locked"
            # while one connection sets journal_mode while another runs executescript; retry briefly.
            for attempt in range(5):
                try:
                    c.execute("PRAGMA journal_mode=WAL")  # persists in the db file, so _tx() needn't repeat it
                    c.executescript(SCHEMA)
                    c.commit()
                    break
                except sqlite3.OperationalError:
                    if attempt == 4:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        finally:
            c.close()

    def _get_installation_key(self) -> bytes:
        """The HMAC key tokens are derived from: `<data dir>/.secret`.

        Plugins load in every Hermes process, so two can start at once. The key is written to a private temp
        file in full, then hard-linked into place: `link` fails if `.secret` exists, so exactly one writer wins,
        and a reader never sees a partial file. Any short key or I/O error raises; a silent fallback would
        either change every token on restart or derive them from an empty key."""
        key_file = self.dir / ".secret"
        if not key_file.exists():
            tmp = self.dir / f".secret.{os.getpid()}.{secrets.token_hex(4)}.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(secrets.token_bytes(MIN_KEY_BYTES))
                    f.flush()
                    os.fsync(f.fileno())
                try:
                    os.link(tmp, key_file)
                except FileExistsError:
                    pass  # another process won the race; read its key below
            finally:
                tmp.unlink(missing_ok=True)
        return self._check_key(key_file.read_bytes(), str(key_file))

    @staticmethod
    def _check_key(key: bytes, where: str) -> bytes:
        if len(key) < MIN_KEY_BYTES:
            raise RuntimeError(
                f"{where} holds {len(key)} bytes; the installation key needs at least {MIN_KEY_BYTES}. "
                "If it is a damaged .secret, delete it to mint a new key (every existing link and page token "
                "then changes)."
            )
        return key

    def derive_token(self, aid: str, seed: str) -> str:
        h = hmac.new(self._key, f"{aid}:{seed}".encode(), "sha256").digest()
        return _b64(h)

    # connection per call: cheap, thread/process safe under WAL
    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=10)
        try:
            c.row_factory = sqlite3.Row
            yield c
        finally:
            c.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            c.row_factory = sqlite3.Row
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
        with self._read() as c:
            return c.execute("SELECT value FROM meta WHERE key='change_seq'").fetchone()[0]

    # ------------------------------------------------ artifacts
    def create_artifact(
        self,
        title: str,
        html: str,
        capabilities: dict | None = None,
        pinned: bool = False,
        seed: dict | None = None,
        origin_session: str | None = None,
        wake: bool = True,
    ) -> dict:
        aid = new_artifact_id()
        now = time.time()
        with self._tx() as c:
            c.execute(
                "INSERT INTO artifacts (id,title,html,capabilities,pinned,origin_session,wake,"
                "status,salt,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    aid,
                    title,
                    html,
                    json.dumps(capabilities or {}),
                    int(pinned),
                    origin_session or None,
                    int(wake),
                    "active",
                    _new_salt(),
                    now,
                    now,
                ),
            )
            self._bump(c)
        for path, data in (seed or {}).items():
            self.set_doc(aid, path, data)
        return self.get_artifact(aid)

    def _artifact_from_row(self, r: sqlite3.Row) -> dict:
        d = dict(r)
        d["capabilities"] = json.loads(d["capabilities"])
        d["pinned"] = bool(d["pinned"])
        d["wake"] = bool(d["wake"])
        d["token"] = self.derive_token(d["id"], d.pop("salt"))
        return d

    def get_artifact(self, aid: str) -> dict | None:
        with self._read() as c:
            r = c.execute("SELECT * FROM artifacts WHERE id=?", (aid,)).fetchone()
        return None if r is None else self._artifact_from_row(r)

    def list_artifacts(self, status: str | None = None, *, full: bool = False) -> list[dict]:
        """Newest first. Summaries omit `html` and `token`; `full` keeps `html` (never the token)."""
        q = "SELECT * FROM artifacts" + (" WHERE status=?" if status else "") + " ORDER BY updated_at DESC"
        with self._read() as c:
            rows = c.execute(q, (status,) if status else ()).fetchall()
        out = []
        for r in rows:
            a = self._artifact_from_row(r)
            del a["token"]
            if not full:
                del a["html"]
            out.append(a)
        return out

    def update_artifact(self, aid: str, **fields: Any) -> dict:
        sets, vals = [], []
        for k, v in fields.items():
            if v is None:
                continue
            if k not in _UPDATE_ENCODERS:
                raise ValueError(f"cannot update {k}")
            sets.append(f"{k}=?")
            vals.append(_UPDATE_ENCODERS[k](v))
        if not sets:
            return self.get_artifact(aid)
        # Only content counts as an update: pinning or waking must not reorder the list or read as "edited".
        if any(fields.get(k) is not None for k in CONTENT_FIELDS):
            sets.append("updated_at=?")
            vals.append(time.time())
        vals.append(aid)
        with self._tx() as c:
            if c.execute(f"UPDATE artifacts SET {', '.join(sets)} WHERE id=?", vals).rowcount == 0:
                raise KeyError(aid)
            self._bump(c)
        return self.get_artifact(aid)

    def notify(self, aid: str) -> dict:
        """The page says its data is ready for review. Records the time only: a notify carries no payload."""
        with self._tx() as c:
            self._require_artifact(c, aid)
            self._mark_notified(c, aid, time.time())
            self._bump(c)
        return self.get_artifact(aid)

    @staticmethod
    def _mark_notified(c: sqlite3.Connection, aid: str, now: float) -> None:
        c.execute("UPDATE artifacts SET notified_at=? WHERE id=?", (now, aid))

    def rotate_token(self, aid: str) -> str:
        salt = _new_salt()
        with self._tx() as c:
            if c.execute("UPDATE artifacts SET salt=? WHERE id=?", (salt, aid)).rowcount == 0:
                raise KeyError(aid)
        return self.derive_token(aid, salt)

    @staticmethod
    def _archive_row(c: sqlite3.Connection, aid: str, now: float) -> int:
        """Archive and replace the salt, which revokes the page token."""
        return c.execute(
            "UPDATE artifacts SET status='archived', archived_at=?, salt=? WHERE id=?", (now, _new_salt(), aid)
        ).rowcount

    def archive(self, aid: str) -> None:
        with self._tx() as c:
            if self._archive_row(c, aid, time.time()) == 0:
                raise KeyError(aid)
            self._bump(c)

    def delete(self, aid: str) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM docs WHERE artifact_id=?", (aid,))
            if c.execute("DELETE FROM artifacts WHERE id=?", (aid,)).rowcount == 0:
                raise KeyError(aid)
            self._bump(c)

    # ------------------------------------------------ documents
    def _row_doc(self, r: sqlite3.Row) -> dict:
        return {
            "id": r["path"].rsplit("/", 1)[-1],
            "path": r["path"],
            "exists": True,
            "data": json.loads(r["data"]),
            "version": r["version"],
        }

    @staticmethod
    def _doc_row(c: sqlite3.Connection, aid: str, path: str) -> sqlite3.Row | None:
        return c.execute("SELECT * FROM docs WHERE artifact_id=? AND path=?", (aid, path)).fetchone()

    def get_doc(self, aid: str, path: str) -> dict:
        split_path(path, kind="doc")
        with self._read() as c:
            r = self._doc_row(c, aid, path)
        if r is None:
            return {"id": path.rsplit("/", 1)[-1], "path": path, "exists": False, "data": None}
        return self._row_doc(r)

    def _write(self, c: sqlite3.Connection, aid: str, path: str, data: dict, *, mode: str, notify: bool) -> None:
        segs = split_path(path, kind="doc")
        coll = "/".join(segs[:-1])
        row = self._doc_row(c, aid, path)
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
            total = c.execute("SELECT COALESCE(SUM(LENGTH(data)),0) FROM docs WHERE artifact_id=?", (aid,)).fetchone()[
                0
            ]
            if total + len(body.encode()) > MAX_TOTAL_DOC_BYTES:
                raise DbError("quota_exceeded", "artifact database exceeds its 50 MiB document quota")
            c.execute(
                "INSERT INTO docs (artifact_id,path,collection,data,version,updated_at) VALUES (?,?,?,?,1,?)",
                (aid, path, coll, body, now),
            )
        else:
            c.execute(
                "UPDATE docs SET data=?, version=version+1, updated_at=? WHERE artifact_id=? AND path=?",
                (body, now, aid, path),
            )
        if notify:
            self._mark_notified(c, aid, now)
        self._bump(c)

    def _require_artifact(self, c: sqlite3.Connection, aid: str) -> None:
        r = c.execute("SELECT status FROM artifacts WHERE id=?", (aid,)).fetchone()
        if r is None:
            raise DbError("revoked", "artifact does not exist")
        if r["status"] == "archived":
            raise DbError("revoked", "artifact is archived")

    def set_doc(self, aid: str, path: str, data: dict, *, notify: bool = False) -> None:
        with self._tx() as c:
            self._require_artifact(c, aid)
            self._write(c, aid, path, data, mode="set", notify=notify)

    def update_doc(self, aid: str, path: str, data: dict, *, notify: bool = False) -> None:
        with self._tx() as c:
            self._require_artifact(c, aid)
            self._write(c, aid, path, data, mode="update", notify=notify)

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
        with self._read() as c:
            rows = c.execute("SELECT * FROM docs WHERE artifact_id=? AND collection=?", (aid, path)).fetchall()
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
        ttl = DEFAULT_LEASE_MS if not ttl_ms else min(max(int(ttl_ms), MIN_LEASE_MS), MAX_LEASE_MS)
        now = time.time()
        with self._tx() as c:
            self._require_artifact(c, aid)
            row = self._doc_row(c, aid, path)
            busy = (
                row is not None
                and row["lease_holder"]
                and row["lease_holder"] != holder
                and (row["lease_expires"] or 0) > now
            )
            if busy:
                return {"acquired": False, "expiresAt": _iso(row["lease_expires"])}
            body = merge(json.loads(row["data"]) if row else {}, data or {})
            self._write(c, aid, path, body, mode="set", notify=False)
            exp = now + ttl / 1000
            c.execute(
                "UPDATE docs SET lease_holder=?, lease_expires=? WHERE artifact_id=? AND path=?",
                (holder, exp, aid, path),
            )
            ver = self._doc_row(c, aid, path)["version"]
            return {"acquired": True, "version": ver, "expiresAt": _iso(exp), "holder": holder}

    # ------------------------------------------------ bulk (read/export)
    def dump_docs(self, aid: str) -> dict[str, dict]:
        with self._read() as c:
            rows = c.execute("SELECT path,data FROM docs WHERE artifact_id=? ORDER BY path", (aid,)).fetchall()
        return {r["path"]: json.loads(r["data"]) for r in rows}

    def doc_counts(self) -> dict[str, int]:
        with self._read() as c:
            return {r[0]: r[1] for r in c.execute("SELECT artifact_id, COUNT(*) FROM docs GROUP BY artifact_id")}

    def export_all(self) -> dict:
        artifacts = self.list_artifacts(full=True)
        for a in artifacts:
            a["docs"] = self.dump_docs(a["id"])
        return {"exported_at": _iso(time.time()), "artifacts": artifacts}
