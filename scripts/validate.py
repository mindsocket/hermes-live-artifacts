#!/usr/bin/env python3
"""Validate the plugin manifest and structure using hermes CLI.

Copies tracked git files to a clean temporary directory before validating so that
untracked or gitignored files (such as tmp/ HAR captures or tokens) do not trigger
the secret scanner.
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    res = subprocess.run(["git", "ls-files", "-z"], capture_output=True, check=True)
    tracked_files = [f for f in res.stdout.split(b"\x00") if f]

    temp_dir = Path(tempfile.mkdtemp(prefix="hermes-live-artifacts-validate-"))
    try:
        for rel_bytes in tracked_files:
            rel_path = Path(rel_bytes.decode("utf-8"))
            dest = temp_dir / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(rel_path, dest)

        cmd = ["hermes", "plugins", "validate", str(temp_dir / "plugin")]
        print(f"Running: {' '.join(cmd)}")
        proc = subprocess.run(cmd)
        return proc.returncode
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
