"""Business logic for every MCP tool. Synchronous and transport-agnostic, so it is easy to test."""

from __future__ import annotations

import hashlib
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import approval
from . import history as h
from .approval import next_step_for_confirmation, public_requires_approval
from .config import DEFAULT_VISIBILITY, Settings
from .confirmations import ConfirmationStore
from .errors import YouTubeMCPError
from .history import History
from .native_confirm import macos_confirm_public
from .staging import Staging
from .verify import NOT_CONFIRMED, UNVERIFIABLE, Verifier, write_report
from .macos import copy_to_clipboard
from .uploads import UploadManager
from .youtube_media import local_duration_ms
from .youtube import (
    WRITABLE_SNIPPET_FIELDS,
    WRITABLE_STATUS_FIELDS,
    YouTubeClient,
    invalid,
    parse_video_id,
    short_url,
    studio_url,
    validate_category_id,
    validate_description,
    validate_tags,
    validate_thumbnail_file,
    validate_title,
    validate_video_file,
    validate_visibility,
    watch_url,
)

log = logging.getLogger(__name__)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# Kept under their historical names for backward compatibility.
_next_step_for_confirmation = next_step_for_confirmation
_public_requires_approval = public_requires_approval


class YouTubeMCPService:
    def __init__(
        self,
        settings: Settings,
        client: YouTubeClient,
        history: History,
        uploads: UploadManager,
        confirmations: ConfirmationStore | None = None,
        clipboard: Callable[[str], bool] = copy_to_clipboard,
        analytics_client: Any = None,
        public_confirmer: Callable[[str, list[tuple[str, str]]], None] | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.history = history
        self.uploads = uploads
        self.confirmations = confirmations or ConfirmationStore(settings.confirmation_ttl_seconds)
        self.clipboard = clipboard
        self._hash_cache: dict[tuple[str, int, int], str] = {}
        self._start_lock = threading.Lock()
        # Native (out-of-band) confirmation for anything that makes content public. Fail-closed default;
        # there is deliberately no setting or tool parameter that disables it.
        self._public_confirmer = public_confirmer or macos_confirm_public
        self._channel_title: str | None = None
        self.staging = Staging(settings.data_dir / "staging")
        self.verifier = Verifier()  # read-only retries after writes (see verify.py)
        # Feature areas (Phase 7). Imported here to avoid import cycles.
        from .analytics import AnalyticsService
        from .captions import CaptionsService
        from .categories import CategoryCatalog
        from .playlists import PlaylistsService
        from .scheduling import SchedulingService

        self.captions = CaptionsService(self)
        self.categories = CategoryCatalog(self)
        self.playlists = PlaylistsService(self)
        self.scheduling = SchedulingService(self)
        self.analytics = AnalyticsService(self, analytics_client)

    # --- helpers ------------------------------------------------------------------------

    def channel_title(self) -> str:
        if self._channel_title is None:
            ch = self.client.get_my_channel()
            custom = ch.get("snippet", {}).get("customUrl")
            self._channel_title = f"{ch.get('snippet', {}).get('title')}" + (f" ({custom})" if custom else "")
        return self._channel_title

    def confirm_public(self, action: str, details: list[tuple[str, str]]) -> None:
        """Ask the person directly (native dialog). Raises unless they explicitly allowed it."""
        full = [("Channel", self.channel_title()), *details]
        try:
            self._public_confirmer(action, full)
        except YouTubeMCPError as err:
            self.history.log_action("native_confirmation_refused", None, {"action": action, "code": err.code})
            raise
        self.history.log_action("native_confirmation_allowed", None, {"action": action, "details": dict(full)})

    def file_sha256(self, path: Path) -> str:
        stat = path.stat()
        key = (str(path), stat.st_size, stat.st_mtime_ns)
        if key not in self._hash_cache:
            digest = hashlib.sha256()
            with path.open("rb") as fh:
                for block in iter(lambda: fh.read(1024 * 1024), b""):
                    digest.update(block)
            self._hash_cache[key] = digest.hexdigest()
        return self._hash_cache[key]

    def _check_duplicates(self, sha256: str, allow_duplicate: bool) -> None:
        self._duplicate_conflict(self.history.find_by_sha256(sha256), allow_duplicate)

    def _duplicate_conflict(self, records: list[dict[str, Any]], allow_duplicate: bool) -> None:
        active = [r for r in records if r["status"] in h.ACTIVE_STATUSES]
        if active:
            raise YouTubeMCPError(
                "upload_in_progress",
                "This exact file is already being uploaded.",
                action=f"Track it with get_upload_progress(upload_id='{active[0]['upload_id']}').",
                details={"upload_id": active[0]["upload_id"]},
            )
        done = [r for r in records if r["status"] == h.COMPLETED and r["video_id"]]
        if done and not allow_duplicate:
            prev = done[0]
            raise YouTubeMCPError(
                "duplicate_upload",
                f"This exact file was already uploaded on {prev['created_at']} as '{prev['title']}'.",
                action=("Share the existing video instead. Only if the user explicitly wants a second copy, "
                        "call upload_video again with allow_duplicate=true."),
                details={"video_id": prev["video_id"], "url": short_url(prev["video_id"]),
                         "upload_id": prev["upload_id"]},
            )

    def _reconcile_ambiguous(self, path: Path, sha256: str, size: int, allow_duplicate: bool) -> None:
        """Did an earlier failed/interrupted attempt of this exact file reach YouTube anyway?

        (E.g. the connection dropped after YouTube accepted the last byte.) Recent channel uploads
        published after the attempt began are compared on several independent signals:
        exact byte size (fileDetails.fileSize), exact title and description as sent, duration
        (when it can be read locally), and the video not being linked to any other history record.

        - exactly one video matches ALL signals and nothing else partially matches -> the attempt is
          recorded as completed with an explicit `recovered_after_failure` warning;
        - anything partially matches (size or title) -> `ambiguous_upload` error for manual review;
          nothing is linked and nothing is uploaded;
        - no evidence -> `upload_outcome_uncertain`: not finding the video among recent uploads does not prove
          YouTube never received it (it may still be processing, or not yet listed), so the outcome stays
          uncertain and the user must decide explicitly before the file is uploaded again.
        Only attempts whose outcome is uncertain (bytes were sent, no definite answer) are checked. Attempts
        that failed before anything was sent, or that YouTube explicitly refused (4xx), never block a retry.
        allow_duplicate=true (the user's explicit decision after reviewing) skips this check.
        """
        records = self.history.find_by_sha256(sha256)
        if any(r["status"] == h.COMPLETED for r in records):
            return
        attempts = [r for r in records if r["status"] in (h.FAILED, h.INTERRUPTED) and not r["video_id"]
                    and h.attempt_outcome(r) == h.OUTCOME_UNCERTAIN]
        if not attempts or allow_duplicate:
            return
        earliest = min(_parse_ts(a["created_at"]) for a in attempts) - timedelta(minutes=10)
        channel = self.client.get_my_channel()
        playlist = channel.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        if not playlist:
            raise self._uncertain_outcome(attempts, "the channel's uploads list could not be read")
        entries = self.client.list_playlist_items(playlist, 50, None).get("items") or []
        ids = [e.get("contentDetails", {}).get("videoId") for e in entries if e.get("contentDetails", {}).get("videoId")]
        details = self.client.get_videos(ids, parts="fileDetails,status,snippet")
        linked = self.history.known_video_ids()
        local_ms = local_duration_ms(path)
        confirmed: dict[str, dict[str, Any]] = {}
        partial: dict[str, dict[str, Any]] = {}
        for vid, v in details.items():
            published = _parse_ts(v.get("snippet", {}).get("publishedAt"))
            if vid in linked or published is None or published < earliest:
                continue
            sn, fd = v.get("snippet", {}), v.get("fileDetails", {})
            remote_ms = int(fd["durationMs"]) if str(fd.get("durationMs", "")).isdigit() else None
            for attempt in attempts:
                evidence = {
                    "file_size_matches": fd.get("fileSize") is not None and int(fd["fileSize"]) == size,
                    "title_matches": sn.get("title") == attempt["title"],
                    "description_matches": (sn.get("description") or "").strip() == (attempt["description"] or "").strip(),
                    "duration_matches": (None if local_ms is None or remote_ms is None
                                         else abs(local_ms - remote_ms) <= 1500),
                }
                info = {"video_id": vid, "title": sn.get("title"), "published_at": sn.get("publishedAt"),
                        "visibility": v.get("status", {}).get("privacyStatus"), "studio_url": studio_url(vid),
                        "evidence": evidence, "attempt": attempt["upload_id"]}
                if all(evidence[k] for k in ("file_size_matches", "title_matches", "description_matches")) \
                        and evidence["duration_matches"] is not False:
                    confirmed[vid] = info
                elif evidence["file_size_matches"] or evidence["title_matches"]:
                    partial[vid] = info
        if len(confirmed) == 1 and not partial:
            info = next(iter(confirmed.values()))
            attempt = next(a for a in attempts if a["upload_id"] == info["attempt"])
            warning = {"code": "recovered_after_failure",
                       "message": (f"The earlier attempt was recorded as {attempt['status']}, but YouTube had received "
                                   f"it as video {info['video_id']} (matched on exact file size, title, description"
                                   + (", duration" if info["evidence"]["duration_matches"] else "") + ")."),
                       "action": f"Check it in YouTube Studio: {info['studio_url']}"}
            self.history.update_upload(attempt["upload_id"], status=h.COMPLETED, video_id=info["video_id"],
                                       actual_visibility=info["visibility"],
                                       warnings=[*(attempt["warnings"] or []), warning])
            self.history.log_action("upload_recovered", info["video_id"], {"upload_id": attempt["upload_id"],
                                                                           "evidence": info["evidence"]})
            log.warning("Recovered upload %s as video %s", attempt["upload_id"], info["video_id"])
            return
        if confirmed or partial:
            candidates = [{k: v for k, v in c.items() if k != "attempt"} for c in [*confirmed.values(), *partial.values()]]
            raise YouTubeMCPError(
                "ambiguous_upload",
                "An earlier attempt to upload this file failed, and the channel has recent video(s) that might be "
                "that upload, but its identity cannot be confirmed. Nothing was linked or uploaded.",
                action=("Ask the user to check these videos in YouTube Studio. If none of them is this file and they "
                        "want to upload it again, call upload_video with allow_duplicate=true."),
                details={"candidates": candidates})
        raise self._uncertain_outcome(attempts, f"none of the channel's {len(details)} most recent uploads matches it")

    def _uncertain_outcome(self, attempts: list[dict[str, Any]], why: str) -> YouTubeMCPError:
        latest = max(attempts, key=lambda a: a["created_at"])
        return YouTubeMCPError(
            "upload_outcome_uncertain",
            (f"An earlier attempt to upload this file ({latest['created_at']}) was interrupted after data was sent, "
             f"so YouTube may have received it. {why[0].upper() + why[1:]}, but that does not prove the upload "
             "failed (new videos can take a while to be listed). Nothing was uploaded."),
            action=("Ask the user to check YouTube Studio (Content) for this video. Only if they confirm it is not "
                    "there and explicitly want to upload it again, call upload_video with allow_duplicate=true."),
            details={"attempts": [{"upload_id": a["upload_id"], "status": a["status"], "started_at": a["created_at"],
                                   "title": a["title"], "error": (a.get("error") or {}).get("code")}
                                  for a in attempts],
                     "studio_url": "https://studio.youtube.com/"})

    # --- upload_video -------------------------------------------------------------------

    def upload_video(
        self,
        file_path: str,
        title: str,
        description: str = "",
        tags: list[str] | None = None,
        category_id: str | None = None,
        visibility: str = DEFAULT_VISIBILITY,
        made_for_kids: bool = False,
        allow_duplicate: bool = False,
        copy_link: bool = False,
        notify: bool = True,
        confirmation_token: str | None = None,
        confirm_public: bool = False,
        background: bool = True,
    ) -> dict[str, Any]:
        path = validate_video_file(file_path)
        title = validate_title(title)
        description = validate_description(description)
        tags = validate_tags(tags)
        category_id = self.categories.ensure_assignable(category_id or self.settings.default_category_id)
        visibility = validate_visibility(visibility or DEFAULT_VISIBILITY)
        sha256 = self.file_sha256(path)
        size = path.stat().st_size

        self.history.recover_interrupted()  # stale "active" rows from crashed processes must not block
        self._reconcile_ambiguous(path, sha256, size, allow_duplicate)
        self._check_duplicates(sha256, allow_duplicate)

        params = {
            "file": str(path), "sha256": sha256, "title": title, "description": description, "tags": tags,
            "category_id": category_id, "visibility": visibility, "made_for_kids": bool(made_for_kids),
            "allow_duplicate": bool(allow_duplicate),
        }

        if not confirmation_token:
            token = self.confirmations.issue("upload_video", params)
            return {
                "ok": True,
                "status": "confirmation_required",
                "preview": {
                    "file": str(path), "file_size_mb": round(size / 1024 / 1024, 1), "title": title,
                    "description": description, "tags": tags, "category_id": category_id,
                    "visibility": visibility, "made_for_kids": bool(made_for_kids),
                    "duplicate_of_previous_upload": bool(allow_duplicate and self.history.find_by_sha256(sha256)),
                },
                "warnings": (["VISIBILITY IS PUBLIC: anyone will be able to find and watch this video."]
                             if visibility == "public" else []),
                "confirmation_token": token,
                "expires_in_seconds": self.confirmations.ttl,
                "next_step": _next_step_for_confirmation("upload_video", token, visibility == "public"),
            }

        if visibility == "public" and not confirm_public:
            raise _public_requires_approval()
        self.confirmations.consume(confirmation_token, "upload_video", params)
        if visibility == "public":
            self.confirm_public("Upload a video as PUBLIC", [
                ("Video title", title), ("File", path.name), ("Size", f"{size / 1024 / 1024:.1f} MB")])

        body = {
            "snippet": {"title": title, "description": description, "categoryId": category_id,
                        **({"tags": tags} if tags else {})},
            "status": {"privacyStatus": visibility, "selfDeclaredMadeForKids": bool(made_for_kids)},
        }
        # Upload a verified private snapshot, so the bytes sent are exactly the bytes approved.
        staged = self.staging.stage(path, sha256)
        try:
            with self._start_lock:  # in-process guard; the DB transaction below guards across processes
                upload_id = self.history.create_upload_checked(
                    lambda records: self._duplicate_conflict(records, allow_duplicate),
                    file_path=str(path), file_name=path.name, file_size=size, sha256=sha256, title=title,
                    description=description, tags=tags, category_id=category_id, requested_visibility=visibility,
                )
        except BaseException:
            self.staging.discard(staged)
            raise
        if visibility == "public":
            self.history.log_action("public_upload_approved", None, {"upload_id": upload_id, "title": title})
        self.uploads.start(upload_id, staged, body, copy_link=copy_link, notify=notify, background=background,
                           cleanup=lambda: self.staging.discard(staged))
        return {"ok": True, **self.uploads.snapshot(upload_id)}

    def get_upload_progress(self, upload_id: str) -> dict[str, Any]:
        return {"ok": True, **self.uploads.snapshot((upload_id or "").strip())}

    # --- update_metadata ----------------------------------------------------------------

    def update_metadata(
        self,
        video_id: str,
        title: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        category_id: str | None = None,
        default_language: str | None = None,
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        video_id = parse_video_id(video_id)
        changes: dict[str, Any] = {}
        if title is not None:
            changes["title"] = validate_title(title)
        if description is not None:
            changes["description"] = validate_description(description)
        if tags is not None:
            changes["tags"] = validate_tags(tags)
        if category_id is not None:
            changes["categoryId"] = self.categories.ensure_assignable(category_id)
        if default_language is not None:
            changes["defaultLanguage"] = default_language.strip()
        if not changes:
            raise invalid("Nothing to update.", "Pass at least one of title, description, tags, category_id, default_language.")

        def plan() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
            snippet = self.client.get_video(video_id, parts="snippet")["snippet"]
            current = {k: snippet[k] for k in WRITABLE_SNIPPET_FIELDS if k in snippet}
            updated = {**current, **changes}
            if not updated.get("tags"):
                updated.pop("tags", None)  # sending an empty list is how tags are cleared; omitting also clears
            changed = {k: {"before": current.get(k), "after": updated.get(k)}
                       for k in changes if current.get(k) != updated.get(k)}
            return current, updated, changed

        current, updated, changed = plan()
        if not changed:
            return {"ok": True, "status": "no_change", "video_id": video_id,
                    "message": "The video already has these values."}
        params = {"video_id": video_id, "changes": changes}
        if not confirmation_token:
            return approval.confirmation_required(
                self.confirmations, "update_metadata", params,
                {"video_id": video_id, "current_title": current.get("title"), "changes": changed,
                 "unchanged_fields_preserved": sorted(set(current) - set(changes))})
        approval.consume(self.confirmations, "update_metadata", confirmation_token, params)
        current, updated, changed = plan()  # re-read right before writing
        if not changed:
            return {"ok": True, "status": "no_change", "video_id": video_id}
        self.client.update_video(video_id, "snippet", updated)
        expected = {k: updated.get(k) for k in changes}
        ver = self.verifier.wait_for(
            lambda: self.client.get_video(video_id, parts="snippet")["snippet"],
            lambda sn: all((sn.get(k) or None) == (v or None) for k, v in expected.items()))
        self.history.log_action("update_metadata", video_id, {"changed": changed, "verified": ver.confirmed})
        return {"ok": True, "status": "updated", "video_id": video_id, "url": short_url(video_id),
                "changed": changed, "unchanged_fields_preserved": sorted(set(current) - set(changes)),
                **write_report(ver, resource={"video_id": video_id}, check_with="get_video_status")}

    # --- set_visibility -----------------------------------------------------------------

    def set_visibility(
        self,
        video_id: str,
        visibility: str,
        confirmation_token: str | None = None,
        confirm_public: bool = False,
    ) -> dict[str, Any]:
        video_id = parse_video_id(video_id)
        target = validate_visibility(visibility)
        video = self.client.get_video(video_id, parts="snippet,status")
        status = video.get("status", {})
        current = status.get("privacyStatus")
        title = video.get("snippet", {}).get("title")
        if current == target:
            return {"ok": True, "status": "no_change", "video_id": video_id, "visibility": current}

        public = target == "public"
        params: dict[str, Any] = {"video_id": video_id, "visibility": target}
        # Indirect exposure: a PRIVATE video that becomes UNLISTED is visible to anyone viewing a PUBLIC playlist
        # that contains it. Only this channel's playlists can be checked. The result is bound to the approval.
        via: dict[str, Any] | None = None
        if current == "private" and target == "unlisted":
            via = self.playlists.public_playlists_containing(video_id)
            params["exposed_via_public_playlists"] = [p["playlist_id"] for p in via["playlists"]]
            params["exposure_check_complete"] = via["complete"]
        exposed = bool(via and via["playlists"])  # treated like a public action: confirm_public + native dialog
        if not confirmation_token:
            preview = {"video_id": video_id, "title": title, "current_visibility": current,
                       "new_visibility": target, "url": watch_url(video_id)}
            warnings = ["This will make the video PUBLIC: anyone will be able to find and watch it."] if public else []
            if via is not None:
                preview["public_playlists_containing_video"] = via["playlists"]
                preview["exposure_check"] = ("this channel's playlists only" if via["complete"]
                                             else "incomplete: not every playlist could be checked")
                if via["playlists"]:
                    warnings.append(f"The video is in {len(via['playlists'])} PUBLIC playlist(s) on this channel; once "
                                    "unlisted, anyone viewing those playlists can watch it.")
            if status.get("publishAt") and target != "private":
                warnings.append("This also removes the video's scheduled publish time.")
            return approval.confirmation_required(self.confirmations, "set_visibility", params, preview,
                                                  public=public or exposed, warnings=warnings)
        approval.consume(self.confirmations, "set_visibility", confirmation_token, params,
                         public=public or exposed, confirm_public=confirm_public)
        if public:
            self.confirm_public("Make a video PUBLIC", [("Video title", title), ("Video ID", video_id),
                                                       ("Current visibility", current)])
        elif exposed:
            assert via is not None
            self.confirm_public("Make a video visible through PUBLIC playlists", [
                ("Video title", title), ("Change", "private -> unlisted"),
                *[("Public playlist", p["title"] or p["playlist_id"]) for p in via["playlists"][:5]]])
        status = self.client.get_video(video_id, parts="status")["status"]  # re-read right before writing
        fields = {k: status[k] for k in WRITABLE_STATUS_FIELDS if k in status}
        fields["privacyStatus"] = target
        if target != "private":
            fields.pop("publishAt", None)  # scheduled publishing only applies to private videos
        self.client.update_video(video_id, "status", fields)

        ver = self.verifier.wait_for(lambda: self.client.get_video(video_id, parts="status")["status"],
                                     lambda st: st.get("privacyStatus") == target)
        actual = (ver.observed or {}).get("privacyStatus") if ver.status != UNVERIFIABLE else None
        report = write_report(ver, resource={"video_id": video_id}, check_with="get_video_status",
                              manual_attention=True)
        warnings = report.pop("warnings", [])
        if ver.status == NOT_CONFIRMED:
            warnings.insert(0, {
                "code": "visibility_not_applied",
                "message": f"YouTube still reports '{actual}'. If the project is unaudited, API uploads stay locked to private.",
                "action": f"Do not repeat the change. Check it in YouTube Studio: {studio_url(video_id)}",
            })
        result: dict[str, Any] = {"ok": True, "status": "updated", "title": title,
                                  "previous_visibility": current, "requested_visibility": target,
                                  "actual_visibility": actual, "url": short_url(video_id), **report}
        if warnings:
            result["warnings"] = warnings
        self.history.log_action("set_visibility", video_id, {"from": current, "to": target, "actual": actual})
        return result

    # --- set_thumbnail ------------------------------------------------------------------

    def set_thumbnail(self, video_id: str, image_path: str, confirmation_token: str | None = None) -> dict[str, Any]:
        video_id = parse_video_id(video_id)
        path = validate_thumbnail_file(image_path)
        params = {"video_id": video_id, "file": str(path), "sha256": self.file_sha256(path)}
        if not confirmation_token:
            title = self.client.get_video(video_id, parts="snippet")["snippet"].get("title")
            token = self.confirmations.issue("set_thumbnail", params)
            return {
                "ok": True, "status": "confirmation_required",
                "preview": {"video_id": video_id, "title": title, "thumbnail_file": str(path),
                            "file_size_kb": round(path.stat().st_size / 1024)},
                "confirmation_token": token, "expires_in_seconds": self.confirmations.ttl,
                "next_step": _next_step_for_confirmation("set_thumbnail", token, False),
            }
        self.confirmations.consume(confirmation_token, "set_thumbnail", params)
        # Send a verified private snapshot, so the image sent is exactly the image approved.
        staged = self.staging.stage(path, params["sha256"])
        try:
            resp = self.client.set_thumbnail(video_id, staged)
        finally:
            self.staging.discard(staged)
        thumbs = (resp.get("items") or [{}])[0]
        self.history.log_action("set_thumbnail", video_id, {"file": str(path)})
        return {"ok": True, "status": "updated", "video_id": video_id, "write_accepted": True,
                "accepted_by_youtube": True, "image_independently_verified": False,
                "thumbnail_urls": {k: v.get("url") for k, v in thumbs.items() if isinstance(v, dict)},
                "note": ("YouTube accepted the image (thumbnails.set succeeded). The API offers no way to confirm "
                         "which image is being served: the thumbnail URLs stay the same and YouTube re-encodes the "
                         "image, so it is not compared. It can take a few minutes to appear everywhere; check it in "
                         f"YouTube Studio: {studio_url(video_id)}")}

    # --- read-only tools ----------------------------------------------------------------

    def get_video_status(self, video_id: str) -> dict[str, Any]:
        video_id = parse_video_id(video_id)
        video = self.client.get_video(video_id)
        status = video.get("status", {})
        proc = video.get("processingDetails", {})
        upload_status = status.get("uploadStatus")
        processing = proc.get("processingStatus")
        if upload_status in ("failed", "rejected", "deleted"):
            state = upload_status
        elif processing == "processing" or upload_status == "uploaded":
            state = "processing"
        elif processing in ("failed", "terminated"):
            state = "processing_failed"
        else:
            state = "ready"
        progress = proc.get("processingProgress") or {}
        result: dict[str, Any] = {
            "ok": True, "video_id": video_id, "title": video.get("snippet", {}).get("title"),
            "state": state, "visibility": status.get("privacyStatus"), "upload_status": upload_status,
            "processing_status": processing, "failure_reason": status.get("failureReason"),
            "rejection_reason": status.get("rejectionReason"), "publish_at": status.get("publishAt"),
            "made_for_kids": status.get("madeForKids"), "embeddable": status.get("embeddable"),
            "duration": video.get("contentDetails", {}).get("duration"),
            "url": short_url(video_id), "studio_url": studio_url(video_id),
        }
        snippet = video.get("snippet", {})
        result.update({
            "description": snippet.get("description"),
            "tags": snippet.get("tags") or [],
            "category_id": snippet.get("categoryId"),
            "default_language": snippet.get("defaultLanguage"),
            "default_audio_language": snippet.get("defaultAudioLanguage"),
            "published_at": snippet.get("publishedAt"),
            "thumbnails": {k: v.get("url") for k, v in (snippet.get("thumbnails") or {}).items() if isinstance(v, dict)},
            "self_declared_made_for_kids": status.get("selfDeclaredMadeForKids"),
            "license": status.get("license"),
            "public_stats_viewable": status.get("publicStatsViewable"),
            "contains_synthetic_media": status.get("containsSyntheticMedia"),
            "scheduled": bool(status.get("publishAt")),
        })
        if progress.get("partsTotal"):
            result["processing_percent"] = round(100 * int(progress.get("partsProcessed", 0)) / int(progress["partsTotal"]), 1)
        if progress.get("timeLeftMs"):
            result["processing_seconds_left"] = round(int(progress["timeLeftMs"]) / 1000)
        if state == "processing":
            result["next_step"] = "Still processing. Check again in a minute or two."
        return result

    def get_share_link(self, video_id: str, copy_to_clipboard: bool = False, link_format: str = "short") -> dict[str, Any]:
        video_id = parse_video_id(video_id)
        if link_format not in ("short", "watch"):
            raise invalid("link_format must be 'short' or 'watch'.")
        video = self.client.get_video(video_id, parts="status")
        visibility = video.get("status", {}).get("privacyStatus")
        url = short_url(video_id) if link_format == "short" else watch_url(video_id)
        result: dict[str, Any] = {"ok": True, "video_id": video_id, "url": url, "visibility": visibility,
                                  "copied_to_clipboard": False}
        if copy_to_clipboard:
            result["copied_to_clipboard"] = self.clipboard(url)
        if visibility == "private":
            result["warning"] = "This video is PRIVATE: only the channel owner (and explicitly invited users) can open the link."
        elif visibility == "unlisted":
            result["note"] = "Unlisted: anyone with the link can watch; it does not appear in search."
        return result

    def list_uploads(self, source: str = "youtube", max_results: int = 25, page_token: str | None = None) -> dict[str, Any]:
        max_results = max(1, min(int(max_results), 50))
        if source == "local":
            offset = int(page_token) if page_token and page_token.isdigit() else 0
            rows = self.history.list_uploads(limit=max_results, offset=offset)
            total = self.history.count_uploads()
            items = []
            for r in rows:
                item = {k: r[k] for k in ("upload_id", "created_at", "status", "title", "file_path",
                                          "requested_visibility", "actual_visibility", "video_id")}
                item["progress_percent"] = round(float(r["progress"]) * 100, 1)
                if r["video_id"]:
                    item["url"] = short_url(r["video_id"])
                if r["error"]:
                    item["error"] = r["error"]
                if r["warnings"]:
                    item["warnings"] = r["warnings"]
                items.append(item)
            nxt = offset + len(rows)
            return {"ok": True, "source": "local", "total": total, "items": items,
                    "next_page_token": str(nxt) if nxt < total else None}
        if source != "youtube":
            raise invalid("source must be 'youtube' or 'local'.")

        channel = self.client.get_my_channel()
        playlist = channel.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        if not playlist:
            return {"ok": True, "source": "youtube", "items": [], "next_page_token": None}
        resp = self.client.list_playlist_items(playlist, max_results, page_token)
        entries = resp.get("items") or []
        ids = [e.get("contentDetails", {}).get("videoId") for e in entries if e.get("contentDetails", {}).get("videoId")]
        details = self.client.get_videos(ids, parts="status,processingDetails")
        items = []
        for e in entries:
            vid = e.get("contentDetails", {}).get("videoId")
            st = details.get(vid, {}).get("status", {})
            items.append({
                "video_id": vid, "title": e.get("snippet", {}).get("title"),
                "published_at": e.get("contentDetails", {}).get("videoPublishedAt") or e.get("snippet", {}).get("publishedAt"),
                "visibility": st.get("privacyStatus") or e.get("status", {}).get("privacyStatus"),
                "upload_status": st.get("uploadStatus"),
                "processing_status": details.get(vid, {}).get("processingDetails", {}).get("processingStatus"),
                "url": short_url(vid) if vid else None,
            })
        reported = resp.get("pageInfo", {}).get("totalResults")
        return {"ok": True, "source": "youtube", "channel_title": channel.get("snippet", {}).get("title"),
                "total": reported, "api_reported_total": reported, "returned_count": len(items),
                "has_more": bool(resp.get("nextPageToken")), "items": items,
                "next_page_token": resp.get("nextPageToken")}

    def _total_uploads(self, channel: dict[str, Any]) -> int | None:
        playlist = channel.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        if not playlist:
            return None
        try:
            total = self.client.list_playlist_items(playlist, 1, None).get("pageInfo", {}).get("totalResults")
        except YouTubeMCPError as err:
            log.warning("Could not count uploads: %s", err)
            return None  # unknown, not guessed
        return int(total) if total is not None else None

    def get_channel_info(self) -> dict[str, Any]:
        ch = self.client.get_my_channel()
        snippet, stats, status = ch.get("snippet", {}), ch.get("statistics", {}), ch.get("status", {})
        custom = snippet.get("customUrl")
        return {
            "ok": True, "channel_id": ch.get("id"), "title": snippet.get("title"), "custom_url": custom,
            "url": f"https://www.youtube.com/{custom}" if custom else f"https://www.youtube.com/channel/{ch.get('id')}",
            # statistics.videoCount counts PUBLIC videos only; kept under its old name for compatibility.
            "video_count": int(stats["videoCount"]) if "videoCount" in stats else None,
            "public_video_count": int(stats["videoCount"]) if "videoCount" in stats else None,
            "total_uploads": self._total_uploads(ch),
            "video_count_note": ("video_count/public_video_count is YouTube's public count (statistics.videoCount). "
                                 "total_uploads also includes unlisted and private videos (uploads playlist)."),
            "subscriber_count": None if stats.get("hiddenSubscriberCount") else (
                int(stats["subscriberCount"]) if "subscriberCount" in stats else None),
            "view_count": int(stats["viewCount"]) if "viewCount" in stats else None,
            "long_uploads_status": status.get("longUploadsStatus"),
            "uploads_playlist_id": ch.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads"),
        }
