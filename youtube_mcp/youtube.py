"""Thin wrapper over the YouTube Data API v3, plus input validation helpers.

All network calls go through `_execute`, which retries transient failures and converts
exceptions into `YouTubeMCPError`. The API client is produced by an injectable factory,
which is how tests substitute mocked API responses.
"""

from __future__ import annotations

import mimetypes
import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .config import VISIBILITIES
from .errors import YouTubeMCPError, translate_exception

VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".avi", ".wmv", ".flv", ".webm", ".mkv", ".mpg", ".mpeg",
    ".3gp", ".3gpp", ".mts", ".m2ts", ".ts", ".ogv", ".hevc", ".mxf", ".prores", ".dnxhr",
}
THUMBNAIL_EXTENSIONS = {".jpg", ".jpeg", ".png"}
THUMBNAIL_MAX_BYTES = 2 * 1024 * 1024
MAX_TITLE_CHARS = 100
MAX_DESCRIPTION_BYTES = 5000
MAX_TAGS_CHARS = 500

# Writable fields only; read-only fields (thumbnails, channelId, publishedAt...) must not be sent back.
WRITABLE_SNIPPET_FIELDS = ("title", "description", "tags", "categoryId", "defaultLanguage", "defaultAudioLanguage")
WRITABLE_STATUS_FIELDS = (
    "privacyStatus", "embeddable", "license", "publicStatsViewable", "publishAt",
    "selfDeclaredMadeForKids", "containsSyntheticMedia",
)

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def short_url(video_id: str) -> str:
    return f"https://youtu.be/{video_id}"


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def studio_url(video_id: str) -> str:
    return f"https://studio.youtube.com/video/{video_id}/edit"


# --- Validation -----------------------------------------------------------------------

def invalid(message: str, action: str | None = None, **details: Any) -> YouTubeMCPError:
    return YouTubeMCPError("invalid_input", message, action=action, details=details or None)


def parse_video_id(value: str) -> str:
    """Accept a bare video ID or any common YouTube URL form."""
    value = (value or "").strip()
    if _VIDEO_ID_RE.match(value):
        return value
    try:
        url = urlparse(value if "://" in value else f"https://{value}")
    except ValueError:
        url = None
    if url and url.hostname:
        host = url.hostname.lower().removeprefix("www.").removeprefix("m.")
        candidate = None
        if host == "youtu.be":
            candidate = url.path.strip("/").split("/")[0]
        elif host.endswith("youtube.com"):
            if url.path == "/watch":
                candidate = (parse_qs(url.query).get("v") or [""])[0]
            else:
                parts = [p for p in url.path.split("/") if p]
                if len(parts) >= 2 and parts[0] in ("shorts", "embed", "live", "v", "video"):
                    candidate = parts[1]
        if candidate and _VIDEO_ID_RE.match(candidate):
            return candidate
    raise invalid(f"'{value}' is not a valid YouTube video ID or URL.",
                  "Pass an 11-character video ID (e.g. abcDEF12345) or a youtube.com / youtu.be link.")


def validate_visibility(visibility: str) -> str:
    v = (visibility or "").strip().lower()
    if v not in VISIBILITIES:
        raise invalid(f"Visibility must be one of {', '.join(VISIBILITIES)}; got '{visibility}'.")
    return v


def validate_title(title: str) -> str:
    title = (title or "").strip()
    if not title:
        raise invalid("A title is required.")
    if len(title) > MAX_TITLE_CHARS:
        raise invalid(f"Title is {len(title)} characters; YouTube allows at most {MAX_TITLE_CHARS}.")
    if "<" in title or ">" in title:
        raise invalid("Titles cannot contain '<' or '>'.")
    return title


def validate_description(description: str) -> str:
    description = description or ""
    size = len(description.encode("utf-8"))
    if size > MAX_DESCRIPTION_BYTES:
        raise invalid(f"Description is {size} bytes; YouTube allows at most {MAX_DESCRIPTION_BYTES}.")
    if "<" in description or ">" in description:
        raise invalid("Descriptions cannot contain '<' or '>'.")
    return description


def validate_tags(tags: list[str] | None) -> list[str]:
    cleaned = []
    for tag in tags or []:
        tag = str(tag).strip()
        if not tag:
            continue
        if "," in tag or "<" in tag or ">" in tag:
            raise invalid(f"Tag '{tag}' contains a comma or angle bracket, which YouTube does not allow.")
        cleaned.append(tag)
    # YouTube counts quotes around tags containing spaces, plus a separator between tags.
    total = sum(len(t) + (2 if " " in t else 0) for t in cleaned) + max(len(cleaned) - 1, 0)
    if total > MAX_TAGS_CHARS:
        raise invalid(f"Tags total {total} characters; YouTube allows at most {MAX_TAGS_CHARS}.")
    return cleaned


def validate_category_id(category_id: str) -> str:
    category_id = str(category_id or "").strip()
    if not category_id.isdigit():
        raise invalid(f"Category ID must be numeric (e.g. 22 or 28); got '{category_id}'.")
    return category_id


