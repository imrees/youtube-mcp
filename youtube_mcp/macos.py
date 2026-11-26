"""Best-effort macOS integrations (clipboard, notifications). No-ops on other platforms."""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys

log = logging.getLogger(__name__)


def copy_to_clipboard(text: str) -> bool:
    if sys.platform != "darwin" or not shutil.which("pbcopy"):
        return False
    try:
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True, timeout=5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("Clipboard copy failed: %s", exc)
        return False


def notify(title: str, message: str) -> bool:
    if sys.platform != "darwin" or not shutil.which("osascript"):
        return False
    # Text is passed as argv (not interpolated into the script) to avoid AppleScript injection.
    script = ["-e", "on run argv", "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
              "-e", "end run"]
    try:
        subprocess.run(["osascript", *script, title, message], check=True, timeout=5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("Notification failed: %s", exc)
        return False
