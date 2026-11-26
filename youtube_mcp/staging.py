"""Private, verified snapshots of approved upload files.

Closes the race between approval and upload: the preview binds the approval to the file's
SHA-256, but the upload reads the file later. At confirmation time the file is snapshotted into
a private staging directory (0700, file 0400) and the snapshot is hashed. The upload only starts
if that hash equals the approved hash, and it reads the snapshot, never the original. Later
changes to the original cannot change what is uploaded.

On APFS the snapshot is a copy-on-write clone (clonefile): instant and with no extra disk space.
Where cloning is impossible (other volume/filesystem) a full copy is made after a free-space
check. Snapshots are removed when the upload completes or fails; leftovers from crashed processes
are removed on the next start. The original file is only ever read.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import logging
import os
import shutil
import tempfile
from pathlib import Path

from .errors import YouTubeMCPError

log = logging.getLogger(__name__)
FREE_SPACE_MARGIN = 512 * 1024 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _clonefile(src: Path, dst: Path) -> bool:
    """APFS copy-on-write clone via clonefile(2). Returns False if unsupported."""
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "/usr/lib/libSystem.B.dylib", use_errno=True)
        fn = libc.clonefile
    except (OSError, AttributeError):
        return False
    fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
    fn.restype = ctypes.c_int
    CLONE_NOFOLLOW = 0x0001
    return fn(os.fsencode(src), os.fsencode(dst), CLONE_NOFOLLOW) == 0


class Staging:
    def __init__(self, root: Path, clone=_clonefile) -> None:
        self.root = root
        self._clone = clone
        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)

    def stage(self, src: Path, expected_sha256: str) -> Path:
        workdir = Path(tempfile.mkdtemp(prefix=f"up-{os.getpid()}-", dir=self.root))
        os.chmod(workdir, 0o700)
        dst = workdir / src.name
        try:
            method = "clone" if self._clone(src, dst) else "copy"
            if method == "copy":
                size = src.stat().st_size
                free = shutil.disk_usage(self.root).free
                if free < size + FREE_SPACE_MARGIN:
                    raise YouTubeMCPError(
                        "insufficient_disk_space",
                        f"Not enough free space to make a verified snapshot of the file ({size / 1e9:.1f} GB needed).",
                        action="Free up disk space, or move the video to the internal disk (APFS snapshots need no space).")
                shutil.copyfile(src, dst)
            os.chmod(dst, 0o400)
            actual = _sha256(dst)
            if actual != expected_sha256:
                raise YouTubeMCPError(
                    "file_changed_since_approval",
                    "The video file changed after it was approved, so it was not uploaded.",
                    action="Request a new preview of the current file and get the user's approval again.")
            log.info("Staged %s (%s) for upload", src.name, method)
            return dst
        except BaseException:
            self.discard(dst)
            raise

    def discard(self, staged: Path) -> None:
        workdir = staged.parent
        if workdir.parent.resolve() != self.root.resolve() or not workdir.name.startswith("up-"):
            return  # never delete anything outside our staging area
        try:
            os.chmod(staged, 0o600)
        except OSError:
            pass
        shutil.rmtree(workdir, ignore_errors=True)

    def cleanup_stale(self) -> int:
        """Remove snapshots left behind by processes that no longer run."""
        from .history import _pid_alive

        removed = 0
        for workdir in self.root.glob("up-*"):
            try:
                pid = int(workdir.name.split("-")[1])
            except (IndexError, ValueError):
                continue
            if pid != os.getpid() and not _pid_alive(pid):
                shutil.rmtree(workdir, ignore_errors=True)
                removed += 1
        return removed
