from __future__ import annotations

import pytest

from tests.fakes import http_error
from youtube_mcp.errors import YouTubeMCPError

BODY = {"snippet": {"title": "Original", "description": "Keep me", "tags": ["one", "two"], "categoryId": "28",
                    "defaultLanguage": "en"},
        "status": {"privacyStatus": "unlisted", "license": "creativeCommon", "embeddable": False}}


def approved(fn, *args, **kwargs):
    """Preview, then confirm with the token (simulating the user's approval in chat)."""
    preview = fn(*args, **kwargs)
    assert preview["status"] == "confirmation_required", preview
    return fn(*args, confirmation_token=preview["confirmation_token"], **kwargs)


@pytest.fixture
def vid(env):
    return env.api.add_video(BODY, video_id="abcDEF12345")


# --- update_metadata ---------------------------------------------------------------------

def test_update_title_preserves_other_fields(env, vid):
    preview = env.service.update_metadata(vid, title="New title")
    assert preview["preview"]["changes"] == {"title": {"before": "Original", "after": "New title"}}
    assert env.api.updates == []  # nothing written before approval
    result = env.service.update_metadata(vid, title="New title", confirmation_token=preview["confirmation_token"])
    assert result["status"] == "updated"
    assert result["changed"] == {"title": {"before": "Original", "after": "New title"}}
    snippet = env.api.videos_db[vid]["snippet"]
    assert snippet["description"] == "Keep me"
    assert snippet["tags"] == ["one", "two"]
    assert snippet["categoryId"] == "28" and snippet["defaultLanguage"] == "en"
    sent = env.api.updates[-1]["snippet"]
    assert "thumbnails" not in sent and "channelId" not in sent and "publishedAt" not in sent


def test_update_accepts_url_and_can_clear_tags(env, vid):
    result = approved(env.service.update_metadata, f"https://www.youtube.com/watch?v={vid}&t=10", tags=[])
    assert "tags" not in env.api.videos_db[vid]["snippet"]
    assert result["changed"]["tags"]["before"] == ["one", "two"]


def test_update_no_change_and_nothing_to_update(env, vid):
    assert env.service.update_metadata(vid, title="Original")["status"] == "no_change"
    assert env.api.updates == []
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.update_metadata(vid)
    assert exc.value.code == "invalid_input"


def test_update_unknown_video(env):
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.update_metadata("zzzzzzzzzzz", title="x")
    assert exc.value.code == "video_not_found" and exc.value.action


# --- set_visibility ----------------------------------------------------------------------

def test_set_private_needs_approval_and_preserves_status(env, vid):
    preview = env.service.set_visibility(vid, "private")
    assert preview["status"] == "confirmation_required" and env.api.updates == []
    result = env.service.set_visibility(vid, "private", confirmation_token=preview["confirmation_token"])
    assert env.native_prompts == []  # not public: no native dialog
    assert result["verified"] and result["actual_visibility"] == "private"
    status = env.api.videos_db[vid]["status"]
    assert status["license"] == "creativeCommon" and status["embeddable"] is False


def test_set_public_requires_token_and_confirm_public(env, vid):
    preview = env.service.set_visibility(vid, "public")
    assert preview["status"] == "confirmation_required"
    assert env.api.videos_db[vid]["status"]["privacyStatus"] == "unlisted"
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.set_visibility(vid, "public", confirmation_token=preview["confirmation_token"])
    assert exc.value.code == "public_requires_approval"
    preview = env.service.set_visibility(vid, "public")
    result = env.service.set_visibility(vid, "public", confirmation_token=preview["confirmation_token"],
                                        confirm_public=True)
    assert result["actual_visibility"] == "public" and result["verified"]
    assert any(a["action"] == "set_visibility" for a in env.history.list_actions(vid))


def test_public_without_token_but_with_confirm_public_only_previews(env, vid):
    result = env.service.set_visibility(vid, "public", confirm_public=True)
    assert result["status"] == "confirmation_required"
    assert env.api.videos_db[vid]["status"]["privacyStatus"] == "unlisted"


def test_token_for_one_video_cannot_publish_another(env, vid):
    other = env.api.add_video(BODY, video_id="zyxWVU98765")
    token = env.service.set_visibility(vid, "public")["confirmation_token"]
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.set_visibility(other, "public", confirmation_token=token, confirm_public=True)
    assert exc.value.code == "confirmation_mismatch"


def test_visibility_not_applied_is_reported(env, vid):
    env.api.lock_private = True
    result = approved(env.service.set_visibility, vid, "private")
    assert result["verified"]
    env.api.videos_db[vid]["status"]["privacyStatus"] = "private"
    preview = env.service.set_visibility(vid, "public")
    result = env.service.set_visibility(vid, "public", confirmation_token=preview["confirmation_token"],
                                        confirm_public=True)
    assert not result["verified"] and result["warnings"][0]["code"] == "visibility_not_applied"


def test_set_same_visibility_is_no_change(env, vid):
    assert env.service.set_visibility(vid, "unlisted")["status"] == "no_change"


