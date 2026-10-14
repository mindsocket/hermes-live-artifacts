import pytest

from hermes_live_artifacts.server.store import DbError, Store


@pytest.fixture
def s(tmp_path):
    return Store(tmp_path)


@pytest.fixture
def aid(s):
    return s.create_artifact("t", "<p>x</p>", {"db": {}})["id"]


def code(fn, *a, **k):
    with pytest.raises(DbError) as e:
        fn(*a, **k)
    return e.value.code


def test_set_get_roundtrip_and_missing(s, aid):
    assert s.get_doc(aid, "tasks/t1")["exists"] is False
    s.set_doc(aid, "tasks/t1", {"a": 1, "n": {"x": [1, 2]}})
    d = s.get_doc(aid, "tasks/t1")
    assert d["exists"] and d["data"] == {"a": 1, "n": {"x": [1, 2]}} and d["id"] == "t1"


def test_set_is_full_replace_update_merges_deep(s, aid):
    s.set_doc(aid, "c/d", {"a": 1, "n": {"x": 1, "y": 2}})
    s.update_doc(aid, "c/d", {"n": {"y": 3}, "arr": [1]})
    assert s.get_doc(aid, "c/d")["data"] == {"a": 1, "n": {"x": 1, "y": 3}, "arr": [1]}
    s.update_doc(aid, "c/d", {"arr": [2]})  # arrays replace wholesale
    assert s.get_doc(aid, "c/d")["data"]["arr"] == [2]
    s.set_doc(aid, "c/d", {"only": 1})
    assert s.get_doc(aid, "c/d")["data"] == {"only": 1}


def test_update_requires_existing(s, aid):
    assert code(s.update_doc, aid, "c/nope", {"a": 1}) == "invalid_argument"


def test_delete_idempotent_and_nested_survive(s, aid):
    s.set_doc(aid, "a/b", {"v": 1})
    s.set_doc(aid, "a/b/c/d", {"v": 2})
    s.delete_doc(aid, "a/b")
    s.delete_doc(aid, "a/b")
    assert not s.get_doc(aid, "a/b")["exists"]
    assert s.get_doc(aid, "a/b/c/d")["exists"]


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
def test_bad_paths(s, aid, path, kind):
    fn = s.get_doc if kind == "doc" else s.query
    assert code(fn, aid, path) == "invalid_argument"


def test_body_limits(s, aid):
    assert code(s.set_doc, aid, "a/b", [1]) == "invalid_argument"
    assert code(s.set_doc, aid, "a/b", {"big": "x" * (256 * 1024)}) == "invalid_argument"
    deep = cur = {}
    for _ in range(40):
        cur["n"] = {}
        cur = cur["n"]
    assert code(s.set_doc, aid, "a/b", deep) == "invalid_argument"
    assert code(s.set_doc, aid, "a/b", {"f": float("nan")}) == "invalid_argument"


def test_doc_count_quota(s, aid, monkeypatch):
    import hermes_live_artifacts.server.store as st

    monkeypatch.setattr(st, "MAX_DOCS_PER_ARTIFACT", 3)
    for i in range(3):
        s.set_doc(aid, f"c/d{i}", {"i": i})
    assert code(s.set_doc, aid, "c/d9", {"i": 9}) == "quota_exceeded"
    s.set_doc(aid, "c/d0", {"i": 100})  # existing doc still writable


def test_query_filters_order_limit(s, aid):
    for i, (n, tag) in enumerate([(3, "b"), (1, "a"), (2, "b"), (5, "c")]):
        s.set_doc(aid, f"items/i{i}", {"n": n, "tag": tag, "tags": [tag, "all"]})
    s.set_doc(aid, "items/nofield", {"other": 1})
    ids = lambda r: [d["id"] for d in r]  # noqa: E731
    assert ids(s.query(aid, "items", [["tag", "==", "b"]])) == ["i0", "i2"]
    assert ids(s.query(aid, "items", [["n", ">=", 2]], ["n", "asc"])) == ["i2", "i0", "i3"]
    assert ids(s.query(aid, "items", [["n", "!=", 3]])) == ["i1", "i2", "i3"]  # absent field excluded
    assert ids(s.query(aid, "items", [["tag", "in", ["a", "c"]]])) == ["i1", "i3"]
    assert ids(s.query(aid, "items", [["tag", "not-in", ["a", "c"]]])) == ["i0", "i2"]
    assert ids(s.query(aid, "items", [["tags", "array-contains", "c"]])) == ["i3"]
    assert ids(s.query(aid, "items", [], ["n", "desc"], 2)) == ["i3", "i0"]
    # missing field sorts last in both directions
    assert ids(s.query(aid, "items", [], ["n", "asc"]))[-1] == "nofield"
    assert ids(s.query(aid, "items", [], ["n", "desc"]))[-1] == "nofield"
    # default order = doc id asc
    assert ids(s.query(aid, "items")) == sorted(ids(s.query(aid, "items")))


