"""Background resumable uploads with retry, progress tracking and visibility verification.

Uploads run in a worker thread so a long upload never blocks the MCP connection or hits
the client's tool-call timeout. Progress lives in memory and is mirrored to the history DB.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import history as h
from .errors import YouTubeMCPError, translate_exception
from .history import History
from .youtube import WRITABLE_STATUS_FIELDS, YouTubeClient, short_url, studio_url

log = logging.getLogger(__name__)

CHUNK_SIZE = 8 * 1024 * 1024


def _restricted_private_warning() -> dict[str, Any]:
    return {
        "code": "visibility_restricted",
        "message": (
            "YouTube set this video to PRIVATE instead of the requested visibility. This most likely means "
            "the Google Cloud project has not passed YouTube's API compliance audit: videos uploaded through "
            "unaudited API projects are locked to private."
        ),
        "action": (
            "The user can change visibility manually in YouTube Studio (if allowed), or apply for the audit at "
            "https://support.google.com/youtube/contact/yt_api_form. set_visibility may not be able to change it."
        ),
    }


class UploadManager:
    def __init__(
        self,
        client: YouTubeClient,
        history: History,
        *,
        notifier: Callable[[str, str], bool] | None = None,
        clipboard: Callable[[str], bool] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        chunk_size: int = CHUNK_SIZE,
        max_retries: int = 8,
        verifier: Any = None,
    ) -> None:
        self.client = client
        self.history = history
        self.notifier = notifier
        self.clipboard = clipboard
        self.sleep = sleep
        self.chunk_size = chunk_size
        self.max_retries = max_retries
        from .verify import Verifier
        self.verifier = verifier or Verifier(sleep=sleep)
        self._threads: dict[str, threading.Thread] = {}
        self._live: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    # --- public API -------------------------------------------------------------------

    def start(
        self,
        upload_id: str,
        path: Path,
        body: dict[str, Any],
        *,
        copy_link: bool = False,
        notify: bool = True,
        background: bool = True,
        cleanup: Callable[[], None] | None = None,
    ) -> None:
        """Start an upload. `cleanup` runs after the upload ends (success or failure)."""
        with self._lock:
            self._live[upload_id] = {"progress": 0.0}
        args = (upload_id, path, body, copy_link, notify, cleanup)
        if not background:
            self._run(*args)
            return
        thread = threading.Thread(target=self._run, args=args, name=f"upload-{upload_id}", daemon=True)
        self._threads[upload_id] = thread
        thread.start()

    def is_running(self, upload_id: str) -> bool:
        thread = self._threads.get(upload_id)
        return bool(thread and thread.is_alive())

    def join(self, upload_id: str, timeout: float | None = None) -> None:
        thread = self._threads.get(upload_id)
        if thread:
            thread.join(timeout)

    def snapshot(self, upload_id: str) -> dict[str, Any]:
        record = self.history.get_upload(upload_id)
        if record is None:
            raise YouTubeMCPError("upload_not_found", f"No upload with ID '{upload_id}' exists in the local history.",
                                  action="Use list_uploads with source='local' to see recorded uploads.")
        live = self._live.get(upload_id, {})
        progress = live.get("progress", record["progress"]) if record["status"] in h.ACTIVE_STATUSES else record["progress"]
        snap: dict[str, Any] = {
            "upload_id": upload_id,
            "status": record["status"],
            "progress_percent": round(float(progress) * 100, 1),
            "file": record["file_path"],
            "title": record["title"],
            "requested_visibility": record["requested_visibility"],
            "actual_visibility": record["actual_visibility"],
            "video_id": record["video_id"],
            "warnings": record["warnings"] or [],
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
        }
        if record["video_id"]:
            snap["url"] = short_url(record["video_id"])
            snap["studio_url"] = studio_url(record["video_id"])
        if record["error"]:
            snap["error"] = record["error"]
        if record["status"] in h.ACTIVE_STATUSES:
            snap["next_step"] = f"Upload still running. Call get_upload_progress with upload_id='{upload_id}' to check again."
        elif record["status"] == h.COMPLETED:
            snap["next_step"] = ("Uploaded. YouTube may still be processing the video; "
                                 "use get_video_status to check when it is ready.")
        return snap

    # --- worker -----------------------------------------------------------------------

    def _set_progress(self, upload_id: str, progress: float, last_saved: list[float]) -> None:
        with self._lock:
            self._live.setdefault(upload_id, {})["progress"] = progress
        if progress - last_saved[0] >= 0.05:
            last_saved[0] = progress
            self.history.update_upload(upload_id, progress=progress)

    def _run(self, upload_id: str, path: Path, body: dict[str, Any], copy_link: bool, notify: bool,
             cleanup: Callable[[], None] | None = None) -> None:
        try:
            self._run_upload(upload_id, path, body, copy_link, notify)
        finally:
            if cleanup:
                try:
                    cleanup()
                except Exception:  # pragma: no cover - cleanup must never mask the result
                    log.warning("Cleanup after upload %s failed", upload_id)

    def _run_upload(self, upload_id: str, path: Path, body: dict[str, Any], copy_link: bool, notify: bool) -> None:
        requested = body["status"]["privacyStatus"]
        title = body["snippet"]["title"]
        sent = [False]  # becomes True once media bytes may have been transmitted
        try:
            self.history.update_upload(upload_id, status=h.UPLOADING, progress=0.0)
            log.info("Upload %s started: %s", upload_id, path.name)
            request = self.client.create_upload_request(
                body, path, self.chunk_size, notify_subscribers=(requested == "public"))
            response = self._upload_chunks(upload_id, request, sent)
            video_id = response.get("id") if isinstance(response, dict) else None
            if not video_id:
                raise YouTubeMCPError("upload_failed", "YouTube finished the upload but returned no video ID.",
                                      action="Check YouTube Studio for the video before retrying.")
        except Exception as exc:
            err = translate_exception(exc)
            log.warning("Upload %s failed: %s", upload_id, err)
            error = err.to_dict()["error"]
            http_status = (error.get("details") or {}).get("http_status")
            if not sent[0]:
                error["outcome"] = h.OUTCOME_NOT_SENT
            elif isinstance(http_status, int) and 400 <= http_status < 500 and err.code != "upload_failed":
                error["outcome"] = h.OUTCOME_REJECTED  # YouTube answered and refused: no video was created
            else:
                error["outcome"] = h.OUTCOME_UNCERTAIN
                error["action"] = ((error.get("action") or "") + " YouTube may have received the video before the "
                                   "failure. Retrying first checks the channel for it; if that cannot settle it, the "
                                   "user must decide explicitly (allow_duplicate=true) before uploading again.").strip()
            self.history.update_upload(upload_id, status=h.FAILED, error=error)
            self.history.log_action("upload_failed", None, {"upload_id": upload_id, "error": err.code})
            if notify and self.notifier:
                self.notifier("YouTube upload failed", f"{title}: {err.message}")
            return

        self.history.update_upload(upload_id, status=h.VERIFYING, progress=1.0, video_id=video_id)
        with self._lock:
            self._live[upload_id]["progress"] = 1.0
        actual, warnings = self._verify_visibility(video_id, requested)
        self.history.update_upload(upload_id, status=h.COMPLETED, actual_visibility=actual, warnings=warnings)
        self.history.log_action("upload", video_id, {
            "upload_id": upload_id, "file": str(path), "title": title,
            "requested_visibility": requested, "actual_visibility": actual,
        })
        url = short_url(video_id)
        log.info("Upload %s complete: video %s (visibility %s)", upload_id, video_id, actual)
        copied = bool(copy_link and self.clipboard and self.clipboard(url))
        if notify and self.notifier:
            if any(w.get("urgent") for w in warnings):
                self.notifier("YouTube upload: CHECK VISIBILITY NOW",
                              f"{title} may be PUBLIC. Open YouTube Studio: {studio_url(video_id)}")
            else:
                self.notifier("YouTube upload complete",
                              f"{title} ({actual or requested})" + (" - link copied" if copied else ""))

    def _upload_chunks(self, upload_id: str, request: Any, sent: list[bool] | None = None) -> Any:
        response = None
        failures = 0
        last_saved = [0.0]
        while response is None:
            try:
                if sent is not None:
                    sent[0] = True
                status, response = request.next_chunk(num_retries=3)
                failures = 0
                if status is not None:
                    self._set_progress(upload_id, float(status.progress()), last_saved)
            except Exception as exc:
                err = translate_exception(exc)
                if not err.retryable or failures >= self.max_retries:
                    raise err from None
                failures += 1
                delay = min(2 ** failures, 64) * random.uniform(0.5, 1.0)
                log.warning("Upload %s: transient error (%s); retry %d/%d in %.1fs",
                            upload_id, err.code, failures, self.max_retries, delay)
                # Ask the server how many bytes it actually has before sending the next chunk.
                if hasattr(request, "_in_error_state"):
                    request._in_error_state = True
                self.sleep(delay)
        return response

    def _verify_visibility(self, video_id: str, requested: str) -> tuple[str | None, list[dict[str, Any]]]:
        warnings: list[dict[str, Any]] = []
        # A just-uploaded video can briefly be "not found" or show an older state; retry reads only.
        # Stop at the first readable state: an unexpected PUBLIC must be corrected immediately, not after retries.
        ver = self.verifier.wait_for(lambda: self.client.get_video(video_id, parts="status"),
                                     lambda v: bool(v.get("status", {}).get("privacyStatus")))
        if ver.observed is None:
            code = (ver.last_error or {}).get("code", "unknown")
            return None, [{
                "code": "verification_failed",
                "message": f"The upload succeeded but its visibility could not be verified ({code}).",
                "action": "Call get_video_status to confirm the visibility. Do not upload the file again.",
            }]
        video = ver.observed
        status = video.get("status", {})
        actual = status.get("privacyStatus")
        if status.get("uploadStatus") in ("rejected", "failed"):
            warnings.append({
                "code": f"upload_{status['uploadStatus']}",
                "message": f"YouTube {status['uploadStatus']} the video "
                           f"(reason: {status.get('rejectionReason') or status.get('failureReason') or 'unknown'}).",
                "action": "Open the video in YouTube Studio for details.",
            })

        if actual == requested:
            return actual, warnings
        if actual == "public":
            return self._correct_unexpected_public(video_id, requested, status, warnings)
        if actual == "private":
            warnings.append(_restricted_private_warning())
        else:
            warnings.append({"code": "visibility_mismatch",
                             "message": f"Requested {requested} but YouTube reports {actual}.",
                             "action": "Use set_visibility or YouTube Studio to fix it."})
        return actual, warnings

    def _correct_unexpected_public(self, video_id: str, requested: str, status: dict[str, Any],
                                   warnings: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, Any]]]:
        """Safety: never leave a video public that was not requested as public.

        The corrective write is sent once (never retried). Success is only claimed when a read-back
        confirms the requested visibility; otherwise an urgent manual-attention warning is returned.
        """
        urgent = {"code": "unexpectedly_public", "requires_manual_attention": True, "urgent": True,
                  "studio_url": studio_url(video_id),
                  "action": f"Check it NOW and set the visibility in YouTube Studio: {studio_url(video_id)}"}
        fields = {k: status[k] for k in WRITABLE_STATUS_FIELDS if k in status}
        fields["privacyStatus"] = requested
        fields.pop("publishAt", None)
        try:
            self.client.update_video(video_id, "status", fields)
        except YouTubeMCPError as err:
            self.history.log_action("visibility_correction_failed", video_id, {"to": requested, "error": err.code})
            warnings.append({**urgent, "message": (f"YouTube reported the video as PUBLIC although {requested} was "
                                                   f"requested, and changing it back failed ({err.code}). It may "
                                                   "still be public.")})
            return "public", warnings
        ver = self.verifier.wait_for(lambda: self.client.get_video(video_id, parts="status"),
                                     lambda v: v.get("status", {}).get("privacyStatus") == requested)
        if ver.confirmed:
            warnings.append({"code": "visibility_corrected",
                             "message": f"YouTube reported the video as public; it was changed back to {requested} "
                                        "(confirmed by reading it back)."})
            self.history.log_action("visibility_corrected", video_id, {"from": "public", "to": requested})
            return requested, warnings
        observed = ((ver.observed or {}).get("status") or {}).get("privacyStatus")
        self.history.log_action("visibility_correction_unconfirmed", video_id,
                                {"to": requested, "observed": observed, "verification": ver.status})
        warnings.append({**urgent, "verification": ver.to_dict(),
                         "message": (f"YouTube reported the video as PUBLIC although {requested} was requested. A "
                                     f"change back to {requested} was sent, but it could not be confirmed (YouTube "
                                     f"last reported: {observed or 'unreadable'}). It may still be public. The change "
                                     "was not repeated.")})
        return observed or None, warnings
