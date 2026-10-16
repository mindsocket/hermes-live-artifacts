"""Operator view of the artifact store, for the Hermes dashboard tab (dashboard/plugin_api.py).

Pure functions over Store and tools, so they are testable without FastAPI. Everything returned is JSON-safe.
The page token never leaves except inside `link`, and only for the one artifact the operator opened.
"""

from __future__ import annotations

from . import tools
from .store import Store

SUMMARY_FIELDS = ("id", "title", "status", "pinned", "created_at", "updated_at", "notified_at", "origin_session")


def summary(a: dict, doc_count: int) -> dict:
    out = {k: a.get(k) for k in SUMMARY_FIELDS}
    out["doc_count"] = doc_count
    return out


def list_artifacts(st: Store, status: str | None = None) -> dict:
    counts = st.doc_counts()
    return {
        "artifacts": [summary(a, counts.get(a["id"], 0)) for a in st.list_artifacts(status)],
        "change_seq": st.change_seq(),
        "rotation_available": tools.link_secret_enabled(),
    }


def detail(st: Store, aid: str) -> dict:
    """One artifact with its documents. `link` is None once archived: the page no longer serves."""
    a = st.get_artifact(aid)
    if a is None:
        raise KeyError(aid)
    docs = st.dump_docs(aid)
    return {
        "artifact": summary(a, len(docs)),
        "link": None if a["status"] == "archived" else tools.link(a),
        "docs": docs,
        "change_seq": st.change_seq(),
        "rotation_available": tools.link_secret_enabled(),
    }


def set_pinned(st: Store, aid: str, pinned: bool) -> None:
    st.update_artifact(aid, pinned=pinned)


def rotate_link(st: Store, aid: str) -> str:
    """Rotate the artifact's token: existing links (?k=), cookies, API tokens and open event streams die.
    Returns the fresh link. In `never` mode the id in the URL is the credential, so rotating would not revoke
    the page link: refuse (ValueError) rather than imply a revocation that did not happen. KeyError: no such id."""
    if not tools.link_secret_enabled():
        raise ValueError("link rotation is meaningless with link_secret=never: archive the artifact to revoke it")
    st.rotate_token(aid)
    return tools.link(st.get_artifact(aid))


def export_one(st: Store, aid: str) -> dict:
    """The same shape `artifact_export` writes, for one artifact."""
    a = next((x for x in st.list_artifacts(full=True) if x["id"] == aid), None)
    if a is None:
        raise KeyError(aid)
    a["docs"] = st.dump_docs(aid)
    return a
