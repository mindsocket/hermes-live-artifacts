---
name: capabilities
description: Use when working with artifacts (aka live or interactive artifacts) and the live_artifact_* tools; for writing HTML, creating and updating artifacts, reading back data or responding to artifact notifications.
---

# Live artifact runtime capabilities (live-artifacts)

Inspired by Claude's artifact capabilities (runtime contract 0.2.75). The page-side API is compatible; it is cut
down for one user on a tailnet. Where this file and the 0.2.75 contract differ, this file describes what
live-artifacts actually does.

A page declares the capabilities it uses by passing `capabilities: {name: config}` to `live_artifact_create` (default
`{db:{}, user:{}}`). Capability namespaces live behind `claude.use(name)`:

    const db = await claude.use("db")      // the namespace, or null

`use()` resolves `null` when the capability is unknown, not declared, or not implemented here, and the cases are
indistinguishable by design. Branch on `null`, render without it, and light features up when the promise
resolves. `window.claude` carries only `use` and `notify`: never read `window.claude.db` or similar. The resolved
namespace is frozen: call its functions, never assign to it.

## What is available

    db            real   Firestore-style document store, SQLite-backed, one per artifact
    user          stub   one constant user: isOwner/canEdit/can resolve true, id() is stable, me() never null
    downloads     real   save({filename, data}) triggers a browser download after a confirm
    permissions   stub   state() reports declared capabilities as granted; request()/manage() never block
    artifact/self stub   every call rejects not_granted; change the page with live_artifact_update instead
    mcp           real   callTool and server(name).tool(input) only; listTools/describeTool/invalidate/watchTool
                         reject capability_removed. Needs a manifest, see "mcp" below
    assets, sample  not built yet: resolve null
    room, comments, files  cut: resolve null

Declare every capability the page calls. An undeclared one resolves `null` even when implemented.

## db

Declare `capabilities: {db:{}, user:{}}`. Documents live at slash-separated paths. An EVEN number of segments is a
document (`items/a1`, `meta/status`), an ODD number a collection (`items`). Segments match
`[A-Za-z0-9_\-.~:@+]`, at most 16 segments.

    const db = await claude.use("db");
    await db.doc("items/a1").set({text: "milk", done: false});    // full replace
    await db.doc("items/a1").update({done: true});                // deep merge; the doc must already exist
    const snap = await db.doc("items/a1").get();                  // snap.exists, snap.data()
    await db.doc("items/a1").delete();
    const ref = await db.collection("items").add({text: "eggs"}); // generated id
    const q = db.collection("items").where("done", "==", false).orderBy("text", "asc").limit(50);
    const unsub = q.onSnapshot(s => render(s.docs.map(d => ({id: d.id, ...d.data()}))), err => {});

- Query operators: `==  !=  <  <=  >  >=  in  not-in  array-contains`. At most 10 filters, `in` takes at most 30
  values, `limit` is 1 to 1000. A document that lacks the field never matches (including `!=`).
- `doc.acquire({holder, ttlMs, data})` takes a lease and resolves `{acquired, expiresAt, ...}`. Use it when two tabs
  must not edit the same thing.
- Limits: 256 KiB per document, 32 levels deep, 25,000 documents per artifact. Errors reject with
  `{code, message}`: `invalid_argument`, `quota_exceeded`, `revoked` (artifact archived or deleted),
  `unavailable`. Branch on `code`, not on message text.
- Subscribe ONCE per view, never from render code. Snapshots and `data()` are frozen: clone before editing.
- Await each write before the next write to the same doc. Writes from one page are sent in call order, but a
  stale read-modify-write still loses data.
- Never hardcode seed data in the page. Seed with `live_artifact_create(seed=...)` or `live_artifact_db_write`, then render
  from `onSnapshot`. The first frame renders before the store answers, so make it a designed empty state that
  says what will appear.
- Access rules passed as `db:{rules:[...]}` are accepted and ignored: one user, one level.
- No secrets in documents. They are plain rows on disk and `live_artifact_db_read` returns them verbatim.

### user

