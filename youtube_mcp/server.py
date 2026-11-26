"""MCP server definition (stdio transport). Logs go to stderr; stdout is reserved for MCP."""

from __future__ import annotations

import functools
import json
import logging
import sys
import time
from typing import Annotated, Any, Awaitable, Callable

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from . import __version__
from . import history as h
from .analytics import AnalyticsClient
from .auth import CredentialManager
from .config import Settings
from .confirmations import ConfirmationStore
from .errors import RedactingFilter, RedactingFormatter, translate_exception
from .history import History
from .macos import copy_to_clipboard, notify
from .service import YouTubeMCPService
from .uploads import UploadManager
from .youtube import YouTubeClient

log = logging.getLogger("youtube_mcp")

INSTRUCTIONS = """\
Upload and manage videos on the user's own YouTube channel.

Safety rules (always follow):
- New uploads default to UNLISTED. Never choose public unless the user explicitly asks for public.
- upload_video, update_metadata, set_thumbnail and set_visibility are two-step: the first call returns a
  preview and a confirmation_token. Show the preview to the user and wait for their explicit
  approval before calling again with the token. Never approve on the user's behalf.
- confirm_public=true may only be passed when the user has explicitly approved making the video public.
  Every action that makes content public also shows a native macOS dialog that only the user can
  answer; if they cancel or it times out, the action is refused (native_confirmation_*).
- Every write tool (including update_metadata and all set_visibility changes) is two-step.
- Errors are JSON with code, message and action. Relay the action to the user (for example, a
  Terminal command to re-authorize) rather than retrying blindly.
- Long uploads continue in the background; poll get_upload_progress with the upload_id.
- Captions, playlist changes and publish schedules use the same two-step approval. schedule_publish
  always makes the video PUBLIC at the chosen time, so it also needs confirm_public=true.
- Error code unsupported_by_youtube means YouTube itself does not allow it (not a server bug);
  analytics_not_enabled / api_not_enabled are setup steps the user has to complete.
- Analytics dates are Pacific Time and lag 48-72 hours.
- Write results include write_accepted, verified and verification.status (confirmed / not_confirmed /
  unverifiable). If a change is not yet verified, NEVER repeat it: YouTube is often slow to show
  changes. Check again later with the read tool named in the warning. write_outcome_uncertain means
  the connection failed mid-write: check the state before doing anything else.
- upload_outcome_uncertain / ambiguous_upload: an earlier attempt may already be on YouTube. Ask the user to
  check YouTube Studio; only pass allow_duplicate=true if they explicitly decide to upload again.
- Previews can list indirect public exposure (unlisted videos in a playlist being made public, or public
  playlists containing a video being made unlisted). Show these to the user; such changes also need
  confirm_public=true and the native dialog.
"""

VideoId = Annotated[str, Field(description="YouTube video ID (11 characters) or any youtube.com / youtu.be URL.")]
Token = Annotated[str | None, Field(
    description="Token from the preview. Only pass it after the user has explicitly approved the preview.")]

WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)
CHANGE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True)
Lang = Annotated[str, Field(description="BCP-47 language code of the captions, e.g. 'en', 'en-US', 'hi'.")]
PlaylistId = Annotated[str, Field(description="Playlist ID (or a URL containing list=...).")]
PageToken = Annotated[str | None, Field(description="next_page_token from a previous call.")]
TZ = Annotated[str | None, Field(description="IANA timezone, e.g. 'Europe/London' (default: YOUTUBE_MCP_TIMEZONE, else this computer's timezone).")]
Preset = Annotated[str | None, Field(description="'last_7_days', 'last_28_days' (default) or 'last_90_days'. Ignored if dates are given.")]
Start = Annotated[str | None, Field(description="Custom start date YYYY-MM-DD (Pacific Time).")]
End = Annotated[str | None, Field(description="Custom end date YYYY-MM-DD (Pacific Time).")]
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)


def _tool_errors(fn: Callable[..., Awaitable[dict[str, Any]]]) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Turn every failure into a structured ToolError (is_error=true, JSON body)."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return await fn(*args, **kwargs)
        except ToolError:
            raise
        except Exception as exc:
            err = translate_exception(exc)
            if err.code == "internal_error":
                log.exception("Unexpected error in %s", fn.__name__)
            else:
                log.info("%s failed: %s", fn.__name__, err)
            raise ToolError(json.dumps(err.to_dict(), indent=2)) from None

    return wrapper


async def _run(fn: Callable[..., dict[str, Any]], *args: Any, **kwargs: Any) -> dict[str, Any]:
    return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))