def test_query_limits(s, aid):
    assert code(s.query, aid, "c", [["a", "==", 1]] * 11) == "invalid_argument"
    assert code(s.query, aid, "c", [["a", "in", list(range(31))]]) == "invalid_argument"
    assert code(s.query, aid, "c", [["a", "~", 1]]) == "invalid_argument"
    assert code(s.query, aid, "c", [], None, 1001) == "invalid_argument"
    assert code(s.query, aid, "c", [], ["a", "sideways"]) == "invalid_argument"


def test_collections_are_scoped(s, aid):
    s.set_doc(aid, "a/x", {"v": 1})
    s.set_doc(aid, "a/x/sub/y", {"v": 2})
    assert [d["id"] for d in s.query(aid, "a")] == ["x"]
    assert [d["id"] for d in s.query(aid, "a/x/sub")] == ["y"]


def test_artifacts_isolated(s):
    a = s.create_artifact("a", "x")["id"]
    b = s.create_artifact("b", "x")["id"]
    s.set_doc(a, "c/d", {"v": 1})
    assert not s.get_doc(b, "c/d")["exists"]


def test_acquire_lease(s, aid):
    r = s.acquire(aid, "locks/x", "me", 1000, {"owner": "me"})
    assert r["acquired"] and r["holder"] == "me" and r["version"] == 1
    assert s.acquire(aid, "locks/x", "other", 1000) == {"acquired": False, "expiresAt": r["expiresAt"]}
    assert s.acquire(aid, "locks/x", "me", 1000)["acquired"]  # renewal by same holder
    assert s.get_doc(aid, "locks/x")["data"] == {"owner": "me"}


def test_acquire_expiry(s, aid, monkeypatch):
    import hermes_live_artifacts.server.store as st

    t = [1000.0]
    monkeypatch.setattr(st.time, "time", lambda: t[0])
    assert s.acquire(aid, "l/x", "a", 1000)["acquired"]
    assert not s.acquire(aid, "l/x", "b", 1000)["acquired"]
    t[0] += 2
    assert s.acquire(aid, "l/x", "b", 1000)["acquired"]


def test_submit_convention_sets_status(s, aid):
    assert s.get_artifact(aid)["status"] == "active"
    s.set_doc(aid, "meta/status", {"status": "submitted"})
    a = s.get_artifact(aid)
    assert a["status"] == "submitted" and a["submitted_at"]


def test_update_artifact_in_place_keeps_data(s, aid):
    s.set_doc(aid, "c/d", {"v": 1})
    s.update_artifact(aid, html="<p>v2</p>", title="new")
    a = s.get_artifact(aid)
    assert a["html"] == "<p>v2</p>" and a["title"] == "new"
    assert s.get_doc(aid, "c/d")["data"] == {"v": 1}


def test_archive_revokes_and_blocks_writes(s, aid):
    tok = s.get_artifact(aid)["token"]
    s.archive(aid)
    a = s.get_artifact(aid)
    assert a["status"] == "archived" and a["token"] != tok
    assert code(s.set_doc, aid, "c/d", {"v": 1}) == "revoked"
    assert s.dump_docs(aid) == {}


def test_archive_due_respects_pin_and_submit(s):
    day = 86400
    old = s.create_artifact("old", "x")["id"]
    pinned = s.create_artifact("pin", "x", pinned=True)["id"]
    fresh = s.create_artifact("fresh", "x")["id"]
    import time

    now = time.time() + 15 * day
    s.set_doc(fresh, "meta/status", {"status": "submitted"})  # submitted ~now(real) => not due at +15d? it IS 15d old
    due = s.archive_due(14, now=now)
    assert set(due) == {old, fresh}
    assert s.get_artifact(pinned)["status"] != "archived"


def test_rotate_token(s, aid):
    t = s.get_artifact(aid)["token"]
    assert s.rotate_token(aid) != t


def test_token_at_rest_is_not_plaintext(s, aid):
    import sqlite3

    c = sqlite3.connect(s.path)
    raw_db_token = c.execute("SELECT token FROM artifacts WHERE id=?", (aid,)).fetchone()[0]
    c.close()
    derived = s.get_artifact(aid)["token"]
    assert raw_db_token != derived
    assert s.derive_token(aid, raw_db_token) == derived


def test_delete_removes_docs(s, aid):
    s.set_doc(aid, "c/d", {"v": 1})
    s.delete(aid)
    assert s.get_artifact(aid) is None and s.dump_docs(aid) == {}


def test_change_seq_bumps(s, aid):
    a = s.change_seq()
    s.set_doc(aid, "c/d", {"v": 1})
    assert s.change_seq() > a


def test_seed_on_create(s):
    a = s.create_artifact("t", "x", seed={"items/a": {"v": 1}})
    assert s.get_doc(a["id"], "items/a")["data"] == {"v": 1}


def test_concurrent_writers_two_connections(s, aid):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(8) as ex:
        list(ex.map(lambda i: s.set_doc(aid, f"c/d{i}", {"i": i}), range(40)))
    assert len(s.query(aid, "c")) == 40
