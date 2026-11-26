"""Captions, video categories and playlists (Phase 7), against the in-memory fake API."""

from __future__ import annotations

import pytest

from youtube_mcp.errors import YouTubeMCPError

SRT = "1\n00:00:01,000 --> 00:00:03,000\nHello\n\n2\n00:00:04,000 --> 00:00:05,500\nWorld\n"
VTT = "WEBVTT\n\n00:01.000 --> 00:03.000\nHello\n\n00:00:04.000 --> 00:00:05.500\nWorld\n"


def video(env, privacy="unlisted"):
    return env.api.add_video({"snippet": {"title": "Demo"}, "status": {"privacyStatus": privacy}})


def caption_file(env, name="subs.srt", text=SRT):
    path = env.tmp / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def approve(fn, *args, **kwargs):
    preview = fn(*args, **kwargs)
    assert preview["status"] == "confirmation_required", preview
    return fn(*args, confirmation_token=preview["confirmation_token"], **kwargs)


# --- captions --------------------------------------------------------------------------

@pytest.mark.parametrize("name,text,cues", [("a.srt", SRT, 2), ("a.vtt", VTT, 2)])
def test_upload_captions_srt_and_vtt(env, name, text, cues):
    vid = video(env)
    path = caption_file(env, name, text)
    preview = env.service.captions.upload_captions(vid, path, language="en-US", name="English")
    assert preview["preview"]["cue_count"] == cues and preview["preview"]["format"] == name[-3:]
    assert "captions.insert" not in env.api.calls  # nothing written before approval
    done = env.service.captions.upload_captions(vid, path, language="en-US", name="English",
                                                confirmation_token=preview["confirmation_token"])
    assert done["status"] == "uploaded" and done["track"]["language"] == "en-US"
    assert done["track"]["name"] == "English" and done["track"]["is_draft"] is False
    assert env.history.list_actions(vid)[0]["action"] == "upload_captions"


def test_list_captions(env):
    vid = video(env)
    approve(env.service.captions.upload_captions, vid, caption_file(env), language="hi", name="Hindi", is_draft=True)
    out = env.service.captions.list_captions(vid)
    assert out["count"] == 1 and out["tracks"][0]["language"] == "hi" and out["tracks"][0]["is_draft"] is True


def test_upload_captions_refuses_existing_track(env):
    vid = video(env)
    approve(env.service.captions.upload_captions, vid, caption_file(env), language="en", name="")
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.captions.upload_captions(vid, caption_file(env), language="en", name="")
    assert exc.value.code == "caption_exists" and exc.value.details["caption_id"]


def test_upload_captions_token_bound_to_file_content(env):
    vid = video(env)
    path = caption_file(env)
    preview = env.service.captions.upload_captions(vid, path)
    caption_file(env, text=SRT + "\n3\n00:00:06,000 --> 00:00:07,000\nChanged\n")
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.captions.upload_captions(vid, path, confirmation_token=preview["confirmation_token"])
    assert exc.value.code == "confirmation_mismatch"


@pytest.mark.parametrize("name,text,fragment", [
    ("a.txt", SRT, "not supported"),
    ("a.vtt", SRT, "WEBVTT"),
    ("a.srt", "just text", "No caption cues"),
])
def test_caption_file_validation(env, name, text, fragment):
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.captions.upload_captions(video(env), caption_file(env, name, text))
    assert exc.value.code == "invalid_input" and fragment in exc.value.message


def test_caption_language_validation(env):
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.captions.upload_captions(video(env), caption_file(env), language="english!")
    assert exc.value.code == "invalid_input"


def test_update_captions_replaces_file_and_preserves_snippet(env):
    vid = video(env)
    up = approve(env.service.captions.upload_captions, vid, caption_file(env), language="en", name="Main", is_draft=True)
    cid = up["track"]["caption_id"]
    new = caption_file(env, "v2.vtt", VTT)
    done = approve(env.service.captions.update_captions, vid, cid, file_path=new, is_draft=False)
    assert done["verified"] and done["track"]["is_draft"] is False
    sent = env.api.caption_updates[-1]["snippet"]
    assert sent == {"videoId": vid, "language": "en", "name": "Main", "isDraft": False}  # nothing erased
    assert env.api.caption_media[cid] is not None


def test_update_captions_rename_is_unsupported_by_youtube(env):
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.captions.update_captions(video(env), "CAP1", name="New name")
    assert exc.value.code == "unsupported_by_youtube"


def test_update_captions_unknown_track(env):
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.captions.update_captions(video(env), "nope", is_draft=True)
    assert exc.value.code == "caption_not_found"


# --- categories ------------------------------------------------------------------------

def test_list_categories_region_and_default(env):
    out = env.service.categories.list_categories("in")
    assert out["region_code"] == "IN" and out["default_category_id"] == "22"
    ids = {c["category_id"]: c for c in out["categories"]}
    assert ids["22"]["is_default"] and ids["30"]["assignable"] is False
    assert all(c["assignable"] for c in env.service.categories.list_categories("US", assignable_only=True)["categories"])
    env.service.categories.list_categories("IN")
    assert env.api.calls.count("videoCategories.list") == 2  # IN cached, US separate