def build_server(service: YouTubeMCPService) -> MCPServer:
    mcp = MCPServer(name="youtube", title="YouTube", version=__version__, instructions=INSTRUCTIONS)

    @mcp.tool(
        title="Upload video to YouTube",
        description=(
            "Upload a local video file to the user's YouTube channel (default visibility: unlisted). "
            "Two-step: the first call (without confirmation_token) returns a preview and token; show the preview "
            "to the user and call again with the token only after explicit approval. Uploads continue in the "
            "background; use get_upload_progress to follow them. Refuses to upload the same file twice unless "
            "allow_duplicate=true."
        ),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                                    open_world_hint=True),
    )
    @_tool_errors
    async def upload_video(
        ctx: Context,
        file_path: Annotated[str, Field(description="Absolute path to the local video file (e.g. /Users/me/Movies/demo.mp4).")],
        title: Annotated[str, Field(description="Video title, 1-100 characters.")],
        description: Annotated[str, Field(description="Video description (max 5000 bytes).")] = "",
        tags: Annotated[list[str] | None, Field(description="Tags (keywords). Total max 500 characters.")] = None,
        category_id: Annotated[str | None, Field(
            description="YouTube category ID, e.g. 22 People & Blogs, 27 Education, 28 Science & Technology. Defaults to YOUTUBE_MCP_DEFAULT_CATEGORY (22 if unset).")] = None,
        visibility: Annotated[str, Field(description="'unlisted' (default), 'private' or 'public'. Public requires explicit user approval.")] = "unlisted",
        made_for_kids: Annotated[bool, Field(description="Whether the video is made for kids (COPPA).")] = False,
        allow_duplicate: Annotated[bool, Field(description="Upload even if this exact file was uploaded before. Only with the user's explicit request.")] = False,
        copy_link: Annotated[bool, Field(description="Copy the link to the macOS clipboard when the upload finishes.")] = False,
        notify: Annotated[bool, Field(description="Show a macOS notification when the upload finishes or fails.")] = True,
        confirmation_token: Token = None,
        confirm_public: Annotated[bool, Field(description="Must be true for public uploads, and only when the user explicitly approved publishing publicly.")] = False,
        wait_seconds: Annotated[int, Field(ge=0, le=50, description="How long to wait for the upload before returning progress (0-50).")] = 20,
    ) -> dict[str, Any]:
        result = await _run(
            service.upload_video, file_path=file_path, title=title, description=description, tags=tags,
            category_id=category_id, visibility=visibility, made_for_kids=made_for_kids,
            allow_duplicate=allow_duplicate, copy_link=copy_link, notify=notify,
            confirmation_token=confirmation_token, confirm_public=confirm_public,
        )
        upload_id = result.get("upload_id")
        if not upload_id:
            return result  # confirmation preview
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            snap = await _run(service.uploads.snapshot, upload_id)
            if snap["status"] not in h.ACTIVE_STATUSES:
                break
            try:
                await ctx.report_progress(snap["progress_percent"], 100, f"Uploading {snap['title']}")
            except Exception:
                pass
            await anyio.sleep(1)
        return await _run(service.get_upload_progress, upload_id)

    @mcp.tool(title="Get upload progress",
              description="Progress and result of an upload started with upload_video (from the local history).",
              annotations=READ_ONLY)
    @_tool_errors
    async def get_upload_progress(
        upload_id: Annotated[str, Field(description="The upload_id returned by upload_video.")],
    ) -> dict[str, Any]:
        return await _run(service.get_upload_progress, upload_id)

    @mcp.tool(
        title="Update video metadata",
        description=("Change selected metadata of an existing video. Only the fields you pass are changed; all other "
                     "existing fields (including tags and description) are preserved. Pass tags=[] to clear tags. "
                     "Two-step: returns a preview of the changes and a confirmation_token; call again with the "
                     "token only after the user approves."),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True),
    )
    @_tool_errors
    async def update_metadata(
        video_id: VideoId,
        title: Annotated[str | None, Field(description="New title (1-100 characters).")] = None,
        description: Annotated[str | None, Field(description="New description (replaces the old one).")] = None,
        tags: Annotated[list[str] | None, Field(description="New full list of tags (replaces the old list).")] = None,
        category_id: Annotated[str | None, Field(description="New category ID.")] = None,
        default_language: Annotated[str | None, Field(description="BCP-47 language of title/description, e.g. 'en'.")] = None,
        confirmation_token: Token = None,
    ) -> dict[str, Any]:
        return await _run(service.update_metadata, video_id, title=title, description=description, tags=tags,
                          category_id=category_id, default_language=default_language,
                          confirmation_token=confirmation_token)

    @mcp.tool(
        title="Set video thumbnail",
        description=("Upload a custom thumbnail (JPEG/PNG, max 2 MB, 1280x720 recommended). Two-step: returns a "
                     "preview and confirmation_token first; call again with the token after the user approves. "
                     "Requires a verified channel."),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True),
    )
    @_tool_errors
    async def set_thumbnail(
        video_id: VideoId,
        image_path: Annotated[str, Field(description="Absolute path to the JPEG or PNG thumbnail.")],
        confirmation_token: Token = None,
    ) -> dict[str, Any]:
        return await _run(service.set_thumbnail, video_id, image_path, confirmation_token=confirmation_token)

    @mcp.tool(
        title="Set video visibility",
        description=("Change a video's visibility to private, unlisted or public, then verify the actual result. "
                     "Always two-step: returns a preview and confirmation_token; call again with the token after the "
                     "user approves. PUBLIC also needs confirm_public=true, and the user must additionally click "
                     "'Allow Public' in a native macOS dialog on their Mac. Changing private to unlisted is treated "
                     "the same way when the video is in one of the channel's PUBLIC playlists (anyone viewing the "
                     "playlist could then watch it); the preview lists those playlists."),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True),
    )
    @_tool_errors
    async def set_visibility(
        video_id: VideoId,
        visibility: Annotated[str, Field(description="'private', 'unlisted' or 'public'.")],
        confirmation_token: Token = None,
        confirm_public: Annotated[bool, Field(description="Required (true) to make a video public, or when the preview warns that it becomes visible through public playlists; only with explicit user approval.")] = False,
    ) -> dict[str, Any]:
        return await _run(service.set_visibility, video_id, visibility,
                          confirmation_token=confirmation_token, confirm_public=confirm_public)

    @mcp.tool(title="Get video status",
              description=("Processing state (processing/ready/failed/rejected), actual visibility, schedule, title, "
                           "description, tags, category, default language, thumbnails and other details of a video."),
              annotations=READ_ONLY)
    @_tool_errors
    async def get_video_status(video_id: VideoId) -> dict[str, Any]:
        return await _run(service.get_video_status, video_id)

    @mcp.tool(title="Get share link",
              description="Return the shareable URL of a video (with its visibility), optionally copying it to the macOS clipboard.",
              annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True))
    @_tool_errors
    async def get_share_link(
        video_id: VideoId,
        copy_to_clipboard: Annotated[bool, Field(description="Also copy the URL to the macOS clipboard.")] = False,
        link_format: Annotated[str, Field(description="'short' (youtu.be) or 'watch' (youtube.com/watch).")] = "short",
    ) -> dict[str, Any]:
        return await _run(service.get_share_link, video_id, copy_to_clipboard=copy_to_clipboard, link_format=link_format)

    @mcp.tool(title="List uploads",
              description=("List videos. source='youtube' lists the channel's uploads from YouTube (newest first); "
                           "source='local' lists this server's local upload history, including failed and in-progress uploads."),
              annotations=READ_ONLY)
    @_tool_errors
    async def list_uploads(
        source: Annotated[str, Field(description="'youtube' (default) or 'local'.")] = "youtube",
        max_results: Annotated[int, Field(ge=1, le=50, description="Items per page (1-50).")] = 25,
        page_token: Annotated[str | None, Field(description="next_page_token from a previous call.")] = None,
    ) -> dict[str, Any]:
        return await _run(service.list_uploads, source=source, max_results=max_results, page_token=page_token)

    @mcp.tool(title="Get channel info",
              description="Identify the YouTube channel this server is authorized for (name, ID, URL, counts).",
              annotations=READ_ONLY)
    @_tool_errors
    async def get_channel_info() -> dict[str, Any]:
        return await _run(service.get_channel_info)

    # --- Captions ------------------------------------------------------------------------


    @mcp.tool(title="Upload captions",
              description=("Add a subtitle track to a video from a SubRip (.srt) or WebVTT (.vtt) file. Two-step: "
                           "returns a preview and confirmation_token; call again with the token after the user "
                           "approves. Refuses if a track with the same language and name already exists."),
              annotations=WRITE)
    @_tool_errors
    async def upload_captions(
        video_id: VideoId,
        file_path: Annotated[str, Field(description="Absolute path to the .srt or .vtt file (UTF-8).")],
        language: Lang = "en",
        name: Annotated[str, Field(description="Track name shown to viewers (max 150 chars). Empty = default track.")] = "",
        is_draft: Annotated[bool, Field(description="Upload as a draft (not shown to viewers).")] = False,
        confirmation_token: Token = None,
        verify_content: Annotated[bool, Field(description="After uploading, download the track and compare its text with the file (extra 200 quota units).")] = False,
    ) -> dict[str, Any]:
        return await _run(service.captions.upload_captions, video_id, file_path, language=language, name=name,
                          is_draft=is_draft, confirmation_token=confirmation_token, verify_content=verify_content)

    @mcp.tool(title="List captions", description="List a video's caption tracks (language, name, draft, status).",
              annotations=READ_ONLY)
    @_tool_errors
    async def list_captions(video_id: VideoId) -> dict[str, Any]:
        return await _run(service.captions.list_captions, video_id)

    @mcp.tool(title="Update captions",
              description=("Replace a caption track's file and/or change its draft status. YouTube's API cannot "
                           "rename a track or change its language. Two-step approval."),
              annotations=CHANGE)
    @_tool_errors
    async def update_captions(
        video_id: VideoId,
        caption_id: Annotated[str, Field(description="Caption track ID from list_captions.")],
        file_path: Annotated[str | None, Field(description="New .srt/.vtt file to replace the track's captions.")] = None,
        is_draft: Annotated[bool | None, Field(description="Set draft (true) or published (false).")] = None,
        confirmation_token: Token = None,
        verify_content: Annotated[bool, Field(description="After replacing the file, download the track and compare its text (extra 200 quota units).")] = False,
    ) -> dict[str, Any]:
        return await _run(service.captions.update_captions, video_id, caption_id, file_path=file_path,
                          is_draft=is_draft, confirmation_token=confirmation_token, verify_content=verify_content)

    @mcp.tool(title="Download captions",
              description=("Read-only: return the text of one of the channel's caption tracks (captions.download, "
                           "about 250 quota units including the ownership check). list_captions returns metadata "
                           "only; this is the only way to read caption text."),
              annotations=READ_ONLY)
    @_tool_errors
    async def download_captions(
        video_id: VideoId,
        caption_id: Annotated[str, Field(description="Caption track ID from list_captions.")],
        format: Annotated[str, Field(description="srt (default), vtt, sbv, ttml or scc.")] = "srt",
    ) -> dict[str, Any]:
        return await _run(service.captions.download_captions, video_id, caption_id, fmt=format)

    # --- Playlists -----------------------------------------------------------------------


    @mcp.tool(title="List playlists", description="List the channel's playlists (title, privacy, item count).",
              annotations=READ_ONLY)
    @_tool_errors
    async def list_playlists(
        max_results: Annotated[int, Field(ge=1, le=50, description="Items per page (1-50).")] = 25,
        page_token: PageToken = None,
    ) -> dict[str, Any]:
        return await _run(service.playlists.list_playlists, max_results=max_results, page_token=page_token)

    @mcp.tool(title="Create playlist",
              description=("Create a playlist (PRIVATE by default). Two-step approval; a public playlist also "
                           "needs confirm_public=true with the user's explicit approval."),
              annotations=WRITE)
    @_tool_errors
    async def create_playlist(
        title: Annotated[str, Field(description="Playlist title (max 150 chars).")],
        description: Annotated[str, Field(description="Playlist description.")] = "",
        privacy: Annotated[str, Field(description="'private' (default), 'unlisted' or 'public'.")] = "private",
        confirmation_token: Token = None,
        confirm_public: Annotated[bool, Field(description="Required for public playlists, only with explicit approval.")] = False,
    ) -> dict[str, Any]:
        return await _run(service.playlists.create_playlist, title, description=description, privacy=privacy,
                          confirmation_token=confirmation_token, confirm_public=confirm_public)

    @mcp.tool(title="Update playlist",
              description=("Change a playlist's title, description and/or privacy; other fields are preserved. "
                           "Two-step approval; making it public also needs confirm_public=true and the native dialog. "
                           "The preview lists unlisted videos in the playlist that would become visible to anyone "
                           "viewing it."),
              annotations=CHANGE)
    @_tool_errors
    async def update_playlist(
        playlist_id: PlaylistId,
        title: Annotated[str | None, Field(description="New title.")] = None,
        description: Annotated[str | None, Field(description="New description (replaces the old one).")] = None,
        privacy: Annotated[str | None, Field(description="'private', 'unlisted' or 'public'.")] = None,
        confirmation_token: Token = None,
        confirm_public: Annotated[bool, Field(description="Required to make it public, only with explicit approval.")] = False,
    ) -> dict[str, Any]:
        return await _run(service.playlists.update_playlist, playlist_id, title=title, description=description,
                          privacy=privacy, confirmation_token=confirmation_token, confirm_public=confirm_public)

    @mcp.tool(title="List playlist items", description="List the videos in a playlist, with their playlist_item_id.",
              annotations=READ_ONLY)
    @_tool_errors
    async def list_playlist_items(
        playlist_id: PlaylistId,
        max_results: Annotated[int, Field(ge=1, le=50, description="Items per page (1-50).")] = 25,
        page_token: PageToken = None,
    ) -> dict[str, Any]:
        return await _run(service.playlists.list_playlist_items, playlist_id, max_results=max_results,
                          page_token=page_token)

    @mcp.tool(title="Add video to playlist",
              description=("Add a video to a playlist. Refuses duplicates (playlist_duplicate). Two-step approval."),
              annotations=WRITE)
    @_tool_errors
    async def add_to_playlist(
        playlist_id: PlaylistId,
        video_id: VideoId,
        position: Annotated[int | None, Field(ge=0, description="0-based position; only for manually sorted playlists. Default: end.")] = None,
        confirmation_token: Token = None,
    ) -> dict[str, Any]:
        return await _run(service.playlists.add_to_playlist, playlist_id, video_id, position=position,
                          confirmation_token=confirmation_token)

    @mcp.tool(title="Remove video from playlist",
              description=("Remove an entry from a playlist (the video itself is NOT deleted). Pass video_id or a "
                           "specific playlist_item_id. Two-step approval."),
              annotations=CHANGE)
    @_tool_errors
    async def remove_from_playlist(
        playlist_id: PlaylistId,
        video_id: Annotated[str | None, Field(description="Video ID or URL to remove.")] = None,
        playlist_item_id: Annotated[str | None, Field(description="Specific playlist item (if the video appears more than once).")] = None,
        confirmation_token: Token = None,
    ) -> dict[str, Any]:
        return await _run(service.playlists.remove_from_playlist, playlist_id, video_id=video_id,
                          playlist_item_id=playlist_item_id, confirmation_token=confirmation_token)

    # --- Categories ----------------------------------------------------------------------

    @mcp.tool(title="List video categories",
              description=("List YouTube video categories for a region (default from YOUTUBE_MCP_REGION, 'US'). "
                           "Use category IDs with assignable=true in upload_video and update_metadata. "
                           "The default upload category is set by YOUTUBE_MCP_DEFAULT_CATEGORY (22, People & Blogs, if unset)."),
              annotations=READ_ONLY)
    @_tool_errors
    async def list_categories(
        region_code: Annotated[str | None, Field(description="ISO 3166-1 alpha-2 region, e.g. IN, US.")] = None,
        hl: Annotated[str, Field(description="Language for category titles, e.g. en_US, hi.")] = "en_US",
        assignable_only: Annotated[bool, Field(description="Only categories that videos can use.")] = False,
    ) -> dict[str, Any]:
        return await _run(service.categories.list_categories, region_code, hl=hl, assignable_only=assignable_only)

    # --- Scheduling ----------------------------------------------------------------------


    @mcp.tool(title="Schedule public publishing",
              description=("Schedule a PRIVATE, never-published video to become PUBLIC at a given time. A time without "
                           "an offset is read in `timezone`. Two-step approval AND confirm_public=true are always "
                           "required. Unlisted/public videos are refused and never changed."),
              annotations=CHANGE)
    @_tool_errors
    async def schedule_publish(
        video_id: VideoId,
        publish_at: Annotated[str, Field(description="ISO 8601 time, e.g. '2026-12-01 18:30' or '2026-12-01T13:00:00Z'.")],
        timezone: TZ = None,
        confirmation_token: Token = None,
        confirm_public: Annotated[bool, Field(description="Must be true, only after the user explicitly approved public publication.")] = False,
    ) -> dict[str, Any]:
        return await _run(service.scheduling.schedule_publish, video_id, publish_at, timezone_name=timezone,
                          confirmation_token=confirmation_token, confirm_public=confirm_public)

    @mcp.tool(title="Get publish schedule", description="Show whether a video has a scheduled publish time.",
              annotations=READ_ONLY)
    @_tool_errors
    async def get_publish_schedule(video_id: VideoId, timezone: TZ = None) -> dict[str, Any]:
        return await _run(service.scheduling.get_publish_schedule, video_id, timezone_name=timezone)

    @mcp.tool(title="Cancel publish schedule",
              description="Remove a video's scheduled publish time; it stays private. Two-step approval.",
              annotations=CHANGE)
    @_tool_errors
    async def cancel_publish_schedule(video_id: VideoId, timezone: TZ = None,
                                      confirmation_token: Token = None) -> dict[str, Any]:
        return await _run(service.scheduling.cancel_publish_schedule, video_id,
                          confirmation_token=confirmation_token, timezone_name=timezone)

    # --- Analytics -----------------------------------------------------------------------


    @mcp.tool(title="Get channel analytics",
              description=("Channel views, watch time, average view duration, subscribers gained/lost, likes, "
                           "comments, shares and traffic sources for a date range. Optional feature: needs "
                           "analytics authorization (see README)."),
              annotations=READ_ONLY)
    @_tool_errors
    async def get_channel_analytics(
        preset: Preset = "last_28_days", start_date: Start = None, end_date: End = None,
        include_daily: Annotated[bool, Field(description="Add a per-day breakdown.")] = False,
        include_traffic_sources: Annotated[bool, Field(description="Add views by traffic source.")] = True,
    ) -> dict[str, Any]:
        return await _run(service.analytics.get_channel_analytics, preset, start_date, end_date,
                          include_daily=include_daily, include_traffic_sources=include_traffic_sources)

    @mcp.tool(title="Get video analytics",
              description=("Analytics for one video: views, watch time, average view duration, engagement, traffic "
                           "sources and audience retention. Optional feature: needs analytics authorization."),
              annotations=READ_ONLY)
    @_tool_errors
    async def get_video_analytics(
        video_id: VideoId, preset: Preset = "last_28_days", start_date: Start = None, end_date: End = None,
        include_daily: Annotated[bool, Field(description="Add a per-day breakdown.")] = False,
        include_traffic_sources: Annotated[bool, Field(description="Add views by traffic source.")] = True,
        include_retention: Annotated[bool, Field(description="Add the audience retention curve.")] = True,
    ) -> dict[str, Any]:
        return await _run(service.analytics.get_video_analytics, video_id, preset, start_date, end_date,
                          include_daily=include_daily, include_traffic_sources=include_traffic_sources,
                          include_retention=include_retention)

    @mcp.tool(title="Get top videos",
              description="The channel's top videos for a date range, ranked by a metric (max 200). Optional feature.",
              annotations=READ_ONLY)
    @_tool_errors
    async def get_top_videos(
        preset: Preset = "last_28_days", start_date: Start = None, end_date: End = None,
        sort_by: Annotated[str, Field(description="views (default), estimatedMinutesWatched, subscribersGained, likes, comments or shares.")] = "views",
        max_results: Annotated[int, Field(ge=1, le=200, description="Number of videos (1-200).")] = 10,
    ) -> dict[str, Any]:
        return await _run(service.analytics.get_top_videos, preset, start_date, end_date, sort_by=sort_by,
                          max_results=max_results)

    return mcp


