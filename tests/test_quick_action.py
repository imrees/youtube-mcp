"""Finder Quick Action front end, exercised against the in-memory fake YouTube API."""

from __future__ import annotations

import hashlib
import plistlib
from pathlib import Path

import pytest

from tests.fakes import http_error
from youtube_mcp import history as h
from youtube_mcp import quick_action as qa
from youtube_mcp.errors import auth_expired


def make_action(env, check_auth=lambda: None):
    notes: list[tuple[str, str, str]] = []
    action = qa.QuickAction(
        env.service, check_auth=check_auth,
        notifier=lambda title, msg, sub="", sound="": notes.append((sub, msg, sound)) or True,
        clipboard=lambda text: env.clipboard.append(text) or True,
        poll_interval=0, sleep=lambda s: None,
    )
    return action, notes


def test_title_from_filename():
    assert qa.title_from_filename(Path("/x/2026-11-26 demo v2.mp4")) == "2026-11-26 demo v2"
    assert qa.title_from_filename(Path("/x/a<b>c.mov")) == "abc"
    assert qa.title_from_filename(Path("/x/  spaced   out .mov")) == "spaced out"
    long = qa.title_from_filename(Path("/x/" + "a" * 150 + ".mp4"))
    assert len(long) == 100 and long.endswith("…")
    assert qa.title_from_filename(Path("/x/<>.mp4")) == "Untitled video"


def test_new_upload_is_unlisted_verified_and_copied(env):
    path = env.video("My Demo.mp4")
    before = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    action, notes = make_action(env)

    result = action.process(path)

    assert result.outcome == "uploaded" and result.visibility == "unlisted"
    assert env.api.videos_db[result.video_id]["snippet"]["title"] == "My Demo"
    assert env.api.videos_db[result.video_id]["status"]["privacyStatus"] == "unlisted"
    assert env.clipboard == [result.url]
    assert notes[0][0] == "Started: My Demo"
    assert notes[-1][0] == "Uploaded: My Demo" and "Link copied" in notes[-1][1]
    rec = env.history.find_by_sha256(env.service.file_sha256(Path(path)))[0]
    assert rec["status"] == h.COMPLETED and rec["actual_visibility"] == "unlisted"
    assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == before  # original untouched
    assert env.notifications == []  # UploadManager's own notifications are off; QA reports instead


def test_duplicate_returns_existing_link_without_uploading(env):
    path = env.video("clip.mov")
    action, notes = make_action(env)
    first = action.process(path)
    inserts = env.api.calls.count("videos.insert.chunk")
    env.clipboard.clear()

    second = action.process(path)

    assert second.outcome == "already_uploaded" and second.video_id == first.video_id
    assert env.api.calls.count("videos.insert.chunk") == inserts
    assert env.clipboard == [first.url]
    assert notes[-1][0] == "Already uploaded: clip"


def test_duplicate_deleted_on_youtube_is_reported_not_reuploaded(env):
    path = env.video("gone.mp4")
    action, notes = make_action(env)
    first = action.process(path)
    del env.api.videos_db[first.video_id]
    inserts = env.api.calls.count("videos.insert.chunk")

    result = action.process(path)

    assert result.outcome == "failed" and "no longer on your channel" in result.message
    assert env.api.calls.count("videos.insert.chunk") == inserts


def test_duplicate_with_network_error_still_returns_link(env):
    path = env.video("net.mp4")
    action, notes = make_action(env)
    first = action.process(path)
    env.api.fail_next["videos.list"] = ConnectionError("offline")

    result = action.process(path)

    assert result.outcome == "already_uploaded" and result.url == first.url and result.visibility is None
    assert "could not be verified" in notes[-1][1]


def test_expired_auth_fails_before_any_history_record(env):
    path = env.video("auth.mp4")

    def expired():
        raise auth_expired("refresh rejected by Google")

    action, notes = make_action(env, check_auth=expired)
    result = action.process(path)

    assert result.outcome == "failed" and "auth_expired" in result.message
    assert "authorize.py" in notes[-1][1] and notes[-1][2] == "Basso"
    assert env.history.count_uploads() == 0


def test_upload_failure_is_reported_and_not_treated_as_duplicate(env):
    path = env.video("fail.mp4")
    env.api.upload_failures = [http_error(403, "uploadLimitExceeded", "limit")]
    action, notes = make_action(env)

    result = action.process(path)
    assert result.outcome == "failed" and notes[-1][0] == "Failed: fail"

    retry = action.process(path)  # failed attempt must not block a retry
    assert retry.outcome == "uploaded"


def test_transient_network_errors_are_retried(env):
    path = env.video("flaky.mp4")
    env.api.upload_failures = [ConnectionError("reset"), ConnectionError("reset")]
    action, _ = make_action(env)
    assert action.process(path).outcome == "uploaded"


def test_private_result_is_flagged(env):
    path = env.video("locked.mp4")
    env.api.upload_privacy_override = "private"
    action, notes = make_action(env)
    result = action.process(path)
    assert result.visibility == "private" and notes[-1][2] == "Basso"
    assert any("PRIVATE" in w for w in result.warnings)


def test_public_from_youtube_is_reverted(env):
    path = env.video("oops.mp4")
    env.api.upload_privacy_override = "public"
    action, _ = make_action(env)
    result = action.process(path)
    assert result.visibility == "unlisted"


@pytest.mark.parametrize("name", ["notes.txt", "missing.mp4"])
def test_invalid_files_fail_cleanly(env, name):
    action, notes = make_action(env)
    path = env.video(name) if name.endswith(".txt") else str(env.tmp / name)
    result = action.process(path)
    assert result.outcome == "failed" and notes[-1][2] == "Basso"
    assert env.api.calls == []


def test_install_and_uninstall_roundtrip(tmp_path):
    assert qa.install(tmp_path) == 0
    bundle = tmp_path / qa.WORKFLOW_NAME
    info = plistlib.loads((bundle / "Contents" / "Info.plist").read_bytes())
    service = info["NSServices"][0]
    assert service["NSMenuItem"]["default"] == "Upload & Share"
    assert "public.movie" in service["NSSendFileTypes"]
    doc = plistlib.loads((bundle / "Contents" / "document.wflow").read_bytes())
    cmd = doc["actions"][0]["action"]["ActionParameters"]["COMMAND_STRING"]
    assert "upload_and_share.py" in cmd and 'upload -- "$@"' in cmd
    assert qa.install(tmp_path) == 0  # reinstall over our own bundle is fine
    assert qa.uninstall(tmp_path) == 0 and not bundle.exists()


def test_install_refuses_to_overwrite_foreign_bundle(tmp_path):
    foreign = tmp_path / qa.WORKFLOW_NAME / "Contents"
    foreign.mkdir(parents=True)
    (foreign / "document.wflow").write_text("someone else's workflow")
    assert qa.install(tmp_path) == 1
    assert qa.uninstall(tmp_path) == 1
    assert (foreign / "document.wflow").exists()