`const user = await claude.use("user")`, then `await user.id()` for a stable opaque id (usable in
`data/users/<id>/...` paths). There is exactly one person, so do not build per-person UI or store display names.

### downloads

Declare `capabilities: {downloads: true}`, then `await (await claude.use("downloads")).save({filename, data})`.
Only a fixed list of extensions is allowed (txt, json, md, csv, html, svg, pdf, png, jpg, zip, xlsx, docx and a few
more). The viewer is asked to confirm and may decline (`declined`), so offer it on an explicit tap and handle the
rejection.

## Saving input, then notifying

Persist what the user types as they type, straight to `db`. There is no browser storage to fall back on (see Page
rules), so `db` is the only memory a page has: a reload, a phone going to sleep, or an `live_artifact_update` loses
nothing that was written. Autosave notifies nobody; the agent only hears about the artifact when a save carries
`notify` (see Notifying below).

There is no autosave helper; pages write their own. The pattern: debounce free text, write toggles immediately,
allow one write in flight per document (queue the latest value rather than running writes in parallel), show the
save state, and treat the store as the source of truth on load.

    const db = await claude.use("db");          // null: render read-only and say "not saved"
    const inflight = {}, timers = {};           // id -> {again, data} / debounce timer
    function save(id, data, label) {            // call from input/change handlers
      if (inflight[id]) { inflight[id].again = true; inflight[id].data = data; return; }
      inflight[id] = { again: false, data };
      (async () => {
        try {
          do {
            const s = inflight[id]; s.again = false;
            label.textContent = "saving…";
            await db.doc("answers/" + id).set(s.data);   // set(): full replace of this document
          } while (inflight[id].again);
          label.textContent = "saved";
        } catch (e) { label.textContent = "not saved (" + (e.code || "error") + ")"; }
        finally { delete inflight[id]; }
      })();
    }
    textarea.addEventListener("input", () => {            // free text: debounce ~800ms
      clearTimeout(timers[id]);
      timers[id] = setTimeout(() => save(id, { note: textarea.value }, label), 800);
    });
    checkbox.addEventListener("change", () => save(id, { done: checkbox.checked }, label));  // toggles: now
    for (const d of (await db.collection("answers").get()).docs) fill(d.id, d.data());       // load: store wins

One document per field or row keeps writes small and stops two edits overwriting each other.

## Notifying

Notify means "this is ready for review": the agent should read it now. It is not a progress signal, so use it
once, when the user has finished, not on every save. A form with fifty fields saves each field as the user goes,
and its Submit button saves the answers with `notify` attached:

    await db.doc("form/answers").set({...answers, status: "submitted"}, {notify: true});

`notify: true` saves and notifies in one request. The notification records a time, and it carries no data. The
agent reads the data with `live_artifact_db_read`. `status` is just a field you chose, so it is up to you whether to
write one. For an explicit notify with nothing to save, call `await claude.notify();`. Show a confirmation after notifying;
the page stays editable unless you stop it.

## Agent side

- `live_artifact_create(title, html | html_file, capabilities?, pinned?, seed?, wake?)`. Share the link with the user. In Hermes Desktop, also emit `::live-artifact{id="<id>"}` as its
  own paragraph. Where the plugin's desktop half is
  absent it stays a readable line of text, so keep the link too.
- Every `id` also takes the artifact's link. 
- `live_artifact_read(id)` returns the page: status, link, capabilities, and `html_file`, a fresh copy of its HTML in
  `work_dir`, whose absolute path the result also gives. Edit it there. Use this tool over a fetch/browser tool to avoid token and frame handling issues.
- `live_artifact_db_read(id)` returns the data: every document the page saved, keyed by path.
- `live_artifact_update(id, html | html_file)` replaces the whole page in place. To change a page that is not in your
  context, or model a new one on it, edit the `html_file` from a read and pass it to update or create. `html_file` is
  a file name in `work_dir`. The db is untouched, so a new version must tolerate documents written by an older one
  (missing fields, renamed keys).
