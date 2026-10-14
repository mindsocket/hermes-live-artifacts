"""Directory-plugin shim.

Hermes loads a directory plugin by importing `__init__.py` at the plugin root. The real code lives in the
`hermes_live_artifacts` package under `src/` (so it can also be pip-installed and loaded through the
`hermes_agent.plugins` entry point). This file only makes `src/` importable and re-exports `register`.
It uses absolute imports on purpose: it must also import cleanly outside a package (pytest collects it).
"""

import sys
from pathlib import Path

_SRC = str(Path(__file__).resolve().parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from hermes_live_artifacts import register  # noqa: E402

__all__ = ["register"]
