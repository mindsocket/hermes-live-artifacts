import sqlite3
import time

import pytest

from hermes_live_artifacts.server import store as store_mod
from hermes_live_artifacts.server.store import DbError, Store


@pytest.fixture
def aid(store):
    return store.create_artifact("t", "<p>x</p>", {"db": {}})["id"]


def code(fn, *a, **k):
    with pytest.raises(DbError) as e:
        fn(*a, **k)
    return e.value.code


def test_set_get_roundtrip_and_missing(store, aid):
    assert store.get_doc(aid, "tasks/t1")["exists"] is False
    store.set_doc(aid, "tasks/t1", {"a": 1, "n": {"x": [1, 2]}})
    d = store.get_doc(aid, "tasks/t1")
    assert d["exists"] and d["data"] == {"a": 1, "n": {"x": [1, 2]}} and d["id"] == "t1"


def test_set_is_full_replace_update_merges_deep(store, aid):
    store.set_doc(aid, "c/d", {"a": 1, "n": {"x": 1, "y": 2}})
    store.update_doc(aid, "c/d", {"n": {"y": 3}, "arr": [1]})
    assert store.get_doc(aid, "c/d")["data"] == {"a": 1, "n": {"x": 1, "y": 3}, "arr": [1]}
    store.update_doc(aid, "c/d", {"arr": [2]})  # arrays replace wholesale
    assert store.get_doc(aid, "c/d")["data"]["arr"] == [2]
    store.set_doc(aid, "c/d", {"only": 1})
    assert store.get_doc(aid, "c/d")["data"] == {"only": 1}


def test_update_requires_existing(store, aid):
    assert code(store.update_doc, aid, "c/nope", {"a": 1}) == "invalid_argument"


def test_delete_idempotent_and_nested_survive(store, aid):
    store.set_doc(aid, "a/b", {"v": 1})
    store.set_doc(aid, "a/b/c/d", {"v": 2})
    store.delete_doc(aid, "a/b")
    store.delete_doc(aid, "a/b")
    assert not store.get_doc(aid, "a/b")["exists"]
    assert store.get_doc(aid, "a/b/c/d")["exists"]


@pytest.mark.parametrize(
    "path,kind",
    [
        ("a", "doc"),
        ("a/b/c", "doc"),
        ("a/b", "collection"),
        ("", "doc"),
        ("a/../b", "doc"),
        ("a//b", "doc"),
        ("a/b c", "doc"),
        ("a/" + "x" * 201, "doc"),
    ],
)
def test_bad_paths(store, aid, path, kind):
    fn = store.get_doc if kind == "doc" else store.query
    assert code(fn, aid, path) == "invalid_argument"


def test_body_limits(store, aid):
    assert code(store.set_doc, aid, "a/b", [1]) == "invalid_argument"
    assert code(store.set_doc, aid, "a/b", {"big": "x" * (256 * 1024)}) == "invalid_argument"
    deep = cur = {}
    for _ in range(40):
        cur["n"] = {}
        cur = cur["n"]
    assert code(store.set_doc, aid, "a/b", deep) == "invalid_argument"
    assert code(store.set_doc, aid, "a/b", {"f": float("nan")}) == "invalid_argument"


def test_doc_count_quota(store, aid, monkeypatch):
    monkeypatch.setattr(store_mod, "MAX_DOCS_PER_ARTIFACT", 3)
    for i in range(3):
        store.set_doc(aid, f"c/d{i}", {"i": i})
    assert code(store.set_doc, aid, "c/d9", {"i": 9}) == "quota_exceeded"
    store.set_doc(aid, "c/d0", {"i": 100})  # existing doc still writable


def test_total_bytes_quota(store, aid, monkeypatch):
    monkeypatch.setattr(store_mod, "MAX_TOTAL_DOC_BYTES", 1000)
    store.set_doc(aid, "c/big", {"blob": "x" * 900})  # 900+ overhead fits
    assert code(store.set_doc, aid, "c/next", {"blob": "x" * 900}) == "quota_exceeded"
    store.set_doc(aid, "c/big", {"blob": "x" * 100})  # replacing smaller is fine


