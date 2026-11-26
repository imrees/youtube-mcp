"""Distributed documentation stays in sync with the code."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_tool_reference_is_up_to_date():
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "generate_tool_reference.py"), "--check"],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


def test_versions_are_consistent():
    import tomllib

    from youtube_mcp import __version__

    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert version == __version__
    assert f"## [{version}]" in (ROOT / "CHANGELOG.md").read_text()
    assert f"v{version}" in (ROOT / "README.md").read_text()
