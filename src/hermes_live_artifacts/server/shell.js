/* Shell-side half of external link opening. The artifact frame (opaque origin, no popup flags) cannot
 * open tabs itself; its shim postMessages the wanted URL here, and the shell — a document we own with a
 * full-origin capability — window.open()s it in a real tab after a scheme check. Only http(s) passes;
 * anything else is ignored (the shim already showed a warning in the page). */
(() => {
  const OPENABLE = /^https?:\/\//i;
  window.addEventListener("message", (e) => {
    // The frame is same-host ('self' per the shell's frame-src), so the origin check is redundant but
    // cheap; the message shape is the real gate.
    const d = e.data || {};
    if (d.type !== "open-link") return;
    const url = String(d.url || "");
    if (OPENABLE.test(url)) window.open(url, "_blank", "noopener");
  });
})();