def test_query_filters_order_limit(store, aid):
    for i, (n, tag) in enumerate([(3, "b"), (1, "a"), (2, "b"), (5, "c")]):
        store.set_doc(aid, f"items/i{i}", {"n": n, "tag": tag, "tags": [tag, "all"]})
    store.set_doc(aid, "items/nofield", {"other": 1})
    ids = lambda r: [d["id"] for d in r]  # noqa: E731
    assert ids(store.query(aid, "items", [["tag", "==", "b"]])) == ["i0", "i2"]
    assert ids(store.query(aid, "items", [["n", ">=", 2]], ["n", "asc"])) == ["i2", "i0", "i3"]
    assert ids(store.query(aid, "items", [["n", "!=", 3]])) == ["i1", "i2", "i3"]  # absent field excluded
    assert ids(store.query(aid, "items", [["tag", "in", ["a", "c"]]])) == ["i1", "i3"]
    assert ids(store.query(aid, "items", [["tag", "not-in", ["a", "c"]]])) == ["i0", "i2"]
    assert ids(store.query(aid, "items", [["tags", "array-contains", "c"]])) == ["i3"]
    assert ids(store.query(aid, "items", [], ["n", "desc"], 2)) == ["i3", "i0"]
    # missing field sorts last in both directions
    assert ids(store.query(aid, "items", [], ["n", "asc"]))[-1] == "nofield"
    assert ids(store.query(aid, "items", [], ["n", "desc"]))[-1] == "nofield"
    # default order = doc id asc
    assert ids(store.query(aid, "items")) == sorted(ids(store.query(aid, "items")))


def test_query_limits(store, aid):
    assert code(store.query, aid, "c", [["a", "==", 1]] * 11) == "invalid_argument"
    assert code(store.query, aid, "c", [["a", "in", list(range(31))]]) == "invalid_argument"
    assert code(store.query, aid, "c", [["a", "~", 1]]) == "invalid_argument"
    assert code(store.query, aid, "c", [], None, 1001) == "invalid_argument"
    assert code(store.query, aid, "c", [], ["a", "sideways"]) == "invalid_argument"


def test_collections_are_scoped(store, aid):
    store.set_doc(aid, "a/x", {"v": 1})
    store.set_doc(aid, "a/x/sub/y", {"v": 2})
    assert [d["id"] for d in store.query(aid, "a")] == ["x"]
    assert [d["id"] for d in store.query(aid, "a/x/sub")] == ["y"]


def test_artifacts_isolated(store):
    a = store.create_artifact("a", "x")["id"]
    b = store.create_artifact("b", "x")["id"]
    store.set_doc(a, "c/d", {"v": 1})
    assert not store.get_doc(b, "c/d")["exists"]


def test_acquire_lease(store, aid):
    r = store.acquire(aid, "locks/x", "me", 1000, {"owner": "me"})
    assert r["acquired"] and r["holder"] == "me" and r["version"] == 1
    assert store.acquire(aid, "locks/x", "other", 1000) == {"acquired": False, "expiresAt": r["expiresAt"]}
    assert store.acquire(aid, "locks/x", "me", 1000)["acquired"]  # renewal by same holder
    assert store.get_doc(aid, "locks/x")["data"] == {"owner": "me"}


