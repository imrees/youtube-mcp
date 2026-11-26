"""Local media metadata used as extra evidence when matching an upload to a YouTube video."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def local_duration_ms(path: Path) -> int | None:
    """Video duration from macOS Spotlight metadata (mdls), or None if unavailable. Read-only."""
    if sys.platform != "darwin" or not shutil.which("mdls"):
        return None
    try:
        out = subprocess.run(["mdls", "-raw", "-name", "kMDItemDurationSeconds", str(path)],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        return int(float(out) * 1000) if out and out != "(null)" else None
    except (subprocess.SubprocessError, OSError, ValueError):
        return None