def _resolve_local_file(path_str: str, kind: str) -> Path:
    if not path_str or not path_str.strip():
        raise invalid(f"A {kind} file path is required.")
    path = Path(path_str.strip()).expanduser()
    if not path.is_absolute():
        raise invalid(f"The {kind} path must be absolute (start with / or ~); got '{path_str}'.")
    path = path.resolve()
    if not path.exists():
        raise YouTubeMCPError("file_not_found", f"No file exists at {path}.",
                              action="Check the path. Exported videos are often in ~/Movies, ~/Documents or ~/Downloads.")
    if not path.is_file():
        raise invalid(f"{path} is not a regular file.")
    if not path.stat().st_size:
        raise invalid(f"{path} is empty (0 bytes).")
    return path


def validate_video_file(path_str: str) -> Path:
    path = _resolve_local_file(path_str, "video")
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        raise invalid(f"'{path.suffix}' is not a recognised video format.",
                      "Export the video as .mp4 or .mov and try again.")
    return path


def validate_thumbnail_file(path_str: str) -> Path:
    path = _resolve_local_file(path_str, "thumbnail")
    if path.suffix.lower() not in THUMBNAIL_EXTENSIONS:
        raise invalid("Thumbnails must be JPEG or PNG files.")
    size = path.stat().st_size
    if size > THUMBNAIL_MAX_BYTES:
        raise invalid(f"Thumbnail is {size / 1024 / 1024:.1f} MB; YouTube allows at most 2 MB.",
                      "Compress or resize it (1280x720 JPEG is recommended).")
    return path


def guess_mimetype(path: Path, default: str) -> str:
    return mimetypes.guess_type(path.name)[0] or default


# --- API client wrapper ------------------------------------------------------------------

