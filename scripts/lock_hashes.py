"""Regenerate requirements.lock.txt with SHA-256 hashes for every pinned distribution.

    .venv/bin/python scripts/lock_hashes.py            # rewrite requirements.lock.txt
    .venv/bin/python scripts/lock_hashes.py --check    # report packages without a wheel for macOS/Linux

Reads the pins (name==version) from requirements.lock.txt and asks PyPI's JSON API for the SHA-256 of
every *wheel* of that exact version. The installer uses `pip --require-hashes --only-binary=:all:`,
so only these exact files can be installed and no package build scripts are run.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "requirements.lock.txt"
PIN = re.compile(r"^([A-Za-z0-9_.\-]+)==([A-Za-z0-9_.\-+]+)")


def pins() -> list[tuple[str, str]]:
    out = []
    for line in LOCK.read_text().splitlines():
        m = PIN.match(line.strip())
        if m:
            out.append((m.group(1), m.group(2)))
    return out


def wheel_hashes(name: str, version: str) -> list[tuple[str, str]]:
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 - fixed https host
        data = json.load(resp)
    return [(f["filename"], f["digests"]["sha256"]) for f in data["urls"] if f["packagetype"] == "bdist_wheel"]


def main() -> int:
    lines = ["# Exact, hash-locked dependency set used by install.sh / install.py.",
             "# Regenerate with: .venv/bin/python scripts/lock_hashes.py",
             "# Installed with: pip install --require-hashes --only-binary=:all: -r requirements.lock.txt"]
    problems = []
    for name, version in pins():
        files = wheel_hashes(name, version)
        if not files:
            problems.append(f"{name}=={version}: no wheels on PyPI")
        mac = [f for f, _ in files if "macosx" in f or "-none-any" in f]
        if not mac:
            problems.append(f"{name}=={version}: no macOS wheel")
        lines.append(f"{name}=={version} \\")
        hashes = sorted({h for _, h in files})
        for i, h in enumerate(hashes):
            lines.append(f"    --hash=sha256:{h}" + (" \\" if i < len(hashes) - 1 else ""))
    if "--check" in sys.argv:
        print("\n".join(problems) or "every pin has a macOS (or universal) wheel")
        return 1 if problems else 0
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    LOCK.write_text("\n".join(lines) + "\n")
    print(f"wrote {LOCK.name}: {len(pins())} pins")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