def create_service(settings: Settings | None = None) -> YouTubeMCPService:
    settings = settings or Settings.from_env()
    credentials = CredentialManager(settings)
    client = YouTubeClient(credentials.build_service)
    history = History(settings.history_db)
    uploads = UploadManager(client, history, notifier=notify, clipboard=copy_to_clipboard)
    analytics = AnalyticsClient(credentials.build_analytics_service)
    return YouTubeMCPService(settings, client, history, uploads,
                             ConfirmationStore(settings.confirmation_ttl_seconds), clipboard=copy_to_clipboard,
                             analytics_client=analytics)


def configure_logging() -> None:
    from .config import setting

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel((setting("YOUTUBE_MCP_LOG_LEVEL") or "INFO").upper())
    for noisy in ("googleapiclient.discovery_cache", "googleapiclient.discovery", "google_auth_httplib2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> None:
    configure_logging()
    service = create_service()
    stale = service.staging.cleanup_stale()
    if stale:
        log.info("Removed %d leftover upload snapshot(s)", stale)
    recovered = service.history.recover_interrupted()
    if recovered:
        log.warning("Marked %d upload(s) from a previous session as interrupted", recovered)
    log.info("YouTubeMCP server %s starting (stdio); data dir: %s", __version__, service.settings.data_dir)
    build_server(service).run("stdio")


if __name__ == "__main__":
    main()