def test_publish_at_dropped_when_leaving_private(env):
    v = env.api.add_video({"snippet": {"title": "S"}, "status": {"privacyStatus": "private",
                                                                  "publishAt": "2027-01-01T00:00:00Z"}})
    preview = env.service.set_visibility(v, "unlisted")
    assert any("scheduled publish time" in w for w in preview["warnings"])
    env.service.set_visibility(v, "unlisted", confirmation_token=preview["confirmation_token"])
    assert "publishAt" not in env.api.updates[-1]["status"]


# --- set_thumbnail -----------------------------------------------------------------------

def test_thumbnail_two_step(env, vid):
    img = env.image()
    preview = env.service.set_thumbnail(vid, img)
    assert preview["status"] == "confirmation_required" and env.api.thumbnail_sets == []
    result = env.service.set_thumbnail(vid, img, confirmation_token=preview["confirmation_token"])
    assert result["status"] == "updated" and env.api.thumbnail_sets == [vid]


def test_thumbnail_validation(env, vid):
    with pytest.raises(YouTubeMCPError):
        env.service.set_thumbnail(vid, env.image("t.gif"))
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.set_thumbnail(vid, env.image("big.jpg", size=3 * 1024 * 1024))
    assert "2 MB" in exc.value.message


def test_thumbnail_unverified_channel_error(env, vid):
    img = env.image()
    token = env.service.set_thumbnail(vid, img)["confirmation_token"]
    env.api.fail_next["thumbnails.set"] = http_error(
        403, "forbidden", "The authenticated user doesn't have permissions to upload and set custom video thumbnails.")
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.set_thumbnail(vid, img, confirmation_token=token)
    assert exc.value.code == "thumbnail_not_allowed" and "verify" in exc.value.action


# --- read-only tools ---------------------------------------------------------------------

def test_video_status(env, vid):
    s = env.service.get_video_status(vid)
    assert s["state"] == "processing" and s["visibility"] == "unlisted"
    assert s["processing_percent"] == 40.0 and s["processing_seconds_left"] == 30
    env.api.videos_db[vid]["status"]["uploadStatus"] = "processed"
    env.api.videos_db[vid]["processingDetails"] = {"processingStatus": "succeeded"}
    assert env.service.get_video_status(vid)["state"] == "ready"
    env.api.videos_db[vid]["status"].update(uploadStatus="rejected", rejectionReason="duplicate")
    s = env.service.get_video_status(vid)
    assert s["state"] == "rejected" and s["rejection_reason"] == "duplicate"


def test_share_link(env, vid):
    r = env.service.get_share_link(vid)
    assert r["url"] == f"https://youtu.be/{vid}" and not r["copied_to_clipboard"] and env.clipboard == []
    r = env.service.get_share_link(f"youtu.be/{vid}", copy_to_clipboard=True, link_format="watch")
    assert r["url"] == f"https://www.youtube.com/watch?v={vid}" and r["copied_to_clipboard"]
    assert env.clipboard == [r["url"]]
    env.api.videos_db[vid]["status"]["privacyStatus"] = "private"
    assert "PRIVATE" in env.service.get_share_link(vid)["warning"]


def test_list_uploads_youtube_paginates(env):
    for i in range(3):
        env.api.add_video({"snippet": {"title": f"V{i}"}, "status": {"privacyStatus": "unlisted"}})
    page1 = env.service.list_uploads(max_results=2)
    assert len(page1["items"]) == 2 and page1["next_page_token"] and page1["total"] == 3
    assert page1["items"][0]["title"] == "V2" and page1["items"][0]["visibility"] == "unlisted"
    page2 = env.service.list_uploads(max_results=2, page_token=page1["next_page_token"])
    assert len(page2["items"]) == 1 and page2["next_page_token"] is None


def test_channel_info(env):
    info = env.service.get_channel_info()
    assert info["title"] == "Test Channel" and info["channel_id"] == "UC_fake_channel"
    assert info["url"] == "https://www.youtube.com/@testchannel" and info["video_count"] == 2


def test_no_channel(env):
    env.api.channel = None
    env.api.channels = lambda: type("C", (), {"list": lambda self, part, mine: type(
        "R", (), {"execute": lambda self, num_retries=0: {"items": []}})()})()
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.get_channel_info()
    assert exc.value.code == "no_channel"


@pytest.mark.parametrize("value", ["", "abc", "https://example.com/watch?v=abcDEF12345", "abcDEF1234!"])
def test_invalid_video_ids(env, value):
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.get_video_status(value)
    assert exc.value.code == "invalid_input"


@pytest.mark.parametrize("value", ["abcDEF12345", "https://youtu.be/abcDEF12345?si=x",
                                   "https://www.youtube.com/shorts/abcDEF12345", "m.youtube.com/watch?v=abcDEF12345"])
def test_valid_video_id_forms(env, vid, value):
    assert env.service.get_video_status(value)["video_id"] == vid
