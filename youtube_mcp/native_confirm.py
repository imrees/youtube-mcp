"""Native macOS confirmation for actions that expose content publicly.

The chat-based preview + confirmation_token flow keeps the *model* from acting by mistake, but
the token passes through the model. For anything that makes content PUBLIC, the server also asks
the person directly with a native macOS dialog that the model cannot answer through MCP.

Design:
- Cancel is the default button (Return/Escape cancel); approving needs a deliberate click.
- Fails closed: if the dialog cannot be shown (not macOS, no GUI session, osascript missing),
  errors, is cancelled, or times out, the action is refused.
- There is no tool parameter, environment variable or setting that skips the dialog. Tests
  inject a fake confirmer through the Python constructor only.
- Text is passed to AppleScript as argv (never interpolated), so titles cannot inject script.

Limitation: this is an additional safeguard, not an absolute security boundary. Anything that can
control the Mac's screen and mouse (for example an agent with computer-use access, or malware
running as the user) could click "Allow". See README "Security model".
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from typing import Callable

from .errors import YouTubeMCPError

log = logging.getLogger(__name__)

ALLOW_BUTTON = "Allow Public"
CANCEL_BUTTON = "Cancel"
DEFAULT_TIMEOUT_SECONDS = 60
DIALOG_TITLE = "YouTubeMCP: public publishing"

# confirmer(action, details) -> None, raising YouTubeMCPError unless the person approved.
PublicConfirmer = Callable[[str, "list[tuple[str, str]]"], None]


def _timeout() -> int:
    try:
        from .config import setting
        value = int(setting("YOUTUBE_MCP_CONFIRM_TIMEOUT") or DEFAULT_TIMEOUT_SECONDS)
    except ValueError:
        value = DEFAULT_TIMEOUT_SECONDS
    return max(10, min(value, 300))  # bounded; cannot be used to disable the dialog


def build_message(action: str, details: list[tuple[str, str]]) -> str:
    lines = [action.upper(), ""]
    lines += [f"{label}: {value}" for label, value in details if value not in (None, "")]
    lines += ["", "This will make content visible to EVERYONE on YouTube.",
              f"Click “{ALLOW_BUTTON}” only if you asked for this."]
    return "\n".join(lines)


def denied(message: str, code: str = "native_confirmation_denied") -> YouTubeMCPError:
    return YouTubeMCPError(
        code, message,
        action=("Nothing was made public. If the user really wants this, request a fresh preview and approval; "
                "the user must click the macOS dialog themselves."))


def macos_confirm_public(action: str, details: list[tuple[str, str]]) -> None:
    """Show the dialog and return only if the person clicked Allow. Otherwise raise (fail closed)."""
    if sys.platform != "darwin" or not shutil.which("osascript"):
        raise denied("Public actions need a native macOS confirmation dialog, which is not available here.",
                     "native_confirmation_unavailable")
    timeout = _timeout()
    message = build_message(action, details)
    script = [
        "on run argv",
        "set r to display dialog (item 1 of argv) with title (item 2 of argv) "
        "buttons {(item 3 of argv), (item 4 of argv)} default button (item 3 of argv) "
        "cancel button (item 3 of argv) with icon caution giving up after (item 5 of argv as integer)",
        "if gave up of r then return \"TIMEOUT\"",
        "return button returned of r",
        "end run",
    ]
    args = ["osascript"]
    for line in script:
        args += ["-e", line]
    args += [message, DIALOG_TITLE, CANCEL_BUTTON, ALLOW_BUTTON, str(timeout)]
    log.info("Asking for native confirmation: %s", action)
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout + 15)
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("Native confirmation failed to run: %s", type(exc).__name__)
        raise denied("The macOS confirmation dialog could not be shown, so nothing was made public.",
                     "native_confirmation_unavailable") from None
    answer = (proc.stdout or "").strip()
    if proc.returncode == 0 and answer == ALLOW_BUTTON:
        log.info("Native confirmation: allowed")
        return
    if proc.returncode == 0 and answer == "TIMEOUT":
        log.info("Native confirmation: timed out")
        raise denied(f"The macOS confirmation dialog timed out after {timeout} seconds, so nothing was made public.",
                     "native_confirmation_timeout")
    if "-128" in (proc.stderr or ""):  # user cancelled
        log.info("Native confirmation: cancelled")
        raise denied("The user clicked Cancel in the macOS confirmation dialog. Nothing was made public.")
    log.warning("Native confirmation dialog error (exit %s)", proc.returncode)
    raise denied("The macOS confirmation dialog could not be completed, so nothing was made public.",
                 "native_confirmation_unavailable")