class YouTubeClient:
    def __init__(self, service_factory: Callable[[], Any], *, num_retries: int = 3) -> None:
        self._service_factory = service_factory
        self.num_retries = num_retries

    def service(self) -> Any:
        return self._service_factory()

    def _execute(self, request: Any, num_retries: int | None = None) -> dict[str, Any]:
        try:
            return request.execute(num_retries=self.num_retries if num_retries is None else num_retries)
        except Exception as exc:
            raise translate_exception(exc) from None

    def _build(self, fn: Callable[[Any], Any], num_retries: int | None = None) -> dict[str, Any]:
        try:
            request = fn(self.service())
        except YouTubeMCPError:
            raise
        except Exception as exc:
            raise translate_exception(exc) from None
        return self._execute(request, num_retries)

    def _write(self, fn: Callable[[Any], Any], what: str, resource_id: str | None = None) -> dict[str, Any]:
        """Execute a write exactly once.

        The Google client library would otherwise retry on 5xx and connection errors, which can repeat
        a write YouTube already accepted (e.g. create a playlist twice). If the connection fails or the
        server errors after the request was sent, the outcome is unknown: report that instead of guessing.
        """
        try:
            return self._build(fn, num_retries=0)
        except YouTubeMCPError as err:
            if err.code in ("network_error", "youtube_unavailable"):
                raise YouTubeMCPError(
                    "write_outcome_uncertain",
                    f"The connection failed while YouTube was processing the request to {what}. It may or may not "
                    "have been applied.",
                    action=("Do NOT repeat the request. First check the current state with the matching read tool "
                            "(get_video_status, list_playlists, list_playlist_items, list_captions), then decide."),
                    details={"resource_id": resource_id, "cause": err.to_dict()["error"]}) from None
            raise

    # Videos ---------------------------------------------------------------------------

    def get_video(self, video_id: str, parts: str = "snippet,status,processingDetails,contentDetails") -> dict[str, Any]:
        resp = self._build(lambda s: s.videos().list(part=parts, id=video_id))
        items = resp.get("items") or []
        if not items:
            raise YouTubeMCPError(
                "video_not_found",
                f"No video with ID {video_id} is visible to the authorized channel.",
                action="Check the ID; use list_uploads to see this channel's videos. Videos owned by other channels cannot be managed.",
            )
        return items[0]

    def get_videos(self, video_ids: list[str], parts: str = "status,processingDetails") -> dict[str, dict[str, Any]]:
        if not video_ids:
            return {}
        resp = self._build(lambda s: s.videos().list(part=parts, id=",".join(video_ids[:50])))
        return {item["id"]: item for item in resp.get("items") or []}

    def update_video(self, video_id: str, part: str, fields: dict[str, Any]) -> dict[str, Any]:
        body = {"id": video_id, part: fields}
        return self._write(lambda s: s.videos().update(part=part, body=body), f"update video {video_id}", video_id)

    def create_upload_request(self, body: dict[str, Any], path: Path, chunk_size: int,
                              notify_subscribers: bool = False) -> Any:
        from googleapiclient.http import MediaFileUpload

        def make(s: Any) -> Any:
            media = MediaFileUpload(str(path), mimetype=guess_mimetype(path, "application/octet-stream"),
                                    chunksize=chunk_size, resumable=True)
            return s.videos().insert(part="snippet,status", body=body, media_body=media,
                                    notifySubscribers=notify_subscribers)

        try:
            return make(self.service())
        except YouTubeMCPError:
            raise
        except Exception as exc:
            raise translate_exception(exc) from None

    def set_thumbnail(self, video_id: str, path: Path) -> dict[str, Any]:
        from googleapiclient.http import MediaFileUpload

        return self._write(lambda s: s.thumbnails().set(
            videoId=video_id,
            media_body=MediaFileUpload(str(path), mimetype=guess_mimetype(path, "image/jpeg"), resumable=False),
        ), f"set the thumbnail of video {video_id}", video_id)

    # Channel --------------------------------------------------------------------------

    def get_my_channel(self) -> dict[str, Any]:
        resp = self._build(lambda s: s.channels().list(part="snippet,contentDetails,statistics,status", mine=True))
        items = resp.get("items") or []
        if not items:
            raise YouTubeMCPError("no_channel", "The authorized Google account has no YouTube channel.",
                                  action="Create a channel at https://www.youtube.com/create_channel, then re-authorize.")
        return items[0]

    def list_playlist_items(self, playlist_id: str, max_results: int, page_token: str | None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"part": "snippet,contentDetails,status", "playlistId": playlist_id,
                                  "maxResults": max_results}
        if page_token:
            kwargs["pageToken"] = page_token
        return self._build(lambda s: s.playlistItems().list(**kwargs))

    # Captions -------------------------------------------------------------------------

    def list_captions(self, video_id: str, caption_id: str | None = None) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"part": "snippet", "videoId": video_id}
        if caption_id:
            kwargs["id"] = caption_id
        return self._build(lambda s: s.captions().list(**kwargs)).get("items") or []

    def insert_caption(self, snippet: dict[str, Any], path: Path) -> dict[str, Any]:
        from googleapiclient.http import MediaFileUpload

        return self._write(lambda s: s.captions().insert(
            part="snippet", body={"snippet": snippet},
            media_body=MediaFileUpload(str(path), mimetype="application/octet-stream", resumable=False)),
            f"add a caption track to video {snippet.get('videoId')}", snippet.get("videoId"))

    def update_caption(self, caption_id: str, snippet: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
        from googleapiclient.http import MediaFileUpload

        kwargs: dict[str, Any] = {"part": "snippet", "body": {"id": caption_id, "snippet": snippet}}
        if path is not None:
            kwargs["media_body"] = MediaFileUpload(str(path), mimetype="application/octet-stream", resumable=False)
        return self._write(lambda s: s.captions().update(**kwargs), f"update caption track {caption_id}", caption_id)

    def download_caption(self, caption_id: str, tfmt: str | None) -> bytes:
        kwargs: dict[str, Any] = {"id": caption_id}
        if tfmt:
            kwargs["tfmt"] = tfmt
        data: Any = self._build(lambda s: s.captions().download_media(**kwargs))
        return data if isinstance(data, bytes) else str(data).encode("utf-8")

    # Categories -----------------------------------------------------------------------

    def list_video_categories(self, region_code: str, hl: str) -> list[dict[str, Any]]:
        return self._build(lambda s: s.videoCategories().list(part="snippet", regionCode=region_code, hl=hl)).get("items") or []

    # Playlists ------------------------------------------------------------------------

    def list_my_playlists(self, max_results: int, page_token: str | None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"part": "snippet,status,contentDetails", "mine": True, "maxResults": max_results}
        if page_token:
            kwargs["pageToken"] = page_token
        return self._build(lambda s: s.playlists().list(**kwargs))

    def get_playlist(self, playlist_id: str) -> dict[str, Any]:
        resp = self._build(lambda s: s.playlists().list(part="snippet,status,contentDetails", id=playlist_id))
        items = resp.get("items") or []
        if not items:
            raise YouTubeMCPError("playlist_not_found", f"No playlist with ID {playlist_id} was found.",
                                  action="Use list_playlists to see this channel's playlists.")
        return items[0]

    def insert_playlist(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._write(lambda s: s.playlists().insert(part="snippet,status", body=body), "create a playlist")

    def update_playlist(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._write(lambda s: s.playlists().update(part="snippet,status", body=body),
                           f"update playlist {body.get('id')}", body.get("id"))

    def list_playlist_entries(self, playlist_id: str, max_results: int = 50, page_token: str | None = None,
                              video_id: str | None = None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"part": "snippet,contentDetails,status", "playlistId": playlist_id,
                                  "maxResults": max_results}
        if page_token:
            kwargs["pageToken"] = page_token
        if video_id:
            kwargs["videoId"] = video_id
        return self._build(lambda s: s.playlistItems().list(**kwargs))

    def insert_playlist_item(self, body: dict[str, Any]) -> dict[str, Any]:
        pid = body.get("snippet", {}).get("playlistId")
        return self._write(lambda s: s.playlistItems().insert(part="snippet,contentDetails", body=body),
                           f"add a video to playlist {pid}", pid)

    def delete_playlist_item(self, playlist_item_id: str) -> None:
        self._write(lambda s: s.playlistItems().delete(id=playlist_item_id),
                    f"remove playlist item {playlist_item_id}", playlist_item_id)
