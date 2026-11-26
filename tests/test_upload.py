from __future__ import annotations

import pytest

from tests.fakes import http_error
from youtube_mcp.errors import YouTubeMCPError


def test_first_call_returns_preview_and_uploads_nothing(env):
    path = env.video()
    result = env.service.upload_video(path, "My demo")
    assert result["status"] == "confirmation_required"
    assert result["preview"]["visibility"] == "unlisted"
    assert result["confirmation_token"].startswith("confirm-")
    assert "STOP" in result["next_step"]
    assert env.api.inserts == [] and env.history.count_uploads() == 0


def test_approved_upload_defaults_to_unlisted_and_verifies(env):
    result = env.upload(env.video(), title="My demo", tags=["a", "b"], copy_link=True)
    assert result["status"] == "completed"
    assert result["requested_visibility"] == "unlisted"
    assert result["actual_visibility"] == "unlisted"
    assert result["warnings"] == []
    assert result["url"].startswith("https://youtu.be/")
    body = env.api.inserts[0]["body"]
    assert body["status"]["privacyStatus"] == "unlisted"
    assert body["snippet"]["categoryId"] == "22"  # generic default (People & Blogs)
    assert env.api.inserts[0]["notify"] is False
    assert env.clipboard == [result["url"]]
    assert env.notifications and "complete" in env.notifications[0][0]
    assert "videos.list" in env.api.calls  # verification happened
    assert env.history.list_actions()[0]["action"] == "upload"


def test_token_is_single_use(env):
    path = env.video()
    token = env.service.upload_video(path, "T")["confirmation_token"]
    env.service.upload_video(path, "T", confirmation_token=token, background=False)
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.upload_video(path, "T", confirmation_token=token, allow_duplicate=True, background=False)
    assert exc.value.code == "confirmation_invalid"


def test_token_rejected_if_parameters_change(env):
    path = env.video()
    token = env.service.upload_video(path, "Approved title")["confirmation_token"]
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.upload_video(path, "Different title", confirmation_token=token, background=False)
    assert exc.value.code == "confirmation_mismatch"
    assert env.api.inserts == []


def test_token_rejected_if_file_changes_after_preview(env):
    path = env.video()
    token = env.service.upload_video(path, "T")["confirmation_token"]
    with open(path, "ab") as fh:
        fh.write(b"tampered")
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.upload_video(path, "T", confirmation_token=token, background=False)
    assert exc.value.code == "confirmation_mismatch"


def test_token_expires(env):
    now = [0.0]
    env.service.confirmations._clock = lambda: now[0]
    path = env.video()
    token = env.service.upload_video(path, "T")["confirmation_token"]
    now[0] = 601
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.upload_video(path, "T", confirmation_token=token, background=False)
    assert exc.value.code == "confirmation_expired"


def test_public_upload_requires_confirm_public(env):
    path = env.video()
    preview = env.service.upload_video(path, "T", visibility="public")
    assert preview["warnings"] and "PUBLIC" in preview["warnings"][0]
    assert "confirm_public=true" in preview["next_step"]
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.upload_video(path, "T", visibility="public",
                                 confirmation_token=preview["confirmation_token"], background=False)
    assert exc.value.code == "public_requires_approval"
    assert env.api.inserts == []
    # With explicit approval it proceeds
    preview = env.service.upload_video(path, "T", visibility="public")
    result = env.service.upload_video(path, "T", visibility="public", confirm_public=True,
                                      confirmation_token=preview["confirmation_token"], background=False)
    assert result["actual_visibility"] == "public"
    assert env.api.inserts[0]["notify"] is True


def test_duplicate_upload_blocked_unless_allowed(env):
    path = env.video()
    first = env.upload(path, title="T")
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.upload_video(path, "T again")
    assert exc.value.code == "duplicate_upload"
    assert exc.value.details["video_id"] == first["video_id"]
    # Same content under a different filename is still a duplicate
    with open(path, "rb") as fh:
        copy_path = env.video("copy.mp4", fh.read())
    with pytest.raises(YouTubeMCPError):
        env.service.upload_video(copy_path, "T")
    second = env.upload(path, title="T again", allow_duplicate=True)
    assert second["status"] == "completed" and second["video_id"] != first["video_id"]


