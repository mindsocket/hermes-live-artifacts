/* Shell-side half of external link opening. The artifact frame (opaque origin, no popup flags) cannot
 * open tabs itself; its shim postMessages the wanted URL here, and the shell — a document we own with a
 * full-origin capability — shows it as a link the user taps to open in a real tab. http(s) passes
 * freely (the shim shows them as ordinary links); any other scheme passes only when the shim marked
 * it user-confirmed via the banner. Unknown schemes the browser itself cannot launch (obsidian:,
 * mailto: and friends are user agent dependent) simply do nothing when followed — the attempt is
 * harmless.
 *
 * We deliberately do not window.open() on message arrival: transient user activation does not cross
 * the postMessage hop (measured live — the call reported success twice while no tab appeared), so the
 * browser treats it as a non-gesture popup and blocks it. The user's tap on the bar is a genuine
 * gesture in this document and always opens. */
(() => {
  const OPENABLE = /^https?:\/\//i;
  function linkBar(url) {
    let bar = document.getElementById("__claude_open_fallback");
    if (!bar) {
      bar = document.createElement("div");
      bar.id = "__claude_open_fallback";
      bar.setAttribute(
        "style",
        "position:fixed;left:12px;right:12px;bottom:12px;z-index:2147483647;background:#1f1f1f;color:#fff;" +
          "font:13px/1.4 -apple-system,sans-serif;padding:10px 14px;border-radius:8px;box-shadow:0 4px 12px rgba(0,0,0,.3)"
      );
      document.body.appendChild(bar);
    }
    bar.textContent = "";
    const span = document.createElement("span");
    span.textContent = "Open link: ";
    const a = document.createElement("a");
    a.href = url;
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = url.length > 60 ? url.slice(0, 57) + "…" : url;
    a.setAttribute("style", "color:#8ab4f8;word-break:break-all");
    bar.append(span, a);
    setTimeout(() => bar.remove(), 15000);
  }
  window.addEventListener("message", (e) => {
    // The frame is same-host ('self' per the shell's frame-src), so the origin check is redundant but
    // cheap; the message shape is the real gate.
    const d = e.data || {};
    if (d.type !== "open-link") return;
    const url = String(d.url || "");
    if (!url) return;
    if (!OPENABLE.test(url) && d.confirmed !== true) return;
    linkBar(url);
  });
})();
