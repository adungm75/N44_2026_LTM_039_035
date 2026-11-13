"""Run provenance helpers (dependency-free, safe to import from small processes)."""

from __future__ import annotations

import subprocess


def git_hash() -> str:
    """``HEAD`` commit (+ ``-dirty`` if the work tree has changes); ``unknown`` outside git."""
    try:
        rev = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, check=True
        ).stdout.strip()
        return rev + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