def test_upload_accepts_valid_category_and_rejects_invalid(env):
    path = env.video()
    preview = env.service.upload_video(path, title="T", category_id="27")
    assert preview["preview"]["category_id"] == "27"
    for bad in ("30", "999"):  # not assignable / unknown
        with pytest.raises(YouTubeMCPError) as exc:
            env.service.upload_video(path, title="T", category_id=bad)
        assert exc.value.code == "invalid_input"


def test_default_category_needs_no_lookup(env):
    env.service.upload_video(env.video(), title="T")
    assert "videoCategories.list" not in env.api.calls


def test_update_metadata_validates_category(env):
    vid = video(env)
    assert approve(env.service.update_metadata, vid, category_id="27")["status"] == "updated"
    with pytest.raises(YouTubeMCPError):
        env.service.update_metadata(vid, category_id="30")


def test_category_lookup_failure_falls_back_to_youtube_validation(env):
    env.api.fail_next["videoCategories.list"] = ConnectionError("offline")
    assert env.service.categories.ensure_assignable("27") == "27"


# --- playlists -------------------------------------------------------------------------

def test_list_playlists_and_items(env):
    pid = env.api.add_playlist("Mine", "unlisted")
    vid = video(env)
    approve(env.service.playlists.add_to_playlist, pid, vid)
    lists = env.service.playlists.list_playlists()
    assert lists["items"][0]["playlist_id"] == pid and lists["items"][0]["item_count"] == 1
    items = env.service.playlists.list_playlist_items(pid)
    assert items["items"][0]["video_id"] == vid and items["items"][0]["playlist_item_id"]


def test_create_playlist_private_by_default_with_approval(env):
    preview = env.service.playlists.create_playlist("Tutorials", "How-tos")
    assert preview["preview"]["privacy"] == "private"
    assert "playlists.insert" not in env.api.calls
    done = env.service.playlists.create_playlist("Tutorials", "How-tos",
                                                 confirmation_token=preview["confirmation_token"])
    assert done["status"] == "created" and done["privacy"] == "private" and done["verified"]


def test_public_playlist_needs_confirm_public(env):
    preview = env.service.playlists.create_playlist("Open", privacy="public")
    assert preview["warnings"]
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.playlists.create_playlist("Open", privacy="public", confirmation_token=preview["confirmation_token"])
    assert exc.value.code == "public_requires_approval"
    assert not env.api.playlists_db


def test_update_playlist_preserves_other_fields(env):
    pid = env.api.add_playlist("Old", "private", description="Keep me")
    done = approve(env.service.playlists.update_playlist, pid, title="New")
    assert done["status"] == "updated" and done["title"] == "New"
    sent = env.api.playlist_updates[-1]
    assert sent["snippet"]["description"] == "Keep me" and sent["status"]["privacyStatus"] == "private"
    assert env.service.playlists.update_playlist(pid, title="New")["status"] == "no_change"


def test_update_playlist_to_public_needs_confirm_public(env):
    pid = env.api.add_playlist("P", "private")
    preview = env.service.playlists.update_playlist(pid, privacy="public")
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.playlists.update_playlist(pid, privacy="public", confirmation_token=preview["confirmation_token"])
    assert exc.value.code == "public_requires_approval"
    assert env.api.playlists_db[pid]["status"]["privacyStatus"] == "private"


def test_add_to_playlist_prevents_duplicates(env):
    pid, vid = env.api.add_playlist(), video(env)
    approve(env.service.playlists.add_to_playlist, pid, vid)
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.playlists.add_to_playlist(pid, vid)
    assert exc.value.code == "playlist_duplicate"
    assert len(env.api.playlist_items[pid]) == 1


def test_add_to_playlist_rechecks_duplicate_at_execution(env):
    pid, vid = env.api.add_playlist(), video(env)
    p1 = env.service.playlists.add_to_playlist(pid, vid)
    p2 = env.service.playlists.add_to_playlist(pid, vid)
    env.service.playlists.add_to_playlist(pid, vid, confirmation_token=p1["confirmation_token"])
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.playlists.add_to_playlist(pid, vid, confirmation_token=p2["confirmation_token"])
    assert exc.value.code == "playlist_duplicate" and len(env.api.playlist_items[pid]) == 1


def test_remove_from_playlist_keeps_video(env):
    pid, vid = env.api.add_playlist(), video(env)
    approve(env.service.playlists.add_to_playlist, pid, vid)
    preview = env.service.playlists.remove_from_playlist(pid, video_id=vid)
    assert "not deleted" in preview["preview"]["effect"] and env.api.playlist_items[pid]
    done = env.service.playlists.remove_from_playlist(pid, video_id=vid,
                                                      confirmation_token=preview["confirmation_token"])
    assert done["status"] == "removed" and done["verified"]
    assert not env.api.playlist_items[pid] and vid in env.api.videos_db


def test_remove_by_playlist_item_id_and_missing(env):
    pid, vid = env.api.add_playlist(), video(env)
    item = approve(env.service.playlists.add_to_playlist, pid, vid)["playlist_item_id"]
    assert approve(env.service.playlists.remove_from_playlist, pid, playlist_item_id=item)["status"] == "removed"
    assert env.service.playlists.remove_from_playlist(pid, video_id=vid)["status"] == "no_change"
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.playlists.remove_from_playlist(pid, playlist_item_id="nope")
    assert exc.value.code == "playlist_item_not_found"


def test_unknown_playlist_maps_error(env):
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.playlists.list_playlist_items("PLmissing")
    assert exc.value.code == "playlist_not_found"