def test_failed_upload_is_recorded_and_not_a_duplicate(env):
    path = env.video()
    env.api.upload_failures = [http_error(403, "quotaExceeded", "quota")]
    result = env.upload(path, title="T")
    assert result["status"] == "failed"
    assert result["error"]["code"] == "quota_exceeded"
    assert "action" in result["error"]
    assert env.notifications[-1][0] == "YouTube upload failed"
    assert env.upload(path, title="T")["status"] == "completed"  # retry allowed


def test_transient_errors_are_retried_with_backoff(env):
    env.api.upload_failures = [http_error(503, "backendError", "x"), ConnectionResetError("reset"),
                               http_error(500, "", "x")]
    result = env.upload(env.video(), title="T")
    assert result["status"] == "completed"
    assert len(env.sleeps) == 3 and all(s > 0 for s in env.sleeps)


def test_gives_up_after_max_retries(env):
    env.service.uploads.max_retries = 2
    env.api.upload_failures = [http_error(503)] * 5
    result = env.upload(env.video(), title="T")
    assert result["status"] == "failed" and result["error"]["code"] == "youtube_unavailable"


def test_private_lock_by_unaudited_project_is_reported(env):
    env.api.upload_privacy_override = "private"
    result = env.upload(env.video(), title="T")
    assert result["status"] == "completed"
    assert result["actual_visibility"] == "private"
    assert result["warnings"][0]["code"] == "visibility_restricted"


def test_unexpected_public_is_corrected_back(env):
    env.api.upload_privacy_override = "public"
    result = env.upload(env.video(), title="T")
    assert result["actual_visibility"] == "unlisted"
    assert result["warnings"][0]["code"] == "visibility_corrected"
    vid = result["video_id"]
    assert env.api.videos_db[vid]["status"]["privacyStatus"] == "unlisted"
    assert env.api.videos_db[vid]["status"]["license"] == "youtube"  # other status fields preserved


@pytest.mark.parametrize("kwargs,code", [
    ({"title": ""}, "invalid_input"),
    ({"title": "x" * 101}, "invalid_input"),
    ({"title": "bad <title>"}, "invalid_input"),
    ({"title": "ok", "visibility": "secret"}, "invalid_input"),
    ({"title": "ok", "tags": ["has,comma"]}, "invalid_input"),
    ({"title": "ok", "tags": ["x" * 300, "y" * 300]}, "invalid_input"),
    ({"title": "ok", "category_id": "abc"}, "invalid_input"),
])
def test_metadata_validation(env, kwargs, code):
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.upload_video(env.video(), **kwargs)
    assert exc.value.code == code and exc.value.message


def test_file_validation(env):
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.upload_video("/nonexistent/file.mp4", "T")
    assert exc.value.code == "file_not_found"
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.upload_video("relative/file.mp4", "T")
    assert "absolute" in exc.value.message
    with pytest.raises(YouTubeMCPError):
        env.service.upload_video(env.video("notes.txt"), "T")
    with pytest.raises(YouTubeMCPError):
        env.service.upload_video(env.video("empty.mp4", b""), "T")


def test_interrupted_uploads_are_recovered(env):
    upload_id = env.history.create_upload(file_path="/x.mp4", file_name="x.mp4", file_size=1, sha256="abc",
                                          title="T", category_id="28", requested_visibility="unlisted")
    env.history.update_upload(upload_id, status="uploading", pid=999_999)
    assert env.history.recover_interrupted() == 1
    assert env.history.get_upload(upload_id)["status"] == "interrupted"


def test_upload_in_progress_blocks_second_start(env):
    path = env.video()
    sha = env.service.file_sha256(__import__("pathlib").Path(path))
    env.history.create_upload(file_path=path, file_name="demo.mp4", file_size=1, sha256=sha, title="T",
                              category_id="28", requested_visibility="unlisted")
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.upload_video(path, "T")
    assert exc.value.code == "upload_in_progress"


def test_progress_snapshot_and_local_history(env):
    result = env.upload(env.video(), title="T")
    snap = env.service.get_upload_progress(result["upload_id"])
    assert snap["progress_percent"] == 100.0 and snap["status"] == "completed"
    local = env.service.list_uploads(source="local")
    assert local["total"] == 1 and local["items"][0]["video_id"] == result["video_id"]
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.get_upload_progress("nope")
    assert exc.value.code == "upload_not_found"
