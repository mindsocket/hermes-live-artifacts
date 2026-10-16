(function () {
  "use strict";
  // hermes-live-artifacts dashboard tab. Plain JS against the host's plugin SDK: there is no build step.
  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const { React, hooks, components: C, utils } = SDK;
  const { useState, useEffect, useCallback, useRef } = hooks;
  const { Badge, Button, ConfirmDialog, Toast } = C;
  const h = React.createElement;
  const API = "/api/plugins/hermes-live-artifacts";
  const POLL_MS = 2000;
  // The shell and the artifact inside it are separate origins from the dashboard. The sandbox is a second
  // fence on top (no allow-same-origin: the embedded page can never touch dashboard storage or cookies).
  // Nested frames inherit these restrictions, so these must cover everything the artifact's own sandbox allows.
  const FRAME_SANDBOX = "allow-scripts allow-forms allow-downloads allow-modals allow-popups";
  const FILTERS = ["all", "active", "archived"];

  function when(sec) {
    return sec ? utils.timeAgo(sec * 1000) : "—";
  }

  function statusVariant(status) {
    return status === "archived" ? "outline" : "default";
  }

  const CONFIRMS = {
    rotate: {
      title: "Rotate link?",
      description: "The old link, its page cookie and every open session of this artifact stop working. Open pages will fail with 403 until reloaded from the new link.",
      confirmLabel: "Rotate",
    },
    archive: {
      title: "Archive artifact?",
      description: "The page stops serving and its link is revoked. Data is kept.",
      confirmLabel: "Archive",
    },
    delete: {
      title: "Delete artifact permanently?",
      description: "The artifact and all of its documents are removed. This cannot be undone.",
      confirmLabel: "Delete",
      destructive: true,
    },
  };

  function Documents({ docs }) {
    const seen = useRef({});
    const flashed = {};
    for (const [path, data] of Object.entries(docs)) {
      const text = JSON.stringify(data, null, 2);
      if (path in seen.current && seen.current[path] !== text) flashed[path] = true;
      seen.current[path] = text;
    }
    const paths = Object.keys(docs).sort();
    if (!paths.length) return h("p", { className: "ala-empty" }, "No documents yet.");
    const byCollection = {};
    for (const p of paths) {
      const coll = p.includes("/") ? p.slice(0, p.lastIndexOf("/")) : "";
      (byCollection[coll] = byCollection[coll] || []).push(p);
    }
    return h(
      "div",
      { className: "ala-docs" },
      Object.entries(byCollection).map(([coll, list]) =>
        h(
          "section",
          { key: coll },
          h("h4", { className: "ala-coll" }, coll || "(root)"),
          list.map((p) =>
            h(
              "details",
              { key: p, className: utils.cn("ala-doc", flashed[p] && "ala-flash") },
              h("summary", null, p.slice(coll ? coll.length + 1 : 0)),
              h("pre", null, JSON.stringify(docs[p], null, 2)),
            ),
          ),
        ),
      ),
    );
  }

  // A plain click copies the link; a modifier click opens the page in a new tab instead.
  function LinkIcon({ link, onCopy }) {
    const modified = (e) => e.metaKey || e.ctrlKey || e.shiftKey || e.altKey;
    return h(
      "button",
      {
        type: "button",
        className: "ala-icon-btn",
        "aria-label": "Copy link",
        title: "Copy link (⌘/Ctrl/Shift-click opens it in a new tab)",
        onClick: (e) => (modified(e) ? window.open(link, "_blank", "noopener") : onCopy(link)),
      },
      h(
        "svg",
        { viewBox: "0 0 24 24", width: 16, height: 16, fill: "none", stroke: "currentColor", strokeWidth: 2,
          strokeLinecap: "round", strokeLinejoin: "round", "aria-hidden": "true" },
        h("path", { d: "M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" }),
        h("path", { d: "M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" }),
      ),
    );
  }

  function ActionMenu({ items }) {
    const [open, setOpen] = useState(false);
    const ref = useRef(null);
    useEffect(() => {
      if (!open) return;
      const onDown = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
      const onKey = (e) => { if (e.key === "Escape") setOpen(false); };
      document.addEventListener("pointerdown", onDown);
      document.addEventListener("keydown", onKey);
      return () => {
        document.removeEventListener("pointerdown", onDown);
        document.removeEventListener("keydown", onKey);
      };
    }, [open]);
    return h(
      "div",
      { className: "ala-menu", ref },
      h(
        "button",
        {
          type: "button",
          className: "ala-icon-btn",
          "aria-label": "Artifact actions",
          "aria-haspopup": "menu",
          "aria-expanded": open,
          onClick: () => setOpen(!open),
        },
        "⋯",
      ),
      open &&
        h(
          "div",
          { className: "ala-menu-panel", role: "menu" },
          items.map((it) =>
            h(
              "button",
              {
                key: it.label,
                type: "button",
                role: "menuitem",
                className: utils.cn("ala-menu-item", it.destructive && "ala-menu-danger"),
                onClick: () => { setOpen(false); it.onSelect(); },
              },
              it.label,
            ),
          ),
        ),
    );
  }

  // One artifact in the list. Selected, it expands into its metadata and controls.
  function ArtifactCard({ a, selected, detail, view, api }) {
    const archived = a.status === "archived";
    const expanded = selected && detail && detail.artifact.id === a.id ? detail : null;
    const items = [];
    if (expanded && expanded.link && view === "preview") items.push({ label: "Reload", onSelect: api.reload });
    if (expanded && !archived && expanded.rotation_available) {
      items.push({ label: "Rotate link", onSelect: () => api.act("rotate") });
    }
    if (expanded) items.push({ label: "Export JSON", onSelect: () => api.act("export") });
    if (expanded && !archived) items.push({ label: "Archive", onSelect: () => api.act("archive") });
    if (expanded) items.push({ label: "Delete", destructive: true, onSelect: () => api.act("delete") });

    return h(
      "div",
      { className: utils.cn("ala-card", selected && "ala-card-selected") },
      h(
        "button",
        { type: "button", className: "ala-row", "aria-expanded": selected, onClick: () => api.select(a.id) },
        h("div", { className: "ala-row-title" }, (a.pinned ? "📌 " : "") + a.title),
        h(
          "div",
          { className: "ala-row-meta" },
          h(Badge, { variant: statusVariant(a.status) }, a.status),
          h("span", null, a.doc_count + " doc" + (a.doc_count === 1 ? "" : "s")),
          h("span", null, when(a.updated_at)),
        ),
      ),
      selected &&
        h(
          "div",
          { className: "ala-expand" },
          expanded &&
            h(
              "div",
              { className: "ala-actions" },
              expanded.link && h(LinkIcon, { link: expanded.link, onCopy: api.copy }),
              !archived &&
                h(
                  "button",
                  {
                    type: "button",
                    className: "ala-toggle",
                    "aria-pressed": !!a.pinned,
                    onClick: () => api.act("pin"),
                  },
                  a.pinned ? "Pinned" : "Pin",
                ),
              h("span", { className: "ala-spacer" }),
              h(ActionMenu, { items }),
            ),
          expanded &&
            h(
              "dl",
              { className: "ala-meta" },
              h("dt", null, "id"), h("dd", null, a.id),
              h("dt", null, "created"), h("dd", null, when(a.created_at)),
              h("dt", null, "notified"), h("dd", null, when(a.notified_at)),
              h("dt", null, "session"), h("dd", null, a.origin_session || "none (CLI/cron)"),
            ),
          !archived &&
            h(
              "div",
              { className: "ala-seg", role: "group", "aria-label": "View" },
              h(
                "button",
                { type: "button", "aria-pressed": view === "preview", onClick: () => api.setView("preview") },
                "Preview",
              ),
              h(
                "button",
                { type: "button", "aria-pressed": view === "docs", onClick: () => api.setView("docs") },
                "Documents (" + a.doc_count + ")",
              ),
            ),
        ),
    );
  }

  function ArtifactsPage() {
    const [filter, setFilter] = useState("all");
    const [list, setList] = useState(null);
    const [selected, setSelected] = useState(null);
    const [detail, setDetail] = useState(null);
    const [view, setView] = useState("preview");
    const [nonce, setNonce] = useState(0);
    const [error, setError] = useState(null);
    const [confirm, setConfirm] = useState(null);
    const [busy, setBusy] = useState(false);
    const seq = useRef(null);
    const { showToast, toast } = hooks.useToast();

    const loadList = useCallback(async () => {
      const q = filter === "all" ? "" : "?status=" + filter;
      const res = await SDK.fetchJSON(API + "/artifacts" + q);
      setList(res.artifacts);
      seq.current = res.change_seq;
    }, [filter]);

    const loadDetail = useCallback(async (id) => {
      setDetail(await SDK.fetchJSON(API + "/artifacts/" + encodeURIComponent(id)));
    }, []);

    const refresh = useCallback(async () => {
      try {
        await loadList();
        if (selected) await loadDetail(selected);
        setError(null);
      } catch (e) {
        setError(String(e));
      }
    }, [loadList, loadDetail, selected]);

    useEffect(() => { refresh(); }, [refresh]);

    // The store bumps change_seq on every write, including the live artifact's own autosaves.
    useEffect(() => {
      const t = setInterval(async () => {
        try {
          const { change_seq } = await SDK.fetchJSON(API + "/seq");
          if (seq.current !== null && change_seq !== seq.current) await refresh();
        } catch (e) {
          setError(String(e));
        }
      }, POLL_MS);
      return () => clearInterval(t);
    }, [refresh]);

    const call = async (method, path, body) =>
      SDK.fetchJSON(API + path, {
        method,
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });

    const exportJson = async (a) => {
      const res = await SDK.authedFetch(API + "/artifacts/" + encodeURIComponent(a.id) + "/export");
      if (!res.ok) throw new Error(res.status + ": " + (await res.text()));
      const url = URL.createObjectURL(await res.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = "artifact-" + a.id + ".json";
      link.click();
      URL.revokeObjectURL(url);
    };

    // The selected artifact's detail, once loaded for this selection.
    const current = detail && selected && detail.artifact.id === selected ? detail : null;

    const api = {
      select: (id) => {
        if (id === selected) {
          setSelected(null);
          setDetail(null);
          return;
        }
        setView("preview");
        setSelected(id);
      },
      setView,
      reload: () => setNonce(nonce + 1),
      copy: async (link) => {
        try {
          if (!navigator.clipboard) throw new Error("clipboard unavailable (needs https or localhost)");
          await navigator.clipboard.writeText(link);
          showToast("Link copied", "success");
        } catch (e) {
          showToast(String(e), "error");
        }
      },
      act: async (kind) => {
        if (!current) return;
        const a = current.artifact;
        if (CONFIRMS[kind]) return setConfirm({ kind, a });
        try {
          if (kind === "pin") {
            await call("POST", "/artifacts/" + a.id + "/pin", { pinned: !a.pinned });
            await refresh();
          } else if (kind === "export") {
            await exportJson(a);
          }
        } catch (e) {
          showToast(String(e), "error");
        }
      },
    };

    const runConfirmed = async () => {
      const { kind, a } = confirm;
      setBusy(true);
      try {
        if (kind === "rotate") {
          await call("POST", "/artifacts/" + a.id + "/rotate");
          showToast("Link rotated", "success");
        } else if (kind === "archive") {
          await call("POST", "/artifacts/" + a.id + "/archive");
          showToast("Archived", "success");
        } else if (kind === "delete") {
          await call("DELETE", "/artifacts/" + a.id);
          setSelected(null);
          setDetail(null);
          showToast("Deleted", "success");
        }
        await refresh();
      } catch (e) {
        showToast(String(e), "error");
      } finally {
        setBusy(false);
        setConfirm(null);
      }
    };

    // The right panel shows content only: the live page, or its documents.
    let content;
    if (!selected) content = h("p", { className: "ala-empty" }, "Select an artifact.");
    else if (!current) content = h("p", { className: "ala-empty" }, "Loading…");
    else if (view === "preview" && current.link && current.artifact.status !== "archived") {
      content = h("iframe", {
        key: current.link + nonce,
        className: "ala-frame",
        src: current.link,
        sandbox: FRAME_SANDBOX,
        title: "Artifact preview",
      });
    } else content = h(Documents, { docs: current.docs });

    const cfg = confirm && CONFIRMS[confirm.kind];
    return h(
      "div",
      { className: "ala-page" },
      h(
        "div",
        { className: "ala-toolbar" },
        FILTERS.map((f) =>
          h(Button, { key: f, size: "sm", variant: f === filter ? "default" : "outline", onClick: () => setFilter(f) }, f),
        ),
      ),
      error && h("p", { className: "ala-error" }, error),
      h(
        "div",
        { className: "ala-layout" },
        h(
          "div",
          { className: "ala-list" },
          list === null
            ? h("p", { className: "ala-empty" }, "Loading…")
            : list.length === 0
              ? h("p", { className: "ala-empty" }, "No artifacts.")
              : list.map((a) =>
                  h(ArtifactCard, {
                    key: a.id,
                    a,
                    selected: a.id === selected,
                    detail: current,
                    view,
                    api,
                  }),
                ),
        ),
        h("div", { className: "ala-main" }, content),
      ),
      cfg &&
        h(ConfirmDialog, {
          open: true,
          title: cfg.title,
          description: cfg.description,
          confirmLabel: cfg.confirmLabel,
          destructive: !!cfg.destructive,
          loading: busy,
          onCancel: () => setConfirm(null),
          onConfirm: runConfirmed,
        }),
      toast && h(Toast, { toast }),
    );
  }

  window.__HERMES_PLUGINS__.register("hermes-live-artifacts", ArtifactsPage);
})();
