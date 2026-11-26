"""Structured, actionable errors and translation of Google API exceptions.

Every failure surfaced to the MCP client is a `YouTubeMCPError` with a stable `code`,
a plain-English `message`, an `action` telling the user what to do next, and a
`retryable` flag. All text passes through `redact()` so tokens can never leak.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import ssl
from typing import Any

from .config import cli_command

_SECRET_PATTERNS = [
    re.compile(r"ya29\.[0-9A-Za-z_\-\.]+"),          # OAuth access tokens
    re.compile(r"1//[0-9A-Za-z_\-]{10,}"),            # OAuth refresh tokens
    re.compile(r"GOCSPX-[0-9A-Za-z_\-]+"),            # OAuth client secrets
    re.compile(r"(?i)(\"?(?:access_token|refresh_token|client_secret|token)\"?\s*[:=]\s*\"?)[^\s\",}]+"),
    re.compile(r"(?i)(authorization:\s*bearer\s+)\S+"),
]


def redact(text: str) -> str:
    """Remove anything that looks like an OAuth token or client secret."""
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            text = pattern.sub(lambda m: m.group(1) + "[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    return text


class RedactingFilter(logging.Filter):
    """Logging filter that scrubs secrets from every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(record.getMessage())
            record.args = None
        except Exception:  # pragma: no cover - never break logging
            pass
        return True