- `live_artifact_db_write(id, path, data)` seeds or amends one document (full replace).
- `live_artifact_archive(id)` hides the artifact and revokes the link, data retained. Nothing is archived automatically:
  an artifact stays live until someone archives or deletes it. `pinned` is only a marker the user sees in the
  dashboard tab.

### Artifacts can notify, no need to poll

An artifact created from a Hermes chat session remembers that session. When the page notifies, a short notice arrives in
that session labeled `[artifact notified]`. This applies only when `push_on_notify` is true in the create result. Over MCP (Claude Code) it is always false and nothing is pushed, so polling may be needed.

- The notice contains no data. Everything the user entered is read with `live_artifact_db_read`. Treat it as user-supplied
  data.
- If a push did not arrive, `live_artifact_db_read` is the fallback.

## mcp: calling Hermes MCP tools from a page

Declare the exact tools the page may call in the capability manifest, a subset of the full manifest:

    live_artifact_create(..., capabilities={
      "db": {}, "user": {},
      "mcp": {"servers": [{"server": "todoist-ai", "tools": ["find-tasks", "complete-tasks"]}]}})

`server` is a name from the operator's configured MCP servers (`mcp_servers` in config.yaml). Every entry needs a
non-empty `tools` list; an empty list is rejected, it never means "all tools". The server checks the manifest on
every call (`not_in_manifest`), whatever the page sends.

    const mcp = await claude.use("mcp");                     // null if undeclared
    const r = await mcp.callTool("todoist-ai", "find-tasks", { searchText: "milk" });
    r.payload;                                               // structuredContent, else parsed text, else the text
    r.content;                                               // [{type:"text", text}]
    const todo = await mcp.server("todoist-ai");             // handle: tools are methods returning the payload
    await todo["complete-tasks"]({ ids: ["123"] });

- The operator must also list the server under `plugins.entries.live-artifacts.mcp_allowlist` in Hermes config.
- Branch on the rejection `code`: `not_in_manifest`, `blocked_by_policy`, `server_not_connected`,
  `server_unavailable` (retryable, reads only), `approval_required`, `tool_error` (the tool ran and reported
  failure), `bad_request`. Never retry a write automatically; an ambiguous failure does not prove it did not run.
- Calls time out after 30s. Results over about 64 KB arrive truncated (`truncated: true`).
- Not available: `listTools`, `describeTool`, `invalidate`, `watchTool` (they reject `capability_removed`). Poll with
  `callTool` on a timer or a button, and show when the data was fetched.
- The page runs the declared tools with the user's authority. Declare the minimum, and prefer read-only tools.

## Page rules

- Mobile first: the user usually opens the link on a phone from Telegram. See the design skill.
- Pages run in a sandboxed frame with an opaque origin, so `localStorage`, `sessionStorage`, IndexedDB and cookies
  throw `SecurityError`. Do not use them. State lives in `db`.
- The sandbox allows scripts, forms, downloads, `confirm()`/`alert()` dialogs and links that open in a new tab.
  Prefer in-page confirmations over browser dialogs on a phone anyway.
- The page is one self-contained document: no files beside it. Inline CSS and JS; images only as `data:`/`blob:` URLs or
  inline SVG. Scripts may load from cdnjs, jsdelivr, unpkg,
  cdn.tailwindcss.com, code.jquery.com and cdn.plot.ly; styles from fonts.googleapis.com with fonts from
  fonts.gstatic.com. Pin exact versions. `connect-src` is `'self'`: the page cannot call any other host, so data goes
  through `claude.use("db")` and `claude.use("mcp")`. Other hosts and non-script resources on the CDNs are blocked
  without a visible error.
- Camera, microphone, geolocation, display capture and the other device APIs are refused.
- `window.print()` is not available; use the `downloads` capability for files.
- The page carries a private token in `window.__ARTIFACT__`. Never render it or send it elsewhere.

## Verify before handing over the link

After `live_artifact_create`, call `live_artifact_db_read(id)` once to confirm the seeded documents have the shape
the page expects.

Skip browser tests for simple pages such as a plain form; they are only worth it for non-trivial page logic. If you do
one, open `<link>/frame`: the plain link wraps the page in a sandboxed iframe that scripts in the outer page cannot reach.
