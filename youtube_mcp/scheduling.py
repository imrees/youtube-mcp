"""Scheduled publishing: schedule_publish, get_publish_schedule, cancel_publish_schedule.

YouTube rules (videos resource, status.publishAt):
- publishAt "can be set only if the privacy status of the video is private" and "the video has
  never been published"; the update must also send privacyStatus=private.
- At publishAt the video becomes PUBLIC. A time in the past publishes it right away, so this
  server requires the time to be at least MIN_LEAD_MINUTES in the future, checked both when the
  preview is made and again immediately before the API request.
- The reference does not document unscheduling; cancellation re-sends the status part without
  publishAt (the update replaces the whole part), keeps the video private, and is verified by
  reading the video back.

Safety: this server never changes a video's visibility to make it eligible. An unlisted or
public video is refused with `unsupported_by_youtube`; nothing is modified.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import approval
from .verify import write_report
from .errors import YouTubeMCPError, unsupported_by_youtube
from .youtube import WRITABLE_STATUS_FIELDS, invalid, parse_video_id, studio_url, watch_url

if TYPE_CHECKING:
    from .service import YouTubeMCPService

MIN_LEAD_MINUTES = 10


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def resolve_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo((name or "").strip())
    except (ZoneInfoNotFoundError, ValueError):
        raise invalid(f"'{name}' is not a known IANA timezone.", "Use names like America/New_York or Europe/London.") from None


def parse_publish_time(value: str, tz: ZoneInfo) -> datetime:
    """Parse ISO 8601; a time without an offset is interpreted in `tz`. Returns an aware UTC datetime."""
    text = (value or "").strip()
    if not text:
        raise invalid("publish_at is required, e.g. '2026-12-01 18:30' or '2026-12-01T18:30:00+05:30'.")
    try:
        dt = datetime.fromisoformat(text.replace(" ", "T", 1) if "T" not in text else text)
    except ValueError:
        raise invalid(f"'{value}' is not an ISO 8601 date-time.",
                      "Use e.g. '2026-12-01 18:30' (in the given timezone) or '2026-12-01T13:00:00Z'.") from None
    if "T" not in text and " " not in text and ":" not in text:
        raise invalid(f"'{value}' has no time of day.", "Include a time, e.g. '2026-12-01 18:30'.")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
        # Reject wall-clock times that do not exist in this timezone (DST spring-forward gap).
        if dt.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) != dt.replace(tzinfo=None):
            raise invalid(f"{text} does not exist in {tz} (daylight-saving clock change).",
                          "Choose a time outside the skipped hour.")
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def _parse_api_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).replace(microsecond=0)


def _fmt(dt: datetime, tz: ZoneInfo) -> dict[str, str]:
    local = dt.astimezone(tz)
    return {"utc": dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "local": local.strftime("%Y-%m-%d %H:%M %Z (UTC%z)"), "timezone": str(tz)}


class SchedulingService:
    def __init__(self, svc: "YouTubeMCPService", now: Callable[[], datetime] = _utc_now) -> None:
        self.svc = svc
        self.now = now

    @property
    def client(self) -> Any:
        return self.svc.client

    def _tz(self, name: str | None) -> ZoneInfo:
        return resolve_timezone(name or self.svc.settings.default_timezone)

    def _check_lead(self, when: datetime) -> None:
        earliest = self.now() + timedelta(minutes=MIN_LEAD_MINUTES)
        if when < earliest:
            raise YouTubeMCPError(
                "invalid_schedule",
                f"The publish time must be at least {MIN_LEAD_MINUTES} minutes in the future "
                "(YouTube publishes immediately if the time has passed).",
                action="Choose a later time and request a new preview.",
                details={"requested_utc": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "now_utc": self.now().strftime("%Y-%m-%dT%H:%M:%SZ")})

    def _eligible_video(self, video_id: str) -> dict[str, Any]:
        video = self.client.get_video(video_id, parts="snippet,status")
        status = video.get("status", {})
        current = status.get("privacyStatus")
        if status.get("uploadStatus") in ("failed", "rejected", "deleted"):
            raise invalid(f"This video's upload status is '{status['uploadStatus']}', so it cannot be scheduled.")
        if current != "private":
            raise unsupported_by_youtube(
                f"YouTube only allows scheduled publishing for PRIVATE videos that have never been published. "
                f"This video is {current}. It was not changed.",
                action=("This server never changes visibility to make a video eligible. Scheduling needs a private, "
                        "never-published video (for example, upload it as private)."),
                video_id=video_id, current_visibility=current)
        return video

    # ------------------------------------------------------------------------------

    def get_publish_schedule(self, video_id: str, timezone_name: str | None = None) -> dict[str, Any]:
        video_id = parse_video_id(video_id)
        tz = self._tz(timezone_name)
        video = self.client.get_video(video_id, parts="snippet,status")
        status = video.get("status", {})
        at = _parse_api_time(status.get("publishAt"))
        result: dict[str, Any] = {"ok": True, "video_id": video_id, "title": video.get("snippet", {}).get("title"),
                                  "visibility": status.get("privacyStatus"), "scheduled": at is not None}
        if at:
            result["publish_at"] = _fmt(at, tz)
            result["note"] = "At this time YouTube will change the video from private to PUBLIC."
        return result

    def schedule_publish(self, video_id: str, publish_at: str, timezone_name: str | None = None,
                         confirmation_token: str | None = None, confirm_public: bool = False) -> dict[str, Any]:
        video_id = parse_video_id(video_id)
        tz = self._tz(timezone_name)
        when = parse_publish_time(publish_at, tz)
        self._check_lead(when)
        video = self._eligible_video(video_id)
        existing = _parse_api_time(video["status"].get("publishAt"))
        if existing == when:
            return {"ok": True, "status": "no_change", "video_id": video_id, "publish_at": _fmt(when, tz)}
        utc = when.strftime("%Y-%m-%dT%H:%M:%SZ")
        params = {"video_id": video_id, "publish_at_utc": utc}
        if not confirmation_token:
            preview = {"video_id": video_id, "title": video.get("snippet", {}).get("title"),
                       "current_visibility": "private", "publish_at": _fmt(when, tz),
                       "effect": "The video stays private until then, and YouTube makes it PUBLIC at this time.",
                       "url": watch_url(video_id)}
            if existing:
                preview["replaces_existing_schedule"] = _fmt(existing, tz)
            return approval.confirmation_required(
                self.svc.confirmations, "schedule_publish", params, preview, public=True,
                warnings=[f"This schedules PUBLIC publication at {_fmt(when, tz)['local']}."])
        approval.consume(self.svc.confirmations, "schedule_publish", confirmation_token, params,
                         public=True, confirm_public=confirm_public)

        # Re-validate immediately before the write: time may have passed and the video may have changed.
        self._check_lead(when)
        video = self._eligible_video(video_id)
        self.svc.confirm_public("Schedule a video to become PUBLIC", [
            ("Video title", video.get("snippet", {}).get("title")), ("Video ID", video_id),
            ("Publishes at", _fmt(when, tz)["local"]), ("UTC", utc)])
        self._check_lead(when)  # the dialog may have taken a while: validate the time once more
        fields = {k: video["status"][k] for k in WRITABLE_STATUS_FIELDS if k in video["status"]}
        fields.update({"privacyStatus": "private", "publishAt": utc})
        try:
            self.client.update_video(video_id, "status", fields)
        except YouTubeMCPError as err:
            if err.code in ("invalid_metadata", "invalid_request", "permission_denied", "youtube_api_error"):
                raise unsupported_by_youtube(
                    "YouTube refused to schedule this video. YouTube only allows scheduling private videos "
                    "that have never been published (a video that was ever public or unlisted may not qualify).",
                    action=f"Nothing was changed. You can check the video in YouTube Studio: {studio_url(video_id)}",
                    youtube_error=err.to_dict()["error"]) from None
            raise

        ver = self.svc.verifier.wait_for(
            lambda: self.client.get_video(video_id, parts="status")["status"],
            lambda st: _parse_api_time(st.get("publishAt")) == when and st.get("privacyStatus") == "private")
        observed = ver.observed or {}
        self.svc.history.log_action("schedule_publish", video_id, {"publish_at": utc, "verified": ver.confirmed})
        result: dict[str, Any] = {
            "ok": True, "status": "scheduled" if ver.confirmed else "accepted_unverified",
            "publish_at": _fmt(when, tz), "visibility_now": observed.get("privacyStatus"),
            "observed_publish_at": observed.get("publishAt"),
            **write_report(ver, resource={"video_id": video_id}, check_with="get_publish_schedule",
                           manual_attention=True)}
        if not ver.confirmed:
            result["studio_url"] = studio_url(video_id)
        return result

    def cancel_publish_schedule(self, video_id: str, confirmation_token: str | None = None,
                                timezone_name: str | None = None) -> dict[str, Any]:
        video_id = parse_video_id(video_id)
        tz = self._tz(timezone_name)
        video = self.client.get_video(video_id, parts="snippet,status")
        status = video.get("status", {})
        at = _parse_api_time(status.get("publishAt"))
        if at is None:
            return {"ok": True, "status": "no_change", "video_id": video_id,
                    "message": "This video has no scheduled publish time."}
        params = {"video_id": video_id, "cancel_publish_at_utc": at.strftime("%Y-%m-%dT%H:%M:%SZ")}
        if not confirmation_token:
            return approval.confirmation_required(
                self.svc.confirmations, "cancel_publish_schedule", params,
                {"video_id": video_id, "title": video.get("snippet", {}).get("title"),
                 "cancel_publish_at": _fmt(at, tz), "visibility_after": "private (unchanged)"})
        approval.consume(self.svc.confirmations, "cancel_publish_schedule", confirmation_token, params)

        status = self.client.get_video(video_id, parts="status")["status"]
        fields = {k: status[k] for k in WRITABLE_STATUS_FIELDS if k in status and k != "publishAt"}
        fields["privacyStatus"] = "private"
        self.client.update_video(video_id, "status", fields)
        ver = self.svc.verifier.wait_for(
            lambda: self.client.get_video(video_id, parts="status")["status"],
            lambda st: not st.get("publishAt") and st.get("privacyStatus") == "private")
        observed = ver.observed or {}
        self.svc.history.log_action("cancel_publish_schedule", video_id, {**params, "verified": ver.confirmed})
        result: dict[str, Any] = {
            "ok": True, "status": "cancelled" if ver.confirmed else "accepted_unverified",
            "visibility_now": observed.get("privacyStatus"), "observed_publish_at": observed.get("publishAt"),
            **write_report(ver, resource={"video_id": video_id}, check_with="get_publish_schedule",
                           manual_attention=True)}
        if not ver.confirmed:
            result["studio_url"] = studio_url(video_id)
        return result