class RedactingFormatter(logging.Formatter):
    """Formatter that redacts the fully formatted record, including exception tracebacks."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def authorize_command() -> str:
    """The exact shell command that (re)authorizes this server."""
    return " ".join(f'"{part}"' if "/" in part or " " in part else part
                    for part in cli_command("youtube_mcp.authorize", "authorize.py"))


class YouTubeMCPError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        action: str | None = None,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = redact(message)
        self.action = redact(action) if action else None
        self.retryable = retryable
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message, "retryable": self.retryable}
        if self.action:
            error["action"] = self.action
        if self.details:
            error["details"] = json.loads(redact(json.dumps(self.details, default=str)))
        return {"ok": False, "error": error}

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


# --- Common constructors -------------------------------------------------------------

def not_authorized(token_path: Any) -> YouTubeMCPError:
    return YouTubeMCPError(
        "not_authorized",
        "The YouTubeMCP server has not been authorized yet (no token file found).",
        action=f"Ask the user to run this command in Terminal, then retry: {authorize_command()}",
        details={"expected_token_file": str(token_path)},
    )


def auth_expired(reason: str = "") -> YouTubeMCPError:
    return YouTubeMCPError(
        "auth_expired",
        "The saved YouTube authorization has expired or was revoked"
        + (f" ({reason})" if reason else "")
        + ". Google expires refresh tokens after 7 days while the OAuth consent screen is in Testing mode.",
        action=f"Ask the user to re-authorize by running in Terminal: {authorize_command()}",
    )


def insufficient_scope(missing: list[str]) -> YouTubeMCPError:
    return YouTubeMCPError(
        "insufficient_scope",
        "The saved authorization does not include all permissions this server needs.",
        action=f"Ask the user to re-authorize by running in Terminal: {authorize_command()}",
        details={"missing_scopes": missing},
    )


def unsupported_by_youtube(message: str, action: str | None = None, **details: Any) -> YouTubeMCPError:
    """YouTube itself does not offer this (a platform/API limitation, not a bug in this server)."""
    return YouTubeMCPError("unsupported_by_youtube", message, action=action, details=details or None)


def analytics_not_enabled(token_path: Any = None) -> YouTubeMCPError:
    return YouTubeMCPError(
        "analytics_not_enabled",
        "YouTube Analytics is not enabled for this server yet: the saved authorization does not include the "
        "optional read-only analytics permission. This is a setup step, not an error in the server.",
        action=("Enable the 'YouTube Analytics API' in Google Cloud Console, add the yt-analytics.readonly scope "
                "to the OAuth consent screen, then ask the user to run in Terminal: "
                f"{authorize_command()} --analytics  (see README 'YouTube Analytics setup')."),
    )


# --- Google API error translation -----------------------------------------------------

_REASON_MAP: dict[str, tuple[str, str, str | None, bool]] = {
    # reason: (code, message, action, retryable)
    "quotaExceeded": ("quota_exceeded", "The YouTube API daily quota for this Google Cloud project is used up.",
                      "Wait until the quota resets (midnight Pacific Time) or request a quota increase in Google Cloud Console.", False),
    "dailyLimitExceeded": ("quota_exceeded", "The YouTube API daily limit has been reached.",
                           "Wait until the quota resets (midnight Pacific Time).", False),
    "rateLimitExceeded": ("rate_limited", "Too many requests were sent to YouTube in a short time.",
                          "Wait a minute and try again.", True),
    "userRateLimitExceeded": ("rate_limited", "Too many requests were sent to YouTube in a short time.",
                              "Wait a minute and try again.", True),
    "uploadLimitExceeded": ("upload_limit_exceeded", "This YouTube channel has reached its upload limit for now.",
                            "Try again in 24 hours.", False),
    "videoNotFound": ("video_not_found", "No video with that ID was found on this channel.",
                      "Check the video ID. Use list_uploads to see this channel's videos.", False),
    "insufficientPermissions": ("insufficient_scope", "The authorization is missing a required permission.",
                                None, False),
    "ACCESS_TOKEN_SCOPE_INSUFFICIENT": ("insufficient_scope", "The authorization is missing a required permission.",
                                        None, False),
    "authError": ("auth_expired", "YouTube rejected the saved authorization.", None, False),
    "invalidTitle": ("invalid_metadata", "YouTube rejected the video title.",
                     "Use a title of 1-100 characters without '<' or '>'.", False),
    "invalidDescription": ("invalid_metadata", "YouTube rejected the video description.",
                           "Keep the description under 5000 bytes and avoid '<' or '>'.", False),
    "invalidTags": ("invalid_metadata", "YouTube rejected the tags.",
                    "Keep total tag length under 500 characters and avoid commas inside tags.", False),
    "invalidCategoryId": ("invalid_metadata", "YouTube rejected the category ID.",
                          "Use a valid YouTube category ID such as 22 (People & Blogs) or 28 (Science & Technology).", False),
    "invalidVideoMetadata": ("invalid_metadata", "YouTube rejected the video metadata.", None, False),
    "invalidPublishAt": ("invalid_metadata", "YouTube rejected the scheduled publish time.", None, False),
    "mediaBodyRequired": ("invalid_request", "The upload did not include video content.", None, False),
    "uploadNotAllowed": ("upload_not_allowed", "YouTube does not allow this account to upload right now.",
                         "Open YouTube Studio to check the channel's status and any pending verification.", False),
    "youtubeSignupRequired": ("no_channel", "This Google account has no YouTube channel.",
                              "Create a YouTube channel for this account, then re-authorize.", False),
    "captionExists": ("caption_exists", "A caption track with this language and name already exists on the video.",
                      "Use update_captions to replace that track's file, or choose a different track name.", False),
    "nameTooLong": ("invalid_metadata", "The caption track name is too long (max 150 characters).", None, False),
    "captionNotFound": ("caption_not_found", "No caption track with that ID was found on this video.",
                        "Use list_captions to see the video's caption tracks.", False),
    "playlistNotFound": ("playlist_not_found", "No playlist with that ID was found for this channel.",
                         "Use list_playlists to see this channel's playlists.", False),
    "playlistItemNotFound": ("playlist_item_not_found", "That playlist item was not found.",
                             "Use list_playlist_items to see the playlist's current items.", False),
    "manualSortRequired": ("unsupported_by_youtube",
                           "YouTube only accepts a position when the playlist is sorted manually.",
                           "Omit position, or set the playlist's ordering to Manual in YouTube Studio.", False),
    "playlistContainsMaximumNumberOfVideos": ("playlist_full", "The playlist has reached YouTube's maximum size.",
                                              None, False),
    "accessNotConfigured": ("api_not_enabled", "The required Google API is not enabled for this Cloud project.",
                            "Enable it in Google Cloud Console > APIs & Services > Library, wait a few minutes, retry.", False),
    "SERVICE_DISABLED": ("api_not_enabled", "The required Google API is not enabled for this Cloud project.",
                         "Enable it in Google Cloud Console > APIs & Services > Library, wait a few minutes, retry.", False),
    "channelNotFound": ("no_channel", "This Google account has no YouTube channel.",
                        "Create a YouTube channel for this account, then re-authorize.", False),
}


def _parse_http_error(err: Any) -> tuple[int, str, str]:
    status = int(getattr(getattr(err, "resp", None), "status", 0) or 0)
    reason, message = "", ""
    try:
        payload = json.loads(err.content.decode("utf-8") if isinstance(err.content, bytes) else err.content)
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        if isinstance(error, dict):
            message = str(error.get("message", ""))
            errors = error.get("errors") or []
            if errors and isinstance(errors[0], dict):
                reason = str(errors[0].get("reason", ""))
            for detail in error.get("details") or []:
                if isinstance(detail, dict) and detail.get("reason") in ("ACCESS_TOKEN_SCOPE_INSUFFICIENT",
                                                                          "SERVICE_DISABLED"):
                    reason = detail["reason"]
        elif isinstance(error, str):
            reason = error
            message = str(payload.get("error_description", ""))
    except Exception:
        pass
    return status, reason, message


def translate_http_error(err: Any) -> YouTubeMCPError:
    status, reason, api_message = _parse_http_error(err)
    details = {"http_status": status, "youtube_reason": reason or None, "youtube_message": api_message or None}

    if reason in _REASON_MAP:
        code, message, action, retryable = _REASON_MAP[reason]
        if code in ("insufficient_scope", "auth_expired"):
            action = f"Ask the user to re-authorize by running in Terminal: {authorize_command()}"
        return YouTubeMCPError(code, message, action=action, retryable=retryable, details=details)

    if status == 403 and "thumbnail" in api_message.lower():
        return YouTubeMCPError(
            "thumbnail_not_allowed",
            "This channel is not allowed to set custom thumbnails.",
            action="Verify the channel at https://www.youtube.com/verify (phone verification), then retry.",
            details=details,
        )
    if status == 401:
        return auth_expired()
    if status == 403:
        return YouTubeMCPError("permission_denied", api_message or "YouTube denied the request.",
                               action="Make sure the video belongs to the authorized channel.", details=details)
    if status == 404:
        return YouTubeMCPError("not_found", api_message or "The requested YouTube resource was not found.",
                               action="Check the ID and try again.", details=details)
    if status == 400:
        return YouTubeMCPError("invalid_request", api_message or "YouTube rejected the request as invalid.",
                               action="Check the parameters and try again.", details=details)
    if status == 429:
        return YouTubeMCPError("rate_limited", "Too many requests were sent to YouTube.",
                               action="Wait a minute and try again.", retryable=True, details=details)
    if status >= 500:
        return YouTubeMCPError("youtube_unavailable", "YouTube returned a temporary server error.",
                               action="Try again in a few minutes.", retryable=True, details=details)
    return YouTubeMCPError("youtube_api_error", api_message or f"YouTube API error (HTTP {status}).",
                           details=details)


def translate_exception(exc: BaseException) -> YouTubeMCPError:
    """Convert any exception raised while talking to Google into a YouTubeMCPError."""
    if isinstance(exc, YouTubeMCPError):
        return exc

    from google.auth.exceptions import RefreshError, TransportError
    from googleapiclient.errors import HttpError, ResumableUploadError

    if isinstance(exc, RefreshError):
        return auth_expired("refresh rejected by Google")
    if isinstance(exc, (HttpError, ResumableUploadError)):
        return translate_http_error(exc)

    try:
        import httplib2
        network_types: tuple[type[BaseException], ...] = (
            TransportError, ConnectionError, TimeoutError, socket.timeout, socket.gaierror, ssl.SSLError,
            httplib2.HttpLib2Error,
        )
    except ImportError:  # pragma: no cover
        network_types = (TransportError, ConnectionError, TimeoutError, socket.timeout, ssl.SSLError)

    if isinstance(exc, network_types) or isinstance(exc, OSError) and getattr(exc, "errno", None) in (
        50, 51, 54, 60, 61, 64, 65,  # macOS network errnos: down, unreachable, reset, timed out, refused...
    ):
        return YouTubeMCPError(
            "network_error",
            f"Could not reach YouTube: {type(exc).__name__}: {exc}",
            action="Check the internet connection and try again.",
            retryable=True,
        )

    return YouTubeMCPError(
        "internal_error",
        f"Unexpected error: {type(exc).__name__}: {exc}",
        action="Check the server logs (stderr) for details.",
    )
