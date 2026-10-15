(function () {
  "use strict";
  // hermes-live-artifacts dashboard tab. Plain JS against the host's plugin SDK: there is no build step.
  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const { React, hooks, components: C, utils } = SDK;
  const { useState, useEffect, useCallback, useRef } = hooks;
  const { Card, CardContent, Button, Badge, ConfirmDialog, Toast } = C;
  const h = React.createElement;
  const API = "/api/plugins/hermes-live-artifacts";
  const POLL_MS = 2000;
  // The shell and the artifact inside it are separate origins from the dashboard. The sandbox is a second
  // fence on top (no allow-same-origin: the embedded page can never touch dashboard storage or cookies).
  // Nested frames inherit these restrictions, so these must cover everything the artifact's own sandbox allows.
  const FRAME_SANDBOX = "allow-scripts allow-forms allow-downloads allow-modals allow-popups";
  const DEVICES = [
    { id: "phone", label: "Phone", width: "390px" },
    { id: "tablet", label: "Tablet", width: "768px" },
    { id: "desktop", label: "Desktop", width: "100%" },
  ];
  const FILTERS = ["all", "active", "archived"];

  function when(sec) {
    return sec ? utils.timeAgo(sec * 1000) : "—";
  }

  function statusVariant(status) {
    return status === "archived" ? "outline" : "default";
  }

  function ArtifactRow({ a, selected, onSelect }) {
    return h(
      "button",
      { className: utils.cn("ala-row", selected && "ala-row-selected"), onClick: () => onSelect(a.id) },
      h("div", { className: "ala-row-title" }, (a.pinned ? "📌 " : "") + a.title),
      h(
        "div",
        { className: "ala-row-meta" },
        h(Badge, { variant: statusVariant(a.status) }, a.status),
        h("span", null, a.doc_count + " doc" + (a.doc_count === 1 ? "" : "s")),
        a.notified_at && h("span", null, "notified " + when(a.notified_at)),
        h("span", null, when(a.updated_at)),
      ),
    );
  }

  function Preview({ link }) {
    const [device, setDevice] = useState("desktop");
    const [nonce, setNonce] = useState(0);
    const width = DEVICES.find((d) => d.id === device).width;
    return h(
      "div",
      { className: "ala-preview" },
      h(
        "div",
        { className: "ala-toolbar" },
        DEVICES.map((d) =>
          h(
            Button,
            { key: d.id, size: "sm", variant: d.id === device ? "default" : "outline", onClick: () => setDevice(d.id) },
            d.label,
          ),
        ),
        h(Button, { size: "sm", variant: "outline", onClick: () => setNonce(nonce + 1) }, "Reload"),
      ),
      h(
        "div",
        { className: "ala-stage" },
        h("iframe", {
          key: link + nonce,
          className: "ala-frame",
          style: { width },
          src: link,
          sandbox: FRAME_SANDBOX,
          title: "Artifact preview",
        }),
      ),
    );
  }

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

  function Detail({ detail, onAction }) {
    const [view, setView] = useState("preview");
    const a = detail.artifact;
    const archived = a.status === "archived";
    const act = (kind) => () => onAction(kind, a, detail);
    return h(
      Card,
      { className: "ala-detail" },
      h(
        CardContent,
        null,
        h(
          "div",
          { className: "ala-head" },
          h("h2", null, a.title),
          h(Badge, { variant: statusVariant(a.status) }, a.status),
        ),
        h(
          "dl",
          { className: "ala-meta" },
          h("dt", null, "id"), h("dd", null, a.id),
          h("dt", null, "created"), h("dd", null, when(a.created_at)),
          h("dt", null, "notified"), h("dd", null, when(a.notified_at)),
          h("dt", null, "session"), h("dd", null, a.origin_session || "none (CLI/cron)"),
        ),
        h(
          "div",
          { className: "ala-toolbar" },
          !archived && h(Button, { size: "sm", variant: "outline", onClick: act("copy") }, "Copy link"),
          !archived && h(Button, { size: "sm", variant: "outline", onClick: act("pin") }, a.pinned ? "Unpin" : "Pin"),
          !archived && detail.rotation_available &&
            h(Button, { size: "sm", variant: "outline", onClick: act("rotate") }, "Rotate link"),
          !archived && h(Button, { size: "sm", variant: "outline", onClick: act("archive") }, "Archive"),
          h(Button, { size: "sm", variant: "outline", onClick: act("export") }, "Export JSON"),
          h(Button, { size: "sm", variant: "destructive", onClick: act("delete") }, "Delete"),
        ),
        h(
          "div",
          { className: "ala-toolbar" },
          !archived && h(Button, { size: "sm", variant: view === "preview" ? "default" : "outline", onClick: () => setView("preview") }, "Preview"),
          h(Button, { size: "sm", variant: view === "docs" || archived ? "default" : "outline", onClick: () => setView("docs") }, "Documents (" + a.doc_count + ")"),
        ),
        view === "preview" && !archived && detail.link
          ? h(Preview, { link: detail.link })
          : h(Documents, { docs: detail.docs }),
      ),
    );
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

  function ArtifactsPage() {
    const [filter, setFilter] = useState("all");
    const [list, setList] = useState(null);
    const [selected, setSelected] = useState(null);
    const [detail, setDetail] = useState(null);
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

    const onAction = async (kind, a, d) => {
      if (CONFIRMS[kind]) return setConfirm({ kind, a });
      try {
        if (kind === "copy") {
          if (!navigator.clipboard) throw new Error("clipboard unavailable (needs https or localhost)");
          await navigator.clipboard.writeText(d.link);
          showToast("Link copied", "success");
        } else if (kind === "pin") {
          await call("POST", "/artifacts/" + a.id + "/pin", { pinned: !a.pinned });
          await refresh();
        } else if (kind === "export") {
          await exportJson(a);
        }
      } catch (e) {
        showToast(String(e), "error");
      }
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
                  h(ArtifactRow, { key: a.id, a, selected: a.id === selected, onSelect: setSelected }),
                ),
        ),
        h(
          "div",
          { className: "ala-main" },
          detail
            ? h(Detail, { key: detail.artifact.id, detail, onAction })
            : h("p", { className: "ala-empty" }, "Select an artifact."),
        ),
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
