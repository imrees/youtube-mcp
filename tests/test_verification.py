"""Phase 10: post-write verification under eventual consistency, write-outcome classification,
pagination honesty and richer read-backs. Simulates the defects found in live integration tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.fakes import http_error
from youtube_mcp.errors import YouTubeMCPError
from youtube_mcp.verify import CONFIRMED, NOT_CONFIRMED, UNVERIFIABLE, Verifier

SRT = "1\n00:00:01,000 --> 00:00:03,000\nHello there\n\n2\n00:00:04,000 --> 00:00:05,000\nWorld\n"


def approve(fn, *args, **kwargs):
    preview = fn(*args, **kwargs)
    assert preview["status"] == "confirmation_required", preview
    return fn(*args, confirmation_token=preview["confirmation_token"], **kwargs)


def video(env, privacy="private", title="Test"):
    return env.api.add_video({"snippet": {"title": title, "description": "d"}, "status": {"privacyStatus": privacy}})


def writes_sent(env, name):
    return env.api.calls.count(name)


# --- Verifier unit ------------------------------------------------------------------------

def test_verifier_backoff_is_bounded_and_reads_only():
    sleeps, reads = [], []
    v = Verifier(sleep=sleeps.append, clock=lambda: sum(sleeps))
    result = v.wait_for(lambda: reads.append(1) or "old", lambda x: x == "new")
    assert result.status == NOT_CONFIRMED and result.attempts == 6 and len(reads) == 6
    assert sleeps == [0.5, 1.0, 2.0, 4.0, 8.0] and 10 <= sum(sleeps) <= 20


def test_verifier_confirms_after_stale_reads():
    sleeps, values = [], iter(["old", "old", "new"])
    result = Verifier(sleep=sleeps.append).wait_for(lambda: next(values), lambda x: x == "new")
    assert result.status == CONFIRMED and result.attempts == 3 and sleeps == [0.5, 1.0]


def test_verifier_unverifiable_and_stops_on_auth_errors():
    def failing():
        raise YouTubeMCPError("network_error", "down", retryable=True)
    r = Verifier(sleep=lambda s: None).wait_for(failing, lambda x: True)
    assert r.status == UNVERIFIABLE and r.attempts == 6 and r.last_error["code"] == "network_error"

    def expired():
        raise YouTubeMCPError("auth_expired", "expired")
    r = Verifier(sleep=lambda s: None).wait_for(expired, lambda x: True)
    assert r.status == UNVERIFIABLE and r.attempts == 1


# --- the four live defects, reproduced with delayed consistency ----------------------------

def test_create_playlist_not_found_at_first_is_confirmed_not_an_error(env):
    env.api.read_lag = 3  # live: playlists.list(id=new) returned nothing right after insert
    done = approve(env.service.playlists.create_playlist, "Integration")
    assert done["status"] == "created" and done["verified"] and done["verification"]["attempts"] == 4
    assert done["playlist_id"] and done["title"] == "Integration"
    assert writes_sent(env, "playlists.insert") == 1  # the write is never repeated


def test_update_playlist_reports_the_new_state_after_stale_reads(env):
    pid = env.api.add_playlist("P", "private", description="old description")
    env.api.read_lag = 2
    done = approve(env.service.playlists.update_playlist, pid, description="new description")
    assert done["verified"] and done["description"] == "new description"  # live bug: returned the old one
    assert writes_sent(env, "playlists.update") == 1


def test_schedule_publish_confirmed_after_propagation_delay(env):
    now = datetime.now(timezone.utc)
    env.service.scheduling.now = lambda: now
    vid = video(env)
    when = (now + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:00Z")
    env.api.read_lag = 2  # live bug: schedule_not_verified despite success
    done = approve(env.service.scheduling.schedule_publish, vid, when, confirm_public=True)
    assert done["status"] == "scheduled" and done["verified"] and not done["requires_manual_attention"]
    assert writes_sent(env, "videos.update") == 1


def test_cancel_schedule_confirmed_after_propagation_delay(env):
    vid = video(env)
    env.api.videos_db[vid]["status"]["publishAt"] = "2027-01-01T00:00:00Z"
    env.api.read_lag = 3  # live bug: cancel_not_verified despite success
    done = approve(env.service.scheduling.cancel_publish_schedule, vid)
    assert done["status"] == "cancelled" and done["verified"]


# --- verification timeout / inconclusive ----------------------------------------------------

@pytest.mark.parametrize("tool", ["set_visibility", "schedule", "update_metadata", "create_playlist", "add",
                                  "remove", "captions"])
def test_verification_timeout_returns_structured_warning_and_never_rewrites(env, tool):
    now = datetime.now(timezone.utc)
    env.service.scheduling.now = lambda: now
    vid = video(env, "unlisted")
    pid = env.api.add_playlist("P", "private")
    srt = env.tmp / "s.srt"
    srt.write_text(SRT)
    if tool == "remove":
        approve(env.service.playlists.add_to_playlist, pid, vid)
    env.api.read_lag = 100  # never consistent within the verification window
    calls = {
        "set_visibility": lambda: approve(env.service.set_visibility, vid, "private"),
        "schedule": lambda: approve(env.service.scheduling.schedule_publish, video(env), "2099-01-01T10:00:00Z",
                                    confirm_public=True),
        "update_metadata": lambda: approve(env.service.update_metadata, vid, title="New"),
        "create_playlist": lambda: approve(env.service.playlists.create_playlist, "New"),
        "add": lambda: approve(env.service.playlists.add_to_playlist, pid, vid),
        "remove": lambda: approve(env.service.playlists.remove_from_playlist, pid, video_id=vid),
        "captions": lambda: approve(env.service.captions.upload_captions, vid, str(srt)),
    }
    if tool == "remove":  # the lagged view hides the item we want to remove: let the preview see it
        env.api.read_lag = 0
        preview = env.service.playlists.remove_from_playlist(pid, video_id=vid)
        env.api.read_lag = 100
        done = env.service.playlists.remove_from_playlist(pid, video_id=vid,
                                                          confirmation_token=preview["confirmation_token"])
    else:
        done = calls[tool]()
    before = list(env.api.calls)
    assert done["ok"] and done["write_accepted"] and done["verified"] is False
    assert done["verification"]["status"] in (NOT_CONFIRMED, UNVERIFIABLE)
    assert any(w["code"] == "verification_pending" and "Do NOT repeat" in w["action"] for w in done["warnings"])
    if tool in ("set_visibility", "schedule"):
        assert done["requires_manual_attention"] is True
    assert sum(env.sleeps) <= 20 * 2  # bounded (remove previews add their own reads)
    assert env.api.calls == before  # nothing else happens after the report


# --- write outcome classification -----------------------------------------------------------

def test_connection_failure_during_write_is_uncertain_and_not_retried(env):
    env.api.fail_next["playlists.insert"] = ConnectionError("reset after send")
    preview = env.service.playlists.create_playlist("X")
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.playlists.create_playlist("X", confirmation_token=preview["confirmation_token"])
    assert exc.value.code == "write_outcome_uncertain" and "Do NOT repeat" in exc.value.action
    assert writes_sent(env, "playlists.insert") == 1


def test_server_error_during_write_is_uncertain_but_rejection_is_a_failure(env):
    vid = video(env, "unlisted")
    env.api.fail_next["videos.update"] = http_error(503, "backendError", "unavailable")
    with pytest.raises(YouTubeMCPError) as exc:
        approve(env.service.set_visibility, vid, "private")
    assert exc.value.code == "write_outcome_uncertain" and exc.value.details["resource_id"] == vid
    env.api.fail_next["videos.update"] = http_error(403, "forbidden", "nope")
    with pytest.raises(YouTubeMCPError) as exc:
        approve(env.service.set_visibility, vid, "private")
    assert exc.value.code == "permission_denied"


def test_writes_are_sent_without_library_retries_reads_with(env):
    vid = video(env, "unlisted")
    approve(env.service.set_visibility, vid, "private")
    pid = env.api.add_playlist()
    approve(env.service.playlists.add_to_playlist, pid, vid)
    retries = dict(env.api.retry_args)
    assert retries["videos.update"] == 0 and retries["playlistItems.insert"] == 0
    assert retries["videos.list"] == 3 and retries["playlistItems.list"] == 3


# --- other write tools under stale reads -----------------------------------------------------

def test_visibility_locked_private_reports_not_applied(env):
    vid = video(env, "private")
    env.api.lock_private = True
    done = approve(env.service.set_visibility, vid, "unlisted")
    assert done["verified"] is False and done["warnings"][0]["code"] == "visibility_not_applied"
    assert done["requires_manual_attention"] and done["actual_visibility"] == "private"


def test_update_metadata_and_add_remove_confirmed_after_lag(env):
    vid = video(env, "unlisted")
    env.api.read_lag = 2
    assert approve(env.service.update_metadata, vid, title="New", tags=["a"])["verified"]
    pid = env.api.add_playlist()
    env.api.read_lag = 2
    added = approve(env.service.playlists.add_to_playlist, pid, vid)
    assert added["verified"] and added["playlist_item_id"]
    env.api.read_lag = 0
    preview = env.service.playlists.remove_from_playlist(pid, video_id=vid)
    env.api.read_lag = 2
    assert env.service.playlists.remove_from_playlist(pid, video_id=vid,
                                                      confirmation_token=preview["confirmation_token"])["verified"]


def test_newly_uploaded_video_not_found_briefly_still_verifies_visibility(env):
    env.api.read_lag = 0
    path = env.video()
    preview = env.service.upload_video(path, title="T")
    original_add = env.api.add_video

    def add_then_lag(body, video_id=None):
        vid = original_add(body, video_id)
        env.api._lagged[("video", vid)] = [None, 2]  # "not found" for two reads
        return vid
    env.api.add_video = add_then_lag
    result = env.service.upload_video(path, title="T", confirmation_token=preview["confirmation_token"],
                                      background=False)
    assert result["actual_visibility"] == "unlisted"
    assert not any(w["code"] == "verification_failed" for w in result["warnings"])


def test_unexpected_public_is_corrected_without_waiting(env):
    env.api.upload_privacy_override = "public"
    path = env.video()
    preview = env.service.upload_video(path, title="T")
    sleeps_before = len(env.sleeps)
    result = env.service.upload_video(path, title="T", confirmation_token=preview["confirmation_token"],
                                      background=False)
    assert result["actual_visibility"] == "unlisted"
    assert env.sleeps[sleeps_before:] == []  # no backoff before the correction


# --- captions: verification and download ----------------------------------------------------

def test_caption_upload_verified_and_content_compared(env):
    vid = video(env)
    srt = env.tmp / "s.srt"
    srt.write_text(SRT)
    env.api.read_lag = 2
    done = approve(env.service.captions.upload_captions, vid, str(srt), name="T", verify_content=True)
    assert done["verified"] and done["caption_id"]
    assert done["content_verification"]["status"] == "matches"
    assert done["content_verification"]["local_cue_count"] == 2


def test_caption_file_replacement_verified_by_last_updated(env):
    vid = video(env)
    srt = env.tmp / "s.srt"
    srt.write_text(SRT)
    cid = approve(env.service.captions.upload_captions, vid, str(srt))["caption_id"]
    srt2 = env.tmp / "s2.srt"
    srt2.write_text(SRT.replace("World", "Everyone"))
    env.api.read_lag = 2
    done = approve(env.service.captions.update_captions, vid, cid, file_path=str(srt2), verify_content=True)
    assert done["verified"] and done["content_verification"]["status"] == "matches"


def test_download_captions_returns_text_and_checks_ownership(env):
    vid = video(env)
    srt = env.tmp / "s.srt"
    srt.write_text(SRT)
    cid = approve(env.service.captions.upload_captions, vid, str(srt))["caption_id"]
    out = env.service.captions.download_captions(vid, cid)
    assert out["content"] == SRT and out["cue_count"] == 2 and not out["truncated"]
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.captions.download_captions(video(env), cid)  # track belongs to another video
    assert exc.value.code == "caption_not_found"
    with pytest.raises(YouTubeMCPError):
        env.service.captions.download_captions(vid, cid, fmt="docx")


def test_download_forbidden_is_reported_as_youtube_limitation(env):
    vid = video(env)
    srt = env.tmp / "s.srt"
    srt.write_text(SRT)
    cid = approve(env.service.captions.upload_captions, vid, str(srt))["caption_id"]
    env.api.fail_next["captions.download"] = http_error(403, "forbidden", "not allowed")
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.captions.download_captions(vid, cid)
    assert exc.value.code == "unsupported_by_youtube"


# --- read-backs -------------------------------------------------------------------------------

def test_get_video_status_includes_full_metadata(env):
    vid = env.api.add_video({"snippet": {"title": "T", "description": "Desc", "tags": ["x"], "categoryId": "27",
                                         "defaultLanguage": "en"},
                             "status": {"privacyStatus": "private", "publishAt": "2027-01-01T00:00:00Z"}})
    s = env.service.get_video_status(vid)
    assert (s["description"], s["tags"], s["category_id"], s["default_language"]) == ("Desc", ["x"], "27", "en")
    assert s["thumbnails"]["default"] == "x" and s["scheduled"] and s["publish_at"] == "2027-01-01T00:00:00Z"
    assert s["visibility"] == "private" and "license" in s


def test_thumbnail_acceptance_is_not_claimed_as_image_verification(env):
    vid = video(env)
    img = env.tmp / "t.jpg"
    img.write_bytes(b"\xff\xd8" + b"0" * 100)
    done = approve(env.service.set_thumbnail, vid, str(img))
    assert done["accepted_by_youtube"] is True and done["image_independently_verified"] is False


# --- pagination ----------------------------------------------------------------------------------

def test_playlist_pagination_reports_youtube_total_separately(env, monkeypatch):
    for n in range(3):
        env.api.add_playlist(f"P{n}")
    real = env.api.playlists().list

    def inflated(*a, **k):  # YouTube reports more than it returns (observed live: 22 vs 15)
        req = real(*a, **k)
        fn = req.fn
        req.fn = lambda: {**fn(), "pageInfo": {"totalResults": 5}}
        return req
    monkeypatch.setattr(type(env.api), "playlists", lambda self: type("PL", (), {"list": staticmethod(inflated)})())
    out = env.service.playlists.list_playlists(max_results=50)
    assert out["returned_count"] == 3 and out["api_reported_total"] == 5 and out["has_more"] is False
    assert out["discrepancy"]["returned_count"] == 3 and len(out["items"]) == 3  # nothing invented


def test_pagination_tokens_preserved(env):
    pid = env.api.add_playlist()
    for _ in range(3):
        approve(env.service.playlists.add_to_playlist, pid, video(env, "unlisted"))
    first = env.service.playlists.list_playlist_items(pid, max_results=2)
    assert first["has_more"] and first["next_page_token"] and first["returned_count"] == 2
    second = env.service.playlists.list_playlist_items(pid, max_results=2, page_token=first["next_page_token"])
    assert second["returned_count"] == 1 and not second["has_more"] and "discrepancy" not in second


def test_verify_content_is_shown_in_caption_previews_and_bound_to_the_token(env):
    vid = video(env)
    srt = env.tmp / "s.srt"
    srt.write_text(SRT)
    plain = env.service.captions.upload_captions(vid, str(srt), name="T")
    assert plain["preview"]["verify_content"].startswith("no")
    preview = env.service.captions.upload_captions(vid, str(srt), name="T", verify_content=True)
    assert preview["preview"]["verify_content"].startswith("yes") and "quota" in preview["preview"]["verify_content"]
    with pytest.raises(YouTubeMCPError) as exc:  # approved with verification; cannot be confirmed without it
        env.service.captions.upload_captions(vid, str(srt), name="T", confirmation_token=preview["confirmation_token"])
    assert exc.value.code == "confirmation_mismatch"
    cid = approve(env.service.captions.upload_captions, vid, str(srt), name="T")["caption_id"]
    upd = env.service.captions.update_captions(vid, cid, is_draft=True, verify_content=True)
    assert "no effect" in upd["preview"]["verify_content"]
    srt.write_text(SRT.replace("World", "There"))
    upd = env.service.captions.update_captions(vid, cid, file_path=str(srt), verify_content=True)
    assert upd["preview"]["verify_content"].startswith("yes")
