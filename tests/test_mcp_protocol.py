"""End-to-end tests through the MCP protocol: in-process client, and a real stdio subprocess."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import anyio
from mcp import Client, StdioServerParameters

from youtube_mcp.server import build_server

ROOT = Path(__file__).resolve().parent.parent
EXPECTED_TOOLS = {"upload_video", "update_metadata", "set_thumbnail", "set_visibility", "get_video_status",
                  "get_share_link", "list_uploads", "get_channel_info", "get_upload_progress",
                  "upload_captions", "list_captions", "update_captions",
                  "list_playlists", "create_playlist", "update_playlist", "list_playlist_items",
                  "add_to_playlist", "remove_from_playlist", "list_categories",
                  "schedule_publish", "get_publish_schedule", "cancel_publish_schedule",
                  "get_channel_analytics", "get_video_analytics", "get_top_videos",
                  "download_captions"}
# No tool may delete videos/captions/playlists or touch comments. The single "remove" tool only
# removes a playlist entry (never the video) and is approval-gated.
DESTRUCTIVE_NAMES = ("delete", "comment")
ALLOWED_REMOVE_TOOLS = {"remove_from_playlist"}


def error_json(result):
    """Error text is 'Error executing tool <name>: {json}'."""
    text = result.content[0].text
    return json.loads(text[text.index("{"):])


def test_tools_over_mcp(env):
    server = build_server(env.service)

    async def run():
        async with Client(server) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert set(tools) == EXPECTED_TOOLS
            assert not any(word in name for name in tools for word in DESTRUCTIVE_NAMES)
            assert {name for name in tools if "remove" in name} == ALLOWED_REMOVE_TOOLS
            for name in ("list_captions", "list_playlists", "list_playlist_items", "list_categories",
                         "get_publish_schedule", "get_channel_analytics", "get_video_analytics", "get_top_videos",
                         "download_captions"):
                assert tools[name].annotations.read_only_hint is True, name
            # Consistent approval model: every tool that writes to YouTube is two-step.
            write_tools = {name for name, t in tools.items() if not t.annotations.read_only_hint} - {"get_share_link"}
            assert write_tools == {"upload_video", "update_metadata", "set_thumbnail", "set_visibility",
                                   "upload_captions", "update_captions", "create_playlist", "update_playlist",
                                   "add_to_playlist", "remove_from_playlist", "schedule_publish",
                                   "cancel_publish_schedule"}
            for name in write_tools:
                assert "confirmation_token" in tools[name].input_schema["properties"], name
            # No tool parameter can skip the native public confirmation.
            for name, t in tools.items():
                for prop in t.input_schema["properties"]:
                    assert not any(w in prop.lower() for w in ("bypass", "skip", "native", "dialog", "force",
                                                               "no_confirm", "unattended")), (name, prop)
            assert tools["create_playlist"].input_schema["properties"]["privacy"]["default"] == "private"
            assert tools["get_channel_info"].annotations.read_only_hint is True
            assert "ctx" not in tools["upload_video"].input_schema["properties"]
            assert tools["upload_video"].input_schema["properties"]["visibility"]["default"] == "unlisted"

            # Two-step upload over the protocol, with the upload running in a background thread
            path = env.video()
            preview = await client.call_tool("upload_video", {"file_path": path, "title": "Over MCP"})
            assert not preview.is_error
            data = preview.structured_content
            assert data["status"] == "confirmation_required" and env.api.inserts == []
            done = await client.call_tool("upload_video", {"file_path": path, "title": "Over MCP",
                                                           "confirmation_token": data["confirmation_token"],
                                                           "wait_seconds": 10})
            result = done.structured_content
            assert result["status"] == "completed", result
            assert result["actual_visibility"] == "unlisted"

            # Structured error with is_error=True
            err = await client.call_tool("get_video_status", {"video_id": "not-valid"})
            assert err.is_error
            payload = error_json(err)
            assert payload["error"]["code"] == "invalid_input"

            dup = await client.call_tool("upload_video", {"file_path": path, "title": "Again"})
            assert dup.is_error and error_json(dup)["error"]["code"] == "duplicate_upload"

            ch = await client.call_tool("get_channel_info", {})
            assert ch.structured_content["title"] == "Test Channel"

    anyio.run(run)


def test_stdio_subprocess_keeps_stdout_clean(tmp_path):
    """Launch the real server script over stdio, unauthorized, from an unrelated cwd."""
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "youtube_mcp_server.py")],
        env={**os.environ, "YOUTUBE_MCP_HOME": str(tmp_path), "PYTHONUNBUFFERED": "1"},
        cwd=str(tmp_path),
    )

    async def run():
        async with Client(params, read_timeout_seconds=30) as client:
            tools = (await client.list_tools()).tools
            assert {t.name for t in tools} == EXPECTED_TOOLS
            res = await client.call_tool("get_channel_info", {})
            assert res.is_error
            err = error_json(res)["error"]
            assert err["code"] == "not_authorized" and "authorize.py" in err["action"]
            local = await client.call_tool("list_uploads", {"source": "local"})
            assert not local.is_error and local.structured_content["total"] == 0

    anyio.run(run)
    assert (tmp_path / "data" / "history.sqlite3").exists()


def test_phase7_tools_over_mcp(env):
    """Every Phase 7 tool answers over the protocol with structured content or a structured error."""
    vid = env.api.add_video({"snippet": {"title": "Demo"}, "status": {"privacyStatus": "unlisted"}})
    pid = env.api.add_playlist("Mine")
    srt = env.tmp / "subs.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n", encoding="utf-8")

    async def main() -> None:
        async with Client(build_server(env.service)) as client:
            async def ok(name, args):
                res = await client.call_tool(name, args)
                assert not res.is_error, (name, res.content)
                return res.structured_content

            assert (await ok("list_categories", {"region_code": "IN"}))["region_code"] == "IN"
            assert (await ok("list_playlists", {}))["items"][0]["playlist_id"] == pid
            assert (await ok("list_playlist_items", {"playlist_id": pid}))["items"] == []
            assert (await ok("list_captions", {"video_id": vid}))["count"] == 0
            assert (await ok("get_publish_schedule", {"video_id": vid}))["scheduled"] is False
            for name, args in [
                ("upload_captions", {"video_id": vid, "file_path": str(srt)}),
                ("create_playlist", {"title": "New"}),
                ("update_playlist", {"playlist_id": pid, "title": "Renamed"}),
                ("add_to_playlist", {"playlist_id": pid, "video_id": vid}),
            ]:
                assert (await ok(name, args))["status"] == "confirmation_required", name
            assert not env.api.captions_db and len(env.api.playlists_db) == 1  # previews wrote nothing

            for name, args, code in [
                ("schedule_publish", {"video_id": vid, "publish_at": "2099-01-01 10:00"}, "unsupported_by_youtube"),
                ("update_captions", {"video_id": vid, "caption_id": "x"}, "invalid_input"),
                ("remove_from_playlist", {"playlist_id": pid}, "invalid_input"),
                ("cancel_publish_schedule", {"video_id": "bad"}, "invalid_input"),
                ("get_channel_analytics", {}, "analytics_not_enabled"),
                ("get_video_analytics", {"video_id": vid}, "analytics_not_enabled"),
                ("get_top_videos", {}, "analytics_not_enabled"),
            ]:
                res = await client.call_tool(name, args)
                assert res.is_error, name
                assert error_json(res)["error"]["code"] == code, (name, error_json(res))

    anyio.run(main)
