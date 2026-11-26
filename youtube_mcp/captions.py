"""Caption (subtitle) tracks: upload_captions, list_captions, update_captions.

API facts (YouTube Data API v3 reference, captions.insert/list/update):
- insert needs snippet.videoId, snippet.language and snippet.name; snippet.isDraft is optional.
  Max file 100 MB. Quota: insert 400, list 50, update 450 units. Scope: youtube.force-ssl.
- update can only change snippet.isDraft and/or replace the caption file. Renaming a track or
  changing its language is not offered by the API (reported as `unsupported_by_youtube`).
- Omitted snippet properties are cleared on update, so the full writable snippet is re-sent.
- The `sync` parameter is deprecated and not used.
The API reference does not list accepted file formats; YouTube's Help Center lists SubRip (.srt)
and WebVTT (.vtt), which are the two formats accepted here and checked locally before upload.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import approval
from .verify import write_report
from .errors import YouTubeMCPError, unsupported_by_youtube
from .youtube import invalid, parse_video_id

if TYPE_CHECKING:
    from .service import YouTubeMCPService

CAPTION_EXTENSIONS = {".srt": "srt", ".vtt": "vtt"}
CAPTION_MAX_BYTES = 100 * 1024 * 1024
MAX_TRACK_NAME_CHARS = 150
_LANG_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
_SRT_CUE = re.compile(r"\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}")
_VTT_CUE = re.compile(r"(?:\d{1,2}:)?\d{2}:\d{2}\.\d{3}\s*-->\s*(?:\d{1,2}:)?\d{2}:\d{2}\.\d{3}")


def validate_language(language: str) -> str:
    lang = (language or "").strip().replace("_", "-")
    if not _LANG_RE.match(lang):
        raise invalid(f"'{language}' is not a valid BCP-47 language code.",
                      "Use codes like 'en', 'en-US', 'hi' or 'pt-BR'.")
    return lang


def validate_track_name(name: str | None) -> str:
    name = (name or "").strip()
    if len(name) > MAX_TRACK_NAME_CHARS:
        raise invalid(f"Track name is {len(name)} characters; YouTube allows at most {MAX_TRACK_NAME_CHARS}.")
    if "<" in name or ">" in name:
        raise invalid("Track names cannot contain '<' or '>'.")
    return name


def validate_caption_file(path_str: str) -> tuple[Path, str, int]:
    """Return (path, format, cue_count) for a well-formed .srt or .vtt file."""
    if not path_str or not path_str.strip():
        raise invalid("A caption file path is required.")
    path = Path(path_str.strip()).expanduser()
    if not path.is_absolute():
        raise invalid(f"The caption file path must be absolute; got '{path_str}'.")
    path = path.resolve()
    if not path.is_file():
        raise YouTubeMCPError("file_not_found", f"No caption file exists at {path}.",
                              action="Check the path to the exported .srt or .vtt file.")
    fmt = CAPTION_EXTENSIONS.get(path.suffix.lower())
    if not fmt:
        raise invalid(f"'{path.suffix}' caption files are not supported here.",
                      "Export captions as SubRip (.srt) or WebVTT (.vtt).")
    size = path.stat().st_size
    if not size:
        raise invalid(f"{path} is empty.")
    if size > CAPTION_MAX_BYTES:
        raise invalid(f"Caption file is {size / 1024 / 1024:.1f} MB; YouTube allows at most 100 MB.")
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError:
        raise invalid("The caption file is not UTF-8 text.", "Re-export or re-save it as UTF-8.") from None
    if fmt == "vtt":
        if not text.lstrip().startswith("WEBVTT"):
            raise invalid("This .vtt file does not start with the required 'WEBVTT' header.")
        cues = len(_VTT_CUE.findall(text))
    else:
        cues = len(_SRT_CUE.findall(text))
    if not cues:
        raise invalid(f"No caption cues (timestamps like 00:00:01{',' if fmt == 'srt' else '.'}000 --> ...) "
                      f"were found in this .{fmt} file.")
    return path, fmt, cues


DOWNLOAD_FORMATS = ("srt", "vtt", "sbv", "ttml", "scc")
MAX_INLINE_CHARS = 200_000
_TAG_RE = re.compile(r"<[^>]+>")


def cue_texts(text: str) -> list[str]:
    """Spoken text of each cue in an SRT or VTT document (numbering, timings and tags removed)."""
    text = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    cues = []
    for block in re.split(r"\n\s*\n", text):
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        arrow = next((n for n, ln in enumerate(lines) if "-->" in ln), None)
        if arrow is None:
            continue  # header, NOTE or STYLE blocks
        spoken = " ".join(" ".join(_TAG_RE.sub("", ln).split()) for ln in lines[arrow + 1:])
        if spoken:
            cues.append(spoken)
    return cues


def _verify_content_preview(requested: bool, *, applies: bool) -> str:
    if not requested:
        return "no (metadata read-back only)"
    if not applies:
        return "requested, but has no effect: no caption file is being replaced"
    return ("yes: after the write, download the track (read-only, ~200 extra quota units) and compare each cue's "
            "text with the file")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _track(item: dict[str, Any]) -> dict[str, Any]:
    sn = item.get("snippet", {})
    track = {
        "caption_id": item.get("id"), "language": sn.get("language"), "name": sn.get("name"),
        "track_kind": sn.get("trackKind"), "is_draft": sn.get("isDraft"), "status": sn.get("status"),
        "last_updated": sn.get("lastUpdated"), "audio_track_type": sn.get("audioTrackType"),
        "is_cc": sn.get("isCC"), "is_auto_synced": sn.get("isAutoSynced"),
    }
    if sn.get("failureReason"):
        track["failure_reason"] = sn["failureReason"]
    return track


class CaptionsService:
    def __init__(self, svc: "YouTubeMCPService") -> None:
        self.svc = svc

    @property
    def client(self) -> Any:
        return self.svc.client

    def list_captions(self, video_id: str) -> dict[str, Any]:
        video_id = parse_video_id(video_id)
        tracks = [_track(i) for i in self.client.list_captions(video_id)]
        return {"ok": True, "video_id": video_id, "count": len(tracks), "tracks": tracks,
                "note": "track_kind 'asr' tracks are YouTube's automatic captions and cannot be edited here."}

    def _existing(self, video_id: str, language: str, name: str) -> dict[str, Any] | None:
        for item in self.client.list_captions(video_id):
            sn = item.get("snippet", {})
            if (sn.get("trackKind") or "").lower() == "asr":
                continue
            if (sn.get("language") or "").lower() == language.lower() and (sn.get("name") or "") == name:
                return item
        return None

    def upload_captions(self, video_id: str, file_path: str, language: str = "en", name: str = "",
                        is_draft: bool = False, confirmation_token: str | None = None,
                        verify_content: bool = False) -> dict[str, Any]:
        video_id = parse_video_id(video_id)
        path, fmt, cues = validate_caption_file(file_path)
        language = validate_language(language)
        name = validate_track_name(name)
        params = {"video_id": video_id, "file": str(path), "sha256": _sha256(path), "language": language,
                  "name": name, "is_draft": bool(is_draft), "verify_content": bool(verify_content)}

        def check_exists() -> None:
            existing = self._existing(video_id, language, name)
            if existing:
                raise YouTubeMCPError(
                    "caption_exists",
                    f"The video already has a '{language}' caption track named '{name}'.",
                    action=("Use update_captions with this caption_id to replace its file, or pick a different "
                            "track name."),
                    details={"caption_id": existing.get("id")})

        video = self.client.get_video(video_id, parts="snippet,status")
        title = video.get("snippet", {}).get("title")
        exposes = not is_draft and video.get("status", {}).get("privacyStatus") == "public"
        if not confirmation_token:
            check_exists()
            return approval.confirmation_required(
                self.svc.confirmations, "upload_captions", params,
                {"video_id": video_id, "video_title": title, "file": str(path), "format": fmt, "cue_count": cues,
                 "language": language, "track_name": name or "(none)", "is_draft": bool(is_draft),
                 "verify_content": _verify_content_preview(bool(verify_content), applies=True),
                 "effect": "Adds a new subtitle track. Draft tracks are not shown to viewers."})
        approval.consume(self.svc.confirmations, "upload_captions", confirmation_token, params)
        if exposes:
            self.svc.confirm_public("Publish a caption track on a PUBLIC video", [
                ("Video title", title), ("Language", language), ("Track name", name or "(default)"),
                ("File", path.name)])
        check_exists()
        snippet = {"videoId": video_id, "language": language, "name": name, "isDraft": bool(is_draft)}
        # Send (and later compare against) a verified private snapshot of the approved file.
        staged = self.svc.staging.stage(path, params["sha256"])
        try:
            return self._finish_upload(video_id, snippet, staged, path, is_draft, exposes, verify_content)
        finally:
            self.svc.staging.discard(staged)

    def _finish_upload(self, video_id: str, snippet: dict[str, Any], staged: Path, path: Path, is_draft: bool,
                       exposes: bool, verify_content: bool) -> dict[str, Any]:
        language, name = snippet["language"], snippet["name"]
        created = self.client.insert_caption(snippet, staged)
        caption_id = created.get("id")
        self.svc.history.log_action("upload_captions", video_id, {"caption_id": caption_id, "file": str(path),
                                                                  "language": language, "name": name})
        ver = self.svc.verifier.wait_for(
            lambda: self.client.list_captions(video_id, caption_id),
            lambda items: any(i.get("id") == caption_id and i["snippet"].get("isDraft", False) == bool(is_draft)
                              for i in items))
        track = _track(ver.observed[0]) if ver.observed else _track(created)
        result = {"ok": True, "status": "uploaded", "track": track,
                  **write_report(ver, resource={"video_id": video_id, "caption_id": caption_id},
                                 check_with="list_captions", manual_attention=exposes),
                  "note": ("Verification covers the track's metadata. YouTube processes new tracks for a short "
                           "while (status 'syncing' before 'serving'). Use download_captions or verify_content=true "
                           "to compare the caption text.")}
        if verify_content and caption_id:
            result["content_verification"] = self._compare_content(caption_id, staged)
        return result

    def update_captions(self, video_id: str, caption_id: str, file_path: str | None = None,
                        is_draft: bool | None = None, name: str | None = None, language: str | None = None,
                        confirmation_token: str | None = None, verify_content: bool = False) -> dict[str, Any]:
        video_id = parse_video_id(video_id)
        caption_id = (caption_id or "").strip()
        if not caption_id:
            raise invalid("caption_id is required.", "Use list_captions to find it.")
        if name is not None or language is not None:
            raise unsupported_by_youtube(
                "YouTube's API cannot rename a caption track or change its language (captions.update only "
                "changes the draft status and/or replaces the caption file).",
                action="Upload a new track with the desired language/name using upload_captions instead.")
        if file_path is None and is_draft is None:
            raise invalid("Nothing to update.", "Pass file_path (to replace the captions) and/or is_draft.")
        path = None
        fmt, cues = None, None
        if file_path is not None:
            path, fmt, cues = validate_caption_file(file_path)

        current_items = self.client.list_captions(video_id, caption_id)
        if not current_items:
            raise YouTubeMCPError("caption_not_found", f"No caption track {caption_id} on video {video_id}.",
                                  action="Use list_captions to see the video's caption tracks.")
        current = current_items[0]["snippet"]
        if (current.get("trackKind") or "").lower() == "asr":
            raise unsupported_by_youtube("Automatic (ASR) caption tracks cannot be updated through the API.")
        new_draft = current.get("isDraft", False) if is_draft is None else bool(is_draft)
        params = {"video_id": video_id, "caption_id": caption_id, "file": str(path) if path else None,
                  "sha256": _sha256(path) if path else None, "is_draft": new_draft,
                  "verify_content": bool(verify_content)}

        if not confirmation_token:
            preview: dict[str, Any] = {"video_id": video_id, "caption_id": caption_id,
                                       "language": current.get("language"), "track_name": current.get("name"),
                                       "is_draft": {"before": current.get("isDraft"), "after": new_draft},
                                       "verify_content": _verify_content_preview(bool(verify_content),
                                                                                 applies=path is not None)}
            if path:
                preview.update({"replace_file_with": str(path), "format": fmt, "cue_count": cues})
            return approval.confirmation_required(self.svc.confirmations, "update_captions", params, preview)
        approval.consume(self.svc.confirmations, "update_captions", confirmation_token, params)
        if not new_draft and (path is not None or current.get("isDraft")):
            video = self.client.get_video(video_id, parts="snippet,status")
            if video.get("status", {}).get("privacyStatus") == "public":
                self.svc.confirm_public("Publish caption changes on a PUBLIC video", [
                    ("Video title", video.get("snippet", {}).get("title")), ("Language", current.get("language")),
                    ("Track name", current.get("name") or "(default)"),
                    ("Change", "replace caption file" if path else "publish draft track")])

        snippet = {"videoId": video_id, "language": current.get("language"), "name": current.get("name", ""),
                   "isDraft": new_draft}
        before_updated = current.get("lastUpdated")
        staged = self.svc.staging.stage(path, params["sha256"]) if path is not None else None
        try:
            return self._finish_update(video_id, caption_id, snippet, staged, path, new_draft, before_updated,
                                       verify_content)
        finally:
            if staged is not None:
                self.svc.staging.discard(staged)

    def _finish_update(self, video_id: str, caption_id: str, snippet: dict[str, Any], staged: Path | None,
                       path: Path | None, new_draft: bool, before_updated: str | None,
                       verify_content: bool) -> dict[str, Any]:
        self.client.update_caption(caption_id, snippet, staged)
        self.svc.history.log_action("update_captions", video_id, {"caption_id": caption_id,
                                                                  "file": str(path) if path else None,
                                                                  "is_draft": new_draft})

        def applied(items: list[dict[str, Any]]) -> bool:
            sn = items[0]["snippet"] if items else {}
            if not items or sn.get("isDraft", False) != new_draft:
                return False
            # A replaced file shows up as a new lastUpdated (when YouTube reports it).
            return path is None or not before_updated or sn.get("lastUpdated") != before_updated

        ver = self.svc.verifier.wait_for(lambda: self.client.list_captions(video_id, caption_id), applied)
        result = {"ok": True, "status": "updated", "track": _track(ver.observed[0]) if ver.observed else None,
                  **write_report(ver, resource={"video_id": video_id, "caption_id": caption_id},
                                 check_with="list_captions")}
        if verify_content and staged is not None:
            result["content_verification"] = self._compare_content(caption_id, staged)
        return result

    # read-only download --------------------------------------------------------------

    def _compare_content(self, caption_id: str, path: Path) -> dict[str, Any]:
        try:
            remote = self.client.download_caption(caption_id, "srt").decode("utf-8-sig", errors="replace")
        except YouTubeMCPError as err:
            return {"status": "unverifiable", "reason": err.to_dict()["error"],
                    "note": "New tracks can take a while to process; try download_captions later."}
        local = cue_texts(path.read_text(encoding="utf-8-sig"))
        got = cue_texts(remote)
        return {"status": "matches" if local == got else "differs", "local_cue_count": len(local),
                "youtube_cue_count": len(got),
                "note": "Compares the spoken text of each cue; YouTube may re-time cues, so timings are not compared.",
                "quota_cost_units": 200}

    def download_captions(self, video_id: str, caption_id: str, fmt: str = "srt") -> dict[str, Any]:
        """Read-only: return a caption track's text (captions.download, 200 quota units)."""
        video_id = parse_video_id(video_id)
        caption_id = (caption_id or "").strip()
        fmt = (fmt or "srt").strip().lower()
        if fmt not in DOWNLOAD_FORMATS:
            raise invalid(f"format must be one of: {', '.join(DOWNLOAD_FORMATS)}.")
        if not caption_id:
            raise invalid("caption_id is required.", "Use list_captions to find it.")
        items = self.client.list_captions(video_id, caption_id)  # also proves the track belongs to this video
        if not items:
            raise YouTubeMCPError("caption_not_found", f"No caption track {caption_id} on video {video_id}.",
                                  action="Use list_captions to see the video's caption tracks.")
        try:
            data = self.client.download_caption(caption_id, fmt)
        except YouTubeMCPError as err:
            if err.code == "permission_denied":
                raise unsupported_by_youtube(
                    "YouTube did not allow this caption track to be downloaded (403). This can apply to some "
                    "tracks, such as automatic captions; it is not an error in this server.",
                    action="Download it from YouTube Studio instead.", youtube_error=err.to_dict()["error"]) from None
            raise
        text = data.decode("utf-8-sig", errors="replace")
        truncated = len(text) > MAX_INLINE_CHARS
        return {"ok": True, "video_id": video_id, "caption_id": caption_id, "format": fmt,
                "track": _track(items[0]), "cue_count": len(cue_texts(text)) if fmt in ("srt", "vtt") else None,
                "content": text[:MAX_INLINE_CHARS], "truncated": truncated, "characters": len(text),
                "quota_cost_units": 250}
