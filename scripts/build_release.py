"""Build the credential-free source release archive.

    .venv/bin/python scripts/build_release.py OUTPUT_DIR [--name NAME]

Copies an explicit allowlist of files (never credentials, tokens, settings, history, logs, caches or
virtual environments), writes SHA256SUMS (checked by install.py), zips the folder and writes
<name>.zip.sha256. It refuses to continue if any excluded kind of file slipped into the staging folder.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from youtube_mcp import __version__  # noqa: E402

TOP_FILES = ["README.md", "CHANGELOG.md", "LICENSE", "pyproject.toml", "requirements.txt", "requirements-dev.txt",
             "requirements.lock.txt", ".gitignore", "youtube_mcp.env.example", "youtube_mcp_server.py",
             "authorize.py", "upload_and_share.py", "install.sh", "uninstall.sh", "install.py"]
DIRS = {"youtube_mcp": (".py",), "tests": (".py",), "scripts": (".py",), "docs": (".md",)}
FORBIDDEN_NAME_PARTS = ("client_secret", "token_youtube_mcp", "token.json")
FORBIDDEN_SUFFIXES = (".sqlite3", ".sqlite", ".db", ".log", ".lock", ".pyc", ".env", ".json")
EXECUTABLE = {"install.sh", "uninstall.sh", "install.py"}


def collect() -> list[Path]:
    files = [ROOT / f for f in TOP_FILES]
    for d, exts in DIRS.items():
        files += sorted(p for p in (ROOT / d).rglob("*") if p.is_file() and p.suffix in exts
                        and "__pycache__" not in p.parts)
    return files


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("output_dir", type=Path)
    ap.add_argument("--name", default=f"youtube-mcp-{__version__}-source")
    args = ap.parse_args()
    out = args.output_dir.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / args.name
        for f in collect():
            rel = f.relative_to(ROOT)
            if any(bad in rel.name for bad in FORBIDDEN_NAME_PARTS) or rel.suffix in FORBIDDEN_SUFFIXES:
                raise SystemExit(f"refusing to package {rel}")
            (stage / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, stage / rel)
            (stage / rel).chmod(0o755 if rel.name in EXECUTABLE else 0o644)
        rels = sorted(p.relative_to(stage).as_posix() for p in stage.rglob("*") if p.is_file())
        (stage / "SHA256SUMS").write_text("".join(f"{sha256(stage / r)}  {r}\n" for r in rels))
        zip_path = out / f"{args.name}.zip"
        tmp_zip = out / f".{args.name}.zip.tmp"
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(stage.rglob("*")):
                if p.is_file():
                    info = zipfile.ZipInfo(f"{args.name}/{p.relative_to(stage).as_posix()}", (2026, 1, 1, 0, 0, 0))
                    info.external_attr = (p.stat().st_mode & 0o777 | 0o100000) << 16
                    info.compress_type = zipfile.ZIP_DEFLATED
                    zf.writestr(info, p.read_bytes())
        tmp_zip.replace(zip_path)
    digest = sha256(zip_path)
    (out / f"{args.name}.zip.sha256").write_text(f"{digest}  {zip_path.name}\n")
    print(f"{zip_path}\n{digest}  ({len(rels) + 1} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