def test_acquire_expiry(store, aid, monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(store_mod.time, "time", lambda: t[0])
    assert store.acquire(aid, "l/x", "a", 1000)["acquired"]
    assert not store.acquire(aid, "l/x", "b", 1000)["acquired"]
    t[0] += 2
    assert store.acquire(aid, "l/x", "b", 1000)["acquired"]


def test_submit_convention_sets_status(store, aid):
    assert store.get_artifact(aid)["status"] == "active"
    store.set_doc(aid, "meta/status", {"status": "submitted"})
    a = store.get_artifact(aid)
    assert a["status"] == "submitted" and a["submitted_at"]


def test_update_artifact_in_place_keeps_data(store, aid):
    store.set_doc(aid, "c/d", {"v": 1})
    store.update_artifact(aid, html="<p>v2</p>", title="new")
    a = store.get_artifact(aid)
    assert a["html"] == "<p>v2</p>" and a["title"] == "new"
    assert store.get_doc(aid, "c/d")["data"] == {"v": 1}


def test_archive_revokes_and_blocks_writes(store, aid):
    tok = store.get_artifact(aid)["token"]
    store.archive(aid)
    a = store.get_artifact(aid)
    assert a["status"] == "archived" and a["token"] != tok
    assert code(store.set_doc, aid, "c/d", {"v": 1}) == "revoked"
    assert store.dump_docs(aid) == {}


def test_archive_due_respects_pin_and_submit(store):
    day = 86400
    old = store.create_artifact("old", "x")["id"]
    pinned = store.create_artifact("pin", "x", pinned=True)["id"]
    fresh = store.create_artifact("fresh", "x")["id"]
    now = time.time() + 15 * day
    store.set_doc(
        fresh, "meta/status", {"status": "submitted"}
    )  # submitted ~now(real) => not due at +15d? it IS 15d old
    due = store.archive_due(14, now=now)
    assert set(due) == {old, fresh}
    assert store.get_artifact(pinned)["status"] != "archived"


def test_rotate_token(store, aid):
    t = store.get_artifact(aid)["token"]
    assert store.rotate_token(aid) != t


def test_only_a_salt_is_stored_not_the_token(store, aid):
    c = sqlite3.connect(store.path)
    raw_db_salt = c.execute("SELECT salt FROM artifacts WHERE id=?", (aid,)).fetchone()[0]
    c.close()
    derived = store.get_artifact(aid)["token"]
    assert raw_db_salt != derived
    assert store.derive_token(aid, raw_db_salt) == derived


def test_delete_removes_docs(store, aid):
    store.set_doc(aid, "c/d", {"v": 1})
    store.delete(aid)
    assert store.get_artifact(aid) is None and store.dump_docs(aid) == {}


def test_change_seq_bumps(store, aid):
    a = store.change_seq()
    store.set_doc(aid, "c/d", {"v": 1})
    assert store.change_seq() > a


def test_seed_on_create(store):
    a = store.create_artifact("t", "x", seed={"items/a": {"v": 1}})
    assert store.get_doc(a["id"], "items/a")["data"] == {"v": 1}


def test_concurrent_writers_two_connections(store, aid):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(8) as ex:
        list(ex.map(lambda i: store.set_doc(aid, f"c/d{i}", {"i": i}), range(40)))
    assert len(store.query(aid, "c")) == 40


# ---------------------------------------------------------------- installation key (.secret)
def test_data_dir_and_db_are_private(tmp_path):
    Store(tmp_path)
    assert tmp_path.stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "artifacts.db").stat().st_mode & 0o777 == 0o600


def test_existing_dir_with_loose_mode_is_tightened(tmp_path):
    tmp_path.chmod(0o755)
    Store(tmp_path)
    assert tmp_path.stat().st_mode & 0o777 == 0o700


def test_key_is_created_once_private_and_reused(tmp_path):
    k1 = Store(tmp_path)._key
    assert len(k1) == 32 and (tmp_path / ".secret").stat().st_mode & 0o777 == 0o600
    assert Store(tmp_path)._key == k1
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".secret.")] == []  # no temp file left


@pytest.mark.parametrize("content", [b"", b"short"])
def test_empty_or_short_key_file_is_refused_not_used(tmp_path, content):
    (tmp_path / ".secret").write_bytes(content)
    with pytest.raises(RuntimeError, match="at least 32"):
        Store(tmp_path)
    assert (tmp_path / ".secret").read_bytes() == content  # not overwritten: that would change every token


def test_short_env_key_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_LIVE_ARTIFACTS_SECRET", "short")
    with pytest.raises(RuntimeError, match="HERMES_LIVE_ARTIFACTS_SECRET"):
        Store(tmp_path)
    monkeypatch.setenv("HERMES_LIVE_ARTIFACTS_SECRET", "x" * 32)
    assert Store(tmp_path)._key == b"x" * 32
    assert not (tmp_path / ".secret").exists()


def test_concurrent_first_starts_agree_on_one_key(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(8) as ex:
        keys = list(ex.map(lambda _: Store(tmp_path)._key, range(8)))
    assert len(set(keys)) == 1 and keys[0] == (tmp_path / ".secret").read_bytes()
