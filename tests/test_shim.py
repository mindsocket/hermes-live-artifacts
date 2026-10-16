"""Guards on the served shim.js source.

The shim cannot run under pytest, so these assertions pin the two structural invariants its link
handling depends on. They exist because the recursion they guard against was a live bug (2026-10-16):
openLink called window.open after the shim had replaced window.open with a wrapper into openLink,
so every external link and window.open died with RangeError before a popup opened.
"""

from pathlib import Path

SHIM = Path(__file__).parent.parent / "plugin" / "server" / "shim.js"


def source() -> str:
    return SHIM.read_text()


def test_openlink_uses_the_captured_native_open():
    s = source()
    assert "const nativeOpen = window.open.bind(window)" in s
    body = s[s.index("function openLink") : s.index("document.addEventListener")]
    assert "window.open(" not in body, "openLink must call nativeOpen: window.open is overridden below it"


def test_window_open_override_routes_into_openlink():
    s = source()
    line = next(ln for ln in s.splitlines() if "window.open = " in ln)
    assert "openLink" in line
