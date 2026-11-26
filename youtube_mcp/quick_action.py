"""Finder Quick Action "Upload & Share": upload selected videos as UNLISTED and copy the link.

This is a thin front end over the same service layer the MCP server uses (validation,
duplicate detection, upload history, resumable uploads with retry, visibility
verification, OAuth token). It does not talk to any AI model or MCP client, and it does
not need one to be running.

Invoking the Quick Action on a file is the user's explicit authorization to upload it,
so the CLI performs the service's preview/confirm handshake itself. Visibility is fixed
to UNLISTED in code; there is no option to upload publicly from here.

Usage:
    python upload_and_share.py upload FILE [FILE ...]   # what the Quick Action runs
    python upload_and_share.py install                  # install the Finder Quick Action
    python upload_and_share.py uninstall                # remove it
    python upload_and_share.py status                   # show install + authorization status
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import logging.handlers
import os
import plistlib
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator
from contextlib import contextmanager

from . import history as h
from .config import PROJECT_ROOT, Settings, cli_command
from .errors import RedactingFilter, RedactingFormatter, YouTubeMCPError, authorize_command, translate_exception
from .youtube import MAX_TITLE_CHARS, short_url, validate_video_file

log = logging.getLogger("youtube_mcp.quick_action")

VISIBILITY = "unlisted"  # fixed; never configurable from the Quick Action
MENU_TITLE = "Upload & Share"
NOTIFY_TITLE = "YouTube Upload & Share"
WORKFLOW_NAME = f"{MENU_TITLE}.workflow"
SERVICES_DIR = Path.home() / "Library" / "Services"
MARKER = "youtube_mcp-quick-action"  # identifies a bundle this installer created
PROGRESS_STEPS = (25, 50, 75)
MIN_SECONDS_BETWEEN_PROGRESS_NOTES = 8.0


# --- macOS helpers -------------------------------------------------------------------

def notify(title: str, message: str, subtitle: str = "", sound: str = "") -> bool:
    """macOS notification. Text is passed via argv, never interpolated into AppleScript."""
    if sys.platform != "darwin" or not shutil.which("osascript"):
        return False
    script = "display notification (item 2 of argv) with title (item 1 of argv) subtitle (item 3 of argv)"
    if sound:
        script += " sound name (item 4 of argv)"
    try:
        subprocess.run(["osascript", "-e", "on run argv", "-e", script, "-e", "end run",
                        title, message, subtitle, sound],
                       check=True, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("Notification failed: %s", exc)
        return False


def title_from_filename(path: Path) -> str:
    """Default video title: the file name without extension, made valid for YouTube."""
    title = " ".join(path.stem.replace("<", "").replace(">", "").split())
    if len(title) > MAX_TITLE_CHARS:
        title = title[:MAX_TITLE_CHARS - 1].rstrip() + "…"
    return title or "Untitled video"


# --- result model --------------------------------------------------------------------

@dataclass
class FileResult:
    file: str
    outcome: str  # uploaded | already_uploaded | failed | cancelled
    url: str | None = None
    video_id: str | None = None
    visibility: str | None = None
    message: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.outcome in ("uploaded", "already_uploaded")


class Cancelled(Exception):
    pass


# --- core ----------------------------------------------------------------------------

class QuickAction:
    def __init__(
        self,
        service: Any,
        *,
        check_auth: Callable[[], Any],
        notifier: Callable[..., bool] = notify,
        clipboard: Callable[[str], bool] | None = None,
        poll_interval: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        allow_duplicate: bool = False,
    ) -> None:
        self.service = service
        # Only set by an explicit `upload --allow-duplicate` from Terminal (never by the Finder Quick Action).
        self.allow_duplicate = allow_duplicate
        self.check_auth = check_auth
        self.notifier = notifier
        self.clipboard = clipboard or service.clipboard
        self.poll_interval = poll_interval
        self.sleep = sleep
        self.clock = clock
        self.current_upload_id: str | None = None

    # notifications ----------------------------------------------------------------

    def _note(self, message: str, subtitle: str = "", sound: str = "") -> None:
        log.info("notify: %s | %s", subtitle, message)
        self.notifier(NOTIFY_TITLE, message, subtitle, sound)

    def _fail(self, file: str, title: str, err: YouTubeMCPError) -> FileResult:
        message = err.message
        if err.code in ("auth_expired", "not_authorized", "insufficient_scope", "token_invalid"):
            hint = "YouTube authorization needs renewing. Run in Terminal: " + authorize_command()
        elif err.code in ("upload_outcome_uncertain", "ambiguous_upload"):
            hint = ("Check YouTube Studio (Content) first. Only if it is not there, upload it again by running in "
                    "Terminal: " + upload_again_command(file))
        elif err.retryable or err.code == "network_error":
            hint = "Check your internet connection, then run Upload & Share again."
        else:
            hint = err.action or ""
        log.warning("Failed %s: [%s] %s | %s", file, err.code, message, hint)
        self._note(f"{message} {hint}".strip(), subtitle=f"Failed: {title}", sound="Basso")
        return FileResult(file=file, outcome="failed", message=f"[{err.code}] {message} {hint}".strip())

    # main entry -------------------------------------------------------------------

    def process(self, file_arg: str) -> FileResult:
        display = Path(file_arg).name or file_arg
        try:
            path = validate_video_file(file_arg)
        except YouTubeMCPError as err:
            return self._fail(file_arg, display, err)
        title = title_from_filename(path)
        try:
            return self._process(path, title)
        except Cancelled:
            raise
        except Exception as exc:  # never let an exception escape without telling the user
            err = translate_exception(exc)
            if err.code == "internal_error":
                log.exception("Unexpected error for %s", path)
            return self._fail(str(path), title, err)

    def _process(self, path: Path, title: str) -> FileResult:
        svc = self.service
        try:
            preview = svc.upload_video(str(path), title=title, visibility=VISIBILITY, notify=False, copy_link=False,
                                       allow_duplicate=self.allow_duplicate)
        except YouTubeMCPError as err:
            if err.code == "duplicate_upload":
                previous = svc.history.get_upload(err.details.get("upload_id", "")) or {}
                return self._existing(path, previous.get("title") or title, err.details["video_id"], err.message)
            if err.code == "upload_in_progress":
                self._note("This file is already being uploaded. You'll get a notification when it finishes.",
                           subtitle=title)
                return FileResult(file=str(path), outcome="failed", message=err.message)
            raise

        # Refresh/validate the OAuth token before creating a history record.
        self.check_auth()

        # The user's explicit Quick Action invocation is the upload approval.
        size_mb = preview["preview"]["file_size_mb"]
        result = svc.upload_video(str(path), title=title, visibility=VISIBILITY, notify=False, copy_link=False,
                                  allow_duplicate=self.allow_duplicate,
                                  confirmation_token=preview["confirmation_token"], background=True)
        upload_id = result["upload_id"]
        self.current_upload_id = upload_id
        log.info("Upload %s started: %s (%.1f MB)", upload_id, path, size_mb)
        self._note(f"Uploading as Unlisted ({size_mb:.1f} MB)…", subtitle=f"Started: {title}")

        snap = self._wait(upload_id, title)
        self.current_upload_id = None
        if snap["status"] != h.COMPLETED:
            error = snap.get("error") or {}
            err = YouTubeMCPError(error.get("code", "upload_failed"), error.get("message", "The upload failed."),
                                  action=error.get("action"), retryable=bool(error.get("retryable")))
            return self._fail(str(path), title, err)

        video_id = snap["video_id"]
        actual = snap.get("actual_visibility")
        warnings = [w.get("message", "") for w in snap.get("warnings") or []]
        if actual is None:  # verification inside the upload failed; try once more
            actual = self._verified_visibility(video_id)
        return self._finish(path, title, video_id, actual, "uploaded", warnings)

    def _wait(self, upload_id: str, title: str) -> dict[str, Any]:
        uploads = self.service.uploads
        reported = 0
        last_note = self.clock()
        while True:
            snap = uploads.snapshot(upload_id)
            if snap["status"] not in h.ACTIVE_STATUSES:
                return snap
            pct = snap["progress_percent"]
            due = [s for s in PROGRESS_STEPS if s > reported and pct >= s]
            if due:
                reported = due[-1]
                log.info("Upload %s: %.0f%%", upload_id, pct)
                if self.clock() - last_note >= MIN_SECONDS_BETWEEN_PROGRESS_NOTES:
                    last_note = self.clock()
                    self._note(f"{pct:.0f}% uploaded", subtitle=title)
            if not uploads.is_running(upload_id) and snap["status"] in h.ACTIVE_STATUSES:
                # Worker ended without recording a final state (should not happen); re-read once.
                snap = uploads.snapshot(upload_id)
                if snap["status"] in h.ACTIVE_STATUSES:
                    raise YouTubeMCPError("upload_failed", "The upload stopped unexpectedly.",
                                          action="Run Upload & Share again.")
                return snap
            self.sleep(self.poll_interval)

    def _verified_visibility(self, video_id: str) -> str | None:
        try:
            return self.service.get_share_link(video_id)["visibility"]
        except YouTubeMCPError as err:
            log.warning("Could not verify visibility of %s: %s", video_id, err)
            return None

    def _existing(self, path: Path, title: str, video_id: str, message: str) -> FileResult:
        log.info("Duplicate of %s: %s", video_id, message)
        try:
            visibility: str | None = self.service.get_share_link(video_id)["visibility"]
        except YouTubeMCPError as err:
            if err.code in ("video_not_found", "not_found"):
                msg = ("This file was uploaded before, but that video is no longer on your channel. "
                       "Nothing was uploaded.")
                self._note(msg, subtitle=f"Not uploaded: {title}", sound="Basso")
                return FileResult(file=str(path), outcome="failed", video_id=video_id,
                                  message=msg + " To upload it again, use the upload_video tool with allow_duplicate=true.")
            log.warning("Could not verify existing video %s: %s", video_id, err)
            visibility = None
        return self._finish(path, title, video_id, visibility, "already_uploaded", [])

    def _finish(self, path: Path, title: str, video_id: str, visibility: str | None, outcome: str,
                warnings: list[str]) -> FileResult:
        url = short_url(video_id)
        copied = bool(self.clipboard(url))
        lead = "Already uploaded" if outcome == "already_uploaded" else "Uploaded"
        if visibility is None:
            vis_text = "Visibility could not be verified"
            warnings = [*warnings, "Visibility could not be verified; check YouTube Studio."]
        else:
            vis_text = visibility.capitalize()
        sound = "Glass"
        if visibility not in (None, VISIBILITY):
            sound = "Basso"
            if visibility == "private":
                warnings = [*warnings, "YouTube made the video PRIVATE, so others cannot open the link."]
        body = f"{vis_text}. " + ("Link copied: " if copied else "Link: ") + url.removeprefix("https://")
        if warnings:
            body += " ⚠ " + warnings[0]
        self._note(body, subtitle=f"{lead}: {title}", sound=sound)
        return FileResult(file=str(path), outcome=outcome, url=url, video_id=video_id, visibility=visibility,
                          message=body, warnings=warnings)


# --- runner --------------------------------------------------------------------------

def _configure_logging(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(settings.data_dir / "quick_action.log",
                                                   maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    for noisy in ("googleapiclient.discovery_cache", "googleapiclient.discovery", "google_auth_httplib2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        os.chmod(settings.data_dir / "quick_action.log", 0o600)
    except OSError:
        pass


@contextmanager
def _single_runner(settings: Settings, on_wait: Callable[[], None]) -> Iterator[None]:
    """Serialize Quick Action runs so the duplicate check always sees earlier uploads."""
    lock_path = settings.data_dir / "quick_action.lock"
    with os.fdopen(os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600), "r+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            on_wait()
            fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def upload_again_command(file: str) -> str:
    base = cli_command("youtube_mcp.quick_action", "upload_and_share.py")
    return " ".join(shlex.quote(part) for part in base) + f" upload --allow-duplicate -- {shlex.quote(file)}"


def run_upload(files: list[str], settings: Settings | None = None, allow_duplicate: bool = False) -> list[FileResult]:
    from .auth import CredentialManager
    from .server import create_service

    settings = settings or Settings.from_env()
    _configure_logging(settings)
    log.info("Quick Action invoked for %d file(s)", len(files))
    service = create_service(settings)
    service.staging.cleanup_stale()
    recovered = service.history.recover_interrupted()
    if recovered:
        log.warning("Marked %d stale upload(s) as interrupted", recovered)
    action = QuickAction(service, check_auth=CredentialManager(settings).get_credentials,
                         allow_duplicate=allow_duplicate)

    def on_term(signum: int, _frame: Any) -> None:
        raise Cancelled(signal.Signals(signum).name)

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGHUP, on_term)

    results: list[FileResult] = []
    try:
        with _single_runner(settings, lambda: action._note("Waiting for another upload to finish…")):
            for f in files:
                results.append(action.process(f))
    except (Cancelled, KeyboardInterrupt):
        upload_id = action.current_upload_id
        if upload_id:
            record = service.history.get_upload(upload_id) or {}
            service.history.update_upload(upload_id, status=h.INTERRUPTED, error=h.interrupted_error(
                record.get("status", h.UPLOADING), "upload_cancelled", "The upload was cancelled."))
        log.warning("Cancelled (upload %s)", upload_id)
        action._note("Upload cancelled. Nothing was published.", sound="Basso")
        results.append(FileResult(file="", outcome="cancelled", message="Cancelled"))

    links = [r.url for r in results if r.ok and r.url]
    if len(links) > 1:
        action.clipboard("\n".join(links))
        action._note(f"{len(links)} links copied to the clipboard.")
    return results


# --- install / uninstall -------------------------------------------------------------

def _shell_command(argv: list[str]) -> str:
    return (
        f"# {MARKER}: generated by the Upload & Share installer. Uploads the selected videos as UNLISTED.\n"
        "export LANG=en_US.UTF-8\n"
        f"{' '.join(shlex.quote(a) for a in argv)} upload -- \"$@\"\n"
        "exit 0  # results are reported via notifications; avoid Automator's generic error dialog\n"
    )


def _workflow_plists(command: str) -> tuple[dict[str, Any], dict[str, Any]]:
    send_types = [
        "public.movie", "public.mpeg-4", "com.apple.quicktime-movie", "public.avi", "public.mpeg",
        "public.mpeg-2-transport-stream", "public.3gpp", "com.apple.m4v-video", "org.webmproject.webm",
        "org.matroska.mkv", "com.microsoft.windows-media-wmv",
    ]
    info = {"NSServices": [{
        "NSMenuItem": {"default": MENU_TITLE},
        "NSMessage": "runWorkflowAsService",
        "NSRequiredContext": {"NSApplicationIdentifier": "com.apple.finder"},
        "NSSendFileTypes": send_types,
        "NSIconName": "NSActionTemplate",
        "NSBackgroundColorName": "background",
    }]}
    arg = lambda i, name, default: {"default value": default, "name": name, "required": "0", "type": "0", "uuid": str(i)}  # noqa: E731
    action = {
        "AMAccepts": {"Container": "List", "Optional": True, "Types": ["com.apple.cocoa.string"]},
        "AMActionVersion": "2.0.3",
        "AMApplication": ["Automator"],
        "AMParameterProperties": {k: {} for k in ("COMMAND_STRING", "CheckedForUserDefaultShell", "inputMethod", "shell", "source")},
        "AMProvides": {"Container": "List", "Types": ["com.apple.cocoa.string"]},
        "ActionBundlePath": "/System/Library/Automator/Run Shell Script.action",
        "ActionName": "Run Shell Script",
        "ActionParameters": {"COMMAND_STRING": command, "CheckedForUserDefaultShell": True,
                             "inputMethod": 1, "shell": "/bin/zsh", "source": ""},
        "BundleIdentifier": "com.apple.RunShellScript",
        "CFBundleVersion": "2.0.3",
        "CanShowSelectedItemsWhenRun": False,
        "CanShowWhenRun": True,
        "Category": ["AMCategoryUtilities"],
        "Class Name": "RunShellScriptAction",
        "InputUUID": "6C2D1E1A-6B0E-4C1F-9D3B-2A7E5B0C4D11",
        "Keywords": ["Shell", "Script", "Command", "Run", "Unix"],
        "OutputUUID": "8E4F3A2B-1C5D-4E6F-A7B8-9C0D1E2F3A44",
        "UUID": "1A2B3C4D-5E6F-4A7B-8C9D-0E1F2A3B4C55",
        "UnlocalizedApplications": ["Automator"],
        "arguments": {
            "0": arg(0, "inputMethod", 0), "1": arg(1, "CheckedForUserDefaultShell", False),
            "2": arg(2, "source", ""), "3": arg(3, "COMMAND_STRING", ""), "4": arg(4, "shell", "/bin/sh"),
        },
        "conversionLabel": 0,
        "isViewVisible": 1,
        "location": "309.000000:305.000000",
        "nibPath": "/System/Library/Automator/Run Shell Script.action/Contents/Resources/Base.lproj/main.nib",
    }
    document = {
        "AMApplicationBuild": "534", "AMApplicationVersion": "2.10", "AMDocumentVersion": "2",
        "actions": [{"action": action, "isViewVisible": 1}],
        "connectors": {},
        "workflowMetaData": {
            "applicationBundleIDsByPath": {}, "applicationPaths": [],
            "inputTypeIdentifier": "com.apple.Automator.fileSystemObject.movie",
            "outputTypeIdentifier": "com.apple.Automator.nothing",
            "presentationMode": 15, "processesInput": False,
            "serviceApplicationBundleID": "com.apple.finder", "serviceApplicationPath": "/System/Library/CoreServices/Finder.app",
            "serviceInputTypeIdentifier": "com.apple.Automator.fileSystemObject.movie",
            "serviceOutputTypeIdentifier": "com.apple.Automator.nothing",
            "serviceProcessesInput": False, "systemImageName": "NSActionTemplate",
            "useAutomaticInputType": False, "workflowTypeIdentifier": "com.apple.Automator.servicesMenu",
        },
    }
    return info, document


def _is_ours(bundle: Path) -> bool:
    try:
        return MARKER in (bundle / "Contents" / "document.wflow").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


def _refresh_services() -> None:
    pbs = "/System/Library/CoreServices/pbs"
    if Path(pbs).exists():
        subprocess.run([pbs, "-flush"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run([pbs, "-update"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def install(services_dir: Path = SERVICES_DIR) -> int:
    target = services_dir / WORKFLOW_NAME
    if target.exists() and not _is_ours(target):
        print(f"Refusing to overwrite {target}: it was not created by this installer. Rename or remove it first.",
              file=sys.stderr)
        return 1
    # Source checkout: <python> upload_and_share.py; installed package: <python> -m youtube_mcp.quick_action
    argv = cli_command("youtube_mcp.quick_action", "upload_and_share.py")
    info, document = _workflow_plists(_shell_command(argv))
    services_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".upload-share-", dir=services_dir))
    try:
        contents = staging / WORKFLOW_NAME / "Contents"
        contents.mkdir(parents=True)
        (contents / "Info.plist").write_bytes(plistlib.dumps(info))
        (contents / "document.wflow").write_bytes(plistlib.dumps(document))
        if target.exists():
            shutil.rmtree(target)
        (staging / WORKFLOW_NAME).rename(target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    _refresh_services()
    print(f"Installed Finder Quick Action: {target}")
    print(f"Runs: {' '.join(argv)} upload <files>")
    print("Right-click a video in Finder > Quick Actions > Upload & Share.")
    return 0


def workflow_command(services_dir: Path = SERVICES_DIR) -> str | None:
    """The shell command embedded in an installed Upload & Share workflow (None if absent or unreadable)."""
    try:
        doc = plistlib.loads((services_dir / WORKFLOW_NAME / "Contents" / "document.wflow").read_bytes())
        return doc["actions"][0]["action"]["ActionParameters"]["COMMAND_STRING"]
    except (OSError, KeyError, IndexError, TypeError, plistlib.InvalidFileException):
        return None


def uninstall(services_dir: Path = SERVICES_DIR) -> int:
    target = services_dir / WORKFLOW_NAME
    if not target.exists():
        print(f"Not installed ({target} does not exist).")
        return 0
    if not _is_ours(target):
        print(f"Refusing to remove {target}: it was not created by this installer.", file=sys.stderr)
        return 1
    shutil.rmtree(target)
    _refresh_services()
    print(f"Removed {target}. Upload history, authorization and the MCP server are unchanged.")
    return 0


def status(settings: Settings | None = None) -> int:
    from .auth import CredentialManager

    settings = settings or Settings.from_env()
    target = SERVICES_DIR / WORKFLOW_NAME
    print(f"Quick Action : {target} ({'installed' if target.exists() else 'not installed'})")
    print(f"Log file     : {settings.data_dir / 'quick_action.log'}")
    try:
        CredentialManager(settings).get_credentials()
        print("Authorization: OK")
        return 0
    except YouTubeMCPError as err:
        print(f"Authorization: {err.message}\n  {err.action or ''}")
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="upload_and_share.py", description="YouTube 'Upload & Share' Quick Action.")
    sub = parser.add_subparsers(dest="command", required=True)
    up = sub.add_parser("upload", help="Upload video files as unlisted and copy the share link.")
    up.add_argument("--allow-duplicate", action="store_true",
                    help="Upload even if this file may already be on YouTube (only after checking YouTube Studio).")
    up.add_argument("files", nargs="+")
    sub.add_parser("install", help="Install the Finder Quick Action into ~/Library/Services.")
    sub.add_parser("uninstall", help="Remove the Finder Quick Action.")
    sub.add_parser("status", help="Show installation and authorization status.")
    args = parser.parse_args(argv)

    if args.command == "install":
        return install()
    if args.command == "uninstall":
        return uninstall()
    if args.command == "status":
        return status()
    results = run_upload(args.files, allow_duplicate=args.allow_duplicate)
    for r in results:
        print(json.dumps({k: v for k, v in r.__dict__.items() if v not in (None, [], "")}))
    return 0 if results and all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
