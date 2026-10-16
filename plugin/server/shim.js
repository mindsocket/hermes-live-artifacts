/* live-artifacts runtime shim. Defines window.claude.use(name), source-compatible with the
 * Claude artifact runtime contract (0.2.75) for: db, user, downloads, permissions, artifact, self.
 * Everything else resolves null. Config is injected as window.__ARTIFACT__ = {id, caps}. */
(() => {
  const CFG = window.__ARTIFACT__;
  const BASE = `/api/${CFG.id}`;
  const err = (code, message) => Object.assign(new Error(message), { code, message });

  // Writes from this page are issued strictly in call order. The server handles requests on a thread
  // pool, so un-awaited back-to-back set()s could otherwise land out of order (last-writer-wins would
  // then pick an arbitrary writer). Reads/queries are not queued.
  let writeChain = Promise.resolve();
  const WRITE_OPS = new Set(["/db/set", "/db/update", "/db/delete", "/db/acquire", "/notify"]);
  function call(path, body) {
    if (!WRITE_OPS.has(path)) return rawCall(path, body);
    const p = writeChain.then(() => rawCall(path, body));
    writeChain = p.catch(() => {});
    return p;
  }

  async function rawCall(path, body) {
    let r;
    try {
      r = await fetch(BASE + path, {
        method: "POST",
        headers: { "content-type": "application/json", "x-artifact": CFG.token },
        body: JSON.stringify(body || {}),
      });
    } catch (e) { throw err("unavailable", String(e)); }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw err((j.error && j.error.code) || "unavailable", (j.error && j.error.message) || r.statusText);
    return j;
  }

  // ------------------------------------------------------------------ db
  const meta = (pending) => ({ fromCache: false, hasPendingWrites: !!pending });
  // `notify` is an option on a save, not a separate write: a save and its notification travel in one request.
  // Anything but a boolean is a mistake, so it throws rather than quietly saving without notifying.
  const notifyOpt = (opts) => {
    if (opts === undefined) return {};
    if (typeof opts !== "object" || opts === null || typeof opts.notify !== "boolean") {
      throw new TypeError("options must be { notify: true } or { notify: false }");
    }
    return { notify: opts.notify };
  };
  const docSnap = (d) => Object.freeze({
    id: d.id, exists: !!d.exists, data: () => (d.exists ? Object.freeze(d.data) : undefined), metadata: meta(false),
  });
  const querySnap = (docs, changes) => Object.freeze({
    docs, size: docs.length, empty: !docs.length, metadata: meta(false),
    docChanges: () => changes || docs.map((doc, i) => ({ type: "added", doc, oldIndex: -1, newIndex: i })),
  });
  // One SSE stream shared by every listener on the page; each change event triggers a refetch. While the
  // stream is down the browser keeps retrying, and a slow poll covers the gap.
  const listeners = new Set();
  const kick = () => listeners.forEach((l) => l.refresh());
  const POLL_WHILE_DOWN_MS = 30000;
  let es = null, pollTimer = null;
  function ensureStream() {
    if (es) return;
    es = new EventSource(`${BASE}/events?k=${encodeURIComponent(CFG.token)}`);
    es.addEventListener("change", kick);
    es.onopen = () => {
      clearInterval(pollTimer);
      pollTimer = null;
    };
    es.onerror = () => {
      console.warn("[artifact] event stream lost; polling until it reconnects");
      if (!pollTimer) pollTimer = setInterval(kick, POLL_WHILE_DOWN_MS);
    };
  }
  function listen(refresh) {
    const l = { refresh };
    listeners.add(l); ensureStream(); refresh();
    return () => listeners.delete(l);
  }

  function makeQuery(path, filters = [], orderBy = null, lim = null) {
    const q = {
      where: (f, op, v) => makeQuery(path, [...filters, [f, op, v]], orderBy, lim),
      orderBy: (f, dir = "asc") => makeQuery(path, filters, [f, dir], lim),
      limit: (n) => makeQuery(path, filters, orderBy, n),
      async get() {
        const j = await call("/db/query", { path, filters, orderBy, limit: lim });
        return querySnap(j.docs.map(docSnap));
      },
      onSnapshot(next, error) {
        let prev = null, dead = false, busy = false, again = false;
        const run = async () => {
          if (dead) return;
          if (busy) { again = true; return; }
          busy = true;
          try {
            const j = await call("/db/query", { path, filters, orderBy, limit: lim });
            if (dead) return;
            const docs = j.docs.map((d) => {
              const old = prev && prev.byId.get(d.id);
              return old && old.raw === JSON.stringify(d.data) ? old.snap : docSnap(d);
            });
            const byId = new Map(j.docs.map((d, i) => [d.id, { raw: JSON.stringify(d.data), snap: docs[i] }]));
            const changes = [];
            docs.forEach((doc, i) => {
              const old = prev && prev.byId.get(doc.id);
              if (!old) changes.push({ type: "added", doc, oldIndex: -1, newIndex: i });
              else if (old.snap !== doc) changes.push({ type: "modified", doc, oldIndex: prev.order.indexOf(doc.id), newIndex: i });
            });
            if (prev) prev.order.forEach((id, i) => { if (!byId.has(id)) changes.push({ type: "removed", doc: prev.byId.get(id).snap, oldIndex: i, newIndex: -1 }); });
            if (!prev || changes.length) {
              prev = { byId, order: docs.map((d) => d.id) };
              next(querySnap(docs, changes));
            }
          } catch (e) {
            // With an error callback the listener stops and reports; without one it logs and the next change still lands.
            if (error) { dead = true; error(e); } else console.error("[artifact] snapshot failed", e);
          }
          finally { busy = false; if (again) { again = false; run(); } }
        };
        const stop = listen(run);
        return () => { dead = true; stop(); };
      },
    };
    return q;
  }

  function docRef(path) {
    const ref = {
      id: path.split("/").pop(), path,
      async get() { const j = await call("/db/get", { path }); return docSnap(j.doc); },
      async set(data, opts) { await call("/db/set", { path, data, ...notifyOpt(opts) }); },
      async update(data, opts) { await call("/db/update", { path, data, ...notifyOpt(opts) }); },
      async delete() { await call("/db/delete", { path }); },
      async acquire(o) { return (await call("/db/acquire", { path, ...o })).result; },
      collection: (p) => collRef(`${path}/${p}`),
      onSnapshot(next, error) {
        let last = null, dead = false;
        const stop = listen(async () => {
          if (dead) return;
          try {
            const j = await call("/db/get", { path });
            const raw = JSON.stringify(j.doc);
            if (raw !== last) { last = raw; if (!dead) next(docSnap(j.doc)); }
          } catch (e) {
            if (error) { dead = true; error(e); } else console.error("[artifact] snapshot failed", e);
          }
        });
        return () => { dead = true; stop(); };
      },
    };
    return ref;
  }
  const genId = () => Array.from(crypto.getRandomValues(new Uint8Array(10)), (b) => b.toString(16).padStart(2, "0")).join("");
  function collRef(path) {
    return Object.assign(makeQuery(path), {
      path,
      doc: (id) => docRef(`${path}/${id || genId()}`),
      async add(data) { const r = docRef(`${path}/${genId()}`); await r.set(data); return r; },
    });
  }
  const db = Object.freeze({ doc: docRef, collection: collRef });

  // ------------------------------------------------------------------ user (single constant user)
  const USER_ID = "u_" + CFG.id;
  const me = { id: USER_ID, name: "Owner", avatarUrl: "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='32' height='32'><rect width='32' height='32' fill='%23666'/></svg>", color: "#4a6fa5", email: null, isOwner: true, canEdit: true };
  const prof = (id) => ({ id, name: id === USER_ID ? "Owner" : "", avatarUrl: me.avatarUrl, color: me.color, email: null, isMe: id === USER_ID, guest: false });
  const user = Object.freeze({
    isOwner: async () => true, canEdit: async () => true, can: async () => true,
    me: async () => ({ ...me }), id: async () => USER_ID, name: async () => "Owner", avatarUrl: async () => me.avatarUrl, email: async () => null,
    profiles: async (ids) => Object.fromEntries([].concat(ids).map((i) => [i, prof(i)])),
    search: async () => [prof(USER_ID)],
  });

  // ------------------------------------------------------------------ downloads
  const downloads = Object.freeze({
    async save({ filename, data }) {
      if (typeof filename !== "string" || !filename || filename.length > 512 || data == null || data === "") throw err("bad_request", "filename and non-empty data required");
      const ext = filename.split(".").pop().toLowerCase();
      const ok = "gif png jpg jpeg webp mp4 webm txt json md docx pptx epub csv ttf html svg pdf xlsx zip".split(" ");
      if (filename.indexOf(".") < 0 || !ok.includes(ext)) throw err("rejected_extension", "extension not allowed");
      const blob = data instanceof Blob ? data : new Blob([data]);
      if (!confirm(`Save ${filename} (${blob.size} bytes)?`)) throw err("declined", "viewer declined");
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob); a.download = filename.replace(/[\\/]/g, "_");
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 5000);
      return { status: "saved" };
    },
  });

  // ------------------------------------------------------------------ permissions / artifact / self stubs
  const granted = () => Object.fromEntries(CFG.caps.map((c) => [c, "granted"]));
  const permissions = Object.freeze({
    state: async (n) => (n === undefined ? granted() : (CFG.caps.includes(n) ? "granted" : "unavailable")),
    request: async () => granted(), manage: async () => {},
  });
  const stub = Object.freeze(new Proxy({}, { get: () => async () => { throw err("not_granted", "not available in live-artifacts"); } }));

  // ------------------------------------------------------------------ mcp (callTool + server(); no watch/list/describe)
  // The page's manifest rides in __ARTIFACT__.mcp; the server re-checks every call against the stored manifest.
  const removed = (name) => () => Promise.reject(err("capability_removed", `${name} is not implemented in live-artifacts`));
  const mcpCall = async (server, tool, input, opts) => {
    if (typeof server !== "string" || typeof tool !== "string") throw err("bad_request", "server and tool must be strings");
    if (opts && opts.signal && opts.signal.aborted) throw err("cancelled", "call aborted");
    const j = await rawCall("/mcp/call", { server, tool, input: input === undefined ? {} : input });
    return j.result;
  };
  const mcp = Object.freeze({
    callTool: mcpCall,
    server: async (name) => {
      const ent = (CFG.mcp || []).find((e) => e.server === name);
      if (!ent) throw err("server_not_connected", "no server by that name is declared in this artifact");
      return Object.freeze(Object.fromEntries(ent.tools.map((t) => [t, async (input, o) => (await mcpCall(name, t, input, o)).payload])));
    },
    listTools: removed("listTools"), describeTool: removed("describeTool"), invalidate: removed("invalidate"), watchTool: removed("watchTool"),
  });

  // ------------------------------------------------------- external link opening
  // Self-navigation destroys the page (security.md gap 6), so plain <a> links and window.open cannot
  // work unassisted. We intercept both in capture phase. The sandbox carries allow-popups (without
  // allow-popups-to-escape-sandbox): a popup opened from the user's click inherits the sandbox, so it
  // can do nothing the frame itself cannot, and the open happens inside the real gesture — activation
  // does not survive the alternative shell postMessage hop (measured live). Non-http(s) schemes are
  // refused with a visible notice; script-bearing schemes are refused outright.
  const OPENABLE = /^https?:\/\//i;
  // Script-bearing schemes never get an override: the open is handed to the browser scheme-blind, so
  // the only safe place to stop them is here.
  const NEVER_OPENABLE = /^(?:javascript|data|vbscript):/i;
  // Non-http(s) schemes (obsidian:, mailto:, …) are refused on first sight with an override banner;
  // an explicit "Open anyway" remembers scheme+host for the page's lifetime.
  const allowedSchemes = new Set();
  // The native open, captured before the override below: openLink must not call window.open, which this
  // shim replaces with a wrapper into openLink — calling it from inside openLink recurses until the
  // stack overflows, and every link dies with "Maximum call stack size exceeded" before any popup opens
  // (measured live, 2026-10-16).
  const nativeOpen = window.open.bind(window);
  function openLink(url, force) {
    if (typeof url !== "string" || !url) return false;
    if (NEVER_OPENABLE.test(url)) {
      console.warn("[artifact] refusing script-bearing URL:", url);
      notice(`${url.slice(0, 80)} uses a scheme this page is not allowed to open.`, false);
      return false;
    }
    if (OPENABLE.test(url)) {
      nativeOpen(url, "_blank", "noopener");
      return true;
    }
    let key;
    try {
      const u = new URL(url);
      key = `${(u.scheme || "").toLowerCase()}:${(u.hostname || "").toLowerCase()}`;
    } catch (_) { return false; }
    if (force || allowedSchemes.has(key)) {
      nativeOpen(url, "_blank", "noopener");
      return true;
    }
    warnRefused(url, key);
    return false;
  }
  function notice(text, showButton, key) {
    let note = document.getElementById("__claude_link_notice");
    if (!note) {
      note = document.createElement("div");
      note.id = "__claude_link_notice";
      note.setAttribute(
        "style",
        "position:fixed;left:12px;right:12px;bottom:12px;z-index:2147483647;background:#b3261e;color:#fff;" +
          "font:13px/1.4 -apple-system,sans-serif;padding:10px 14px;border-radius:8px;box-shadow:0 4px 12px rgba(0,0,0,.3)"
      );
      document.body.appendChild(note);
    }
    note.textContent = "";
    const span = document.createElement("span");
    span.textContent = text;
    note.append(span);
    if (showButton) {
      const btn = document.createElement("button");
      btn.textContent = "Open anyway";
      btn.setAttribute("style", "margin-left:10px;font:inherit;background:#fff;color:#b3261e;border:0;border-radius:6px;padding:4px 10px;cursor:pointer");
      btn.onclick = () => {
        allowedSchemes.add(key);
        note.remove();
        openLink(url, true);
      };
      note.append(btn);
    }
    setTimeout(() => note.remove(), 8000);
  }
  function warnRefused(url, key) {
    console.warn("[artifact] non-http(s) URL needs confirmation:", url);
    notice(`This page wants to open a non-web link: ${url}`, true, key);
  }
  document.addEventListener(
    "click",
    (e) => {
      // Modifier clicks too: route through openLink like a plain click so scheme guards apply.
      if (e.defaultPrevented) return;
      const a = e.target.closest && e.target.closest("a[href]");
      if (!a) return;
      const href = a.getAttribute("href");
      if (!href || href.startsWith("#") || a.hasAttribute("download")) return;
      let url;
      try { url = new URL(href, location.href).href; } catch (_) { return; }
      // No same-origin let-through: location.origin inside the sandboxed frame is the real host
      // origin (measured live), so a same-host link left to the browser would navigate (and
      // destroy) the artifact. Route every link through openLink.
      e.preventDefault();
      e.stopPropagation();
      openLink(url);
    },
    true
  );
  document.addEventListener(
    "auxclick",
    (e) => {
      // middle-click: same interception as plain click (the browser can't open the tab itself)
      if (e.defaultPrevented || e.button !== 1) return;
      const a = e.target.closest && e.target.closest("a[href]");
      if (!a) return;
      const href = a.getAttribute("href");
      if (!href || href.startsWith("#") || a.hasAttribute("download")) return;
      let url;
      try { url = new URL(href, location.href).href; } catch (_) { return; }
      e.preventDefault();
      e.stopPropagation();
      openLink(url);
    },
    true
  );
  window.open = (url) => (url === undefined || url === "" ? null : openLink(String(url)));

  const ns = { db, user, downloads, permissions, mcp, artifact: stub, self: stub };
  const cache = new Map();
  // Tells the agent the data is ready for review. Carries no payload: save the data first, then notify.
  const notify = async () => { await call("/notify", {}); };
  window.claude = Object.freeze({
    use(name) {
      if (cache.has(name)) return cache.get(name);
      const ok = Object.prototype.hasOwnProperty.call(ns, name) && CFG.caps.includes(name);
      const p = Promise.resolve(ok ? ns[name] : null);
      if (ok) cache.set(name, p);
      return p;
    },
    notify,
  });
})();
