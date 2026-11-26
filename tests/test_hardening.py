"""Regression tests for the final hardening round (independent review findings H1, H2, M1-M4)."""

from __future__ import annotations

import pytest

from youtube_mcp.errors import YouTubeMCPError

SRT = "1\n00:00:01,000 --> 00:00:03,000\nHello there\n\n2\n00:00:04,000 --> 00:00:05,000\nWorld\n"
EVIL_SRT = SRT.replace("Hello there", "Something else entirely")


def approve(fn, *args, **kwargs):
    preview = fn(*args, **kwargs)
    assert preview["status"] == "confirmation_required", preview
    return fn(*args, confirmation_token=preview["confirmation_token"], **kwargs)


def video(env, privacy="private", title="Test"):
    return env.api.add_video({"snippet": {"title": title, "description": "d"}, "status": {"privacyStatus": privacy}})


def staging_empty(env) -> bool:
    return not any(env.service.staging.root.iterdir())


# --- H2: thumbnails and captions are sent from a verified snapshot ------------------------------

def test_thumbnail_is_sent_from_snapshot_even_if_original_changes_during_api_read(env):
    vid = video(env)
    img = env.tmp / "t.jpg"
    approved = b"\xff\xd8" + b"A" * 500
    img.write_bytes(approved)
    preview = env.service.set_thumbnail(vid, str(img))
    env.api.before_media_read = lambda: img.write_bytes(b"\xff\xd8" + b"B" * 500)  # swapped mid-call
    env.service.set_thumbnail(vid, str(img), confirmation_token=preview["confirmation_token"])
    sent_name, sent = env.api.thumbnail_media[-1]
    assert sent == approved and sent_name != str(img)
    assert staging_empty(env)


def test_thumbnail_changed_after_hashing_but_before_snapshot_is_refused(env):
    vid = video(env)
    img = env.tmp / "t.jpg"
    img.write_bytes(b"\xff\xd8" + b"A" * 500)
    preview = env.service.set_thumbnail(vid, str(img))
    real_clone = env.service.staging._clone

    def tamper_then_clone(src, dst):  # the file changes after the confirm call hashed it
        src.write_bytes(b"\xff\xd8" + b"B" * 500)
        return real_clone(src, dst)

    env.service.staging._clone = tamper_then_clone
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.set_thumbnail(vid, str(img), confirmation_token=preview["confirmation_token"])
    assert exc.value.code == "file_changed_since_approval"
    assert env.api.thumbnail_sets == [] and staging_empty(env)


def test_caption_upload_sends_and_verifies_against_snapshot(env):
    vid = video(env)
    srt = env.tmp / "s.srt"
    srt.write_text(SRT)
    preview = env.service.captions.upload_captions(vid, str(srt), name="T", verify_content=True)
    env.api.before_media_read = lambda: srt.write_text(EVIL_SRT)
    done = env.service.captions.upload_captions(vid, str(srt), name="T", verify_content=True,
                                                confirmation_token=preview["confirmation_token"])
    assert env.api.caption_text[done["caption_id"]].decode() == SRT  # approved text, not the swapped file
    assert done["content_verification"]["status"] == "matches"  # compared with the approved snapshot
    assert staging_empty(env)


def test_caption_update_sends_snapshot_and_cleans_up_on_failure(env):
    vid = video(env)
    srt = env.tmp / "s.srt"
    srt.write_text(SRT)
    cid = approve(env.service.captions.upload_captions, vid, str(srt), name="T")["caption_id"]
    new = SRT.replace("World", "Everyone")
    srt.write_text(new)
    preview = env.service.captions.update_captions(vid, cid, file_path=str(srt))
    env.api.before_media_read = lambda: srt.write_text(EVIL_SRT)
    env.service.captions.update_captions(vid, cid, file_path=str(srt), confirmation_token=preview["confirmation_token"])
    assert env.api.caption_text[cid].decode() == new and staging_empty(env)

    srt.write_text(SRT)
    preview = env.service.captions.update_captions(vid, cid, file_path=str(srt))
    env.api.before_media_read = None
    env.api.fail_next["captions.update"] = YouTubeMCPError("caption_not_found", "gone")
    with pytest.raises(YouTubeMCPError):
        env.service.captions.update_captions(vid, cid, file_path=str(srt),
                                             confirmation_token=preview["confirmation_token"])
    assert staging_empty(env)


# --- M1: correcting an unexpectedly public upload ---------------------------------------------

def test_unexpected_public_correction_is_only_claimed_when_read_back_confirms(env):
    env.api.upload_privacy_override = "public"
    client = env.service.uploads.client
    sent = []
    client.update_video = lambda vid, part, fields: sent.append(fields) or {}  # accepted, but never applied
    result = env.upload(env.video(), title="T")
    w = result["warnings"][0]
    assert w["code"] == "unexpectedly_public" and w["urgent"] and w["requires_manual_attention"]
    assert "studio.youtube.com" in w["studio_url"] and w["verification"]["status"] == "not_confirmed"
    assert not any(x["code"] == "visibility_corrected" for x in result["warnings"])
    assert result["actual_visibility"] == "public"
    assert len(sent) == 1  # the corrective write is never repeated
    assert env.notifications[-1][0] == "YouTube upload: CHECK VISIBILITY NOW"


def test_unexpected_public_correction_failure_is_urgent(env):
    from tests.fakes import http_error

    env.api.upload_privacy_override = "public"
    env.api.fail_next["videos.update"] = http_error(503, "backendError", "unavailable")
    result = env.upload(env.video(), title="T")
    w = result["warnings"][0]
    assert w["code"] == "unexpectedly_public" and w["urgent"] and "may still be public" in w["message"]
    assert result["actual_visibility"] == "public"
    assert env.api.calls.count("videos.update") == 1


# --- H1: indirect public exposure through playlists ---------------------------------------------

def put(env, pid, vid):
    env.service.client.insert_playlist_item({"snippet": {"playlistId": pid,
                                                         "resourceId": {"kind": "youtube#video", "videoId": vid}}})


def test_making_playlist_public_discloses_unlisted_videos_and_confirms_natively(env):
    pid = env.api.add_playlist("Course", "private")
    hidden, shown, priv = video(env, "unlisted", "Hidden"), video(env, "public", "Shown"), video(env, "private", "P")
    for v in (hidden, shown, priv):
        put(env, pid, v)
    preview = env.service.playlists.update_playlist(pid, privacy="public")
    exp = preview["preview"]["unlisted_videos_exposed"]
    assert exp["count"] == 1 and exp["videos"][0]["video_id"] == hidden and exp["check_complete"]
    assert any("1 UNLISTED video" in w for w in preview["warnings"])
    env.service.playlists.update_playlist(pid, privacy="public", confirm_public=True,
                                          confirmation_token=preview["confirmation_token"])
    action, details = env.native_prompts[-1]
    d = dict(details)
    assert action == "Make a playlist PUBLIC" and d["Unlisted videos exposed"] == "1" and d["Exposed video"] == "Hidden"
    assert env.api.playlists_db[pid]["status"]["privacyStatus"] == "public"


def test_playlist_exposure_change_after_preview_requires_fresh_preview(env):
    pid = env.api.add_playlist("Course", "private")
    preview = env.service.playlists.update_playlist(pid, privacy="public")
    assert preview["preview"]["unlisted_videos_exposed"]["count"] == 0
    put(env, pid, video(env, "unlisted", "Added later"))  # exposure grows after the user approved
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.playlists.update_playlist(pid, privacy="public", confirm_public=True,
                                              confirmation_token=preview["confirmation_token"])
    assert exc.value.code == "confirmation_mismatch"
    assert env.api.playlists_db[pid]["status"]["privacyStatus"] == "private" and env.native_prompts == []


def test_private_to_unlisted_in_public_playlist_needs_public_approval_and_dialog(env):
    vid = video(env, "private", "Draft")
    pub = env.api.add_playlist("Public list", "public")
    env.api.add_playlist("Private list", "private")
    put(env, pub, vid)
    preview = env.service.set_visibility(vid, "unlisted")
    assert preview["preview"]["public_playlists_containing_video"] == [{"playlist_id": pub, "title": "Public list"}]
    assert "confirm_public=true" in preview["next_step"] and "PUBLIC playlist" in preview["warnings"][0]
    with pytest.raises(YouTubeMCPError) as exc:  # confirm_public is required for this exposure
        env.service.set_visibility(vid, "unlisted", confirmation_token=preview["confirmation_token"])
    assert exc.value.code == "public_requires_approval"
    preview = env.service.set_visibility(vid, "unlisted")
    env.native_answer = "cancel"
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.set_visibility(vid, "unlisted", confirm_public=True, confirmation_token=preview["confirmation_token"])
    assert exc.value.code == "native_confirmation_denied"
    assert env.api.videos_db[vid]["status"]["privacyStatus"] == "private"
    env.native_answer = "allow"
    preview = env.service.set_visibility(vid, "unlisted")
    done = env.service.set_visibility(vid, "unlisted", confirm_public=True,
                                      confirmation_token=preview["confirmation_token"])
    assert done["actual_visibility"] == "unlisted"
    assert dict(env.native_prompts[-1][1])["Public playlist"] == "Public list"


def test_private_to_unlisted_without_public_playlists_is_unchanged(env):
    vid = video(env, "private")
    put(env, env.api.add_playlist("Private list", "private"), vid)
    preview = env.service.set_visibility(vid, "unlisted")
    assert preview["preview"]["public_playlists_containing_video"] == [] and "confirm_public" not in preview["next_step"]
    env.service.set_visibility(vid, "unlisted", confirmation_token=preview["confirmation_token"])
    assert env.native_prompts == [] and env.api.videos_db[vid]["status"]["privacyStatus"] == "unlisted"


def test_video_added_to_public_playlist_after_preview_requires_fresh_preview(env):
    vid = video(env, "private")
    preview = env.service.set_visibility(vid, "unlisted")
    put(env, env.api.add_playlist("Public list", "public"), vid)
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.set_visibility(vid, "unlisted", confirm_public=True, confirmation_token=preview["confirmation_token"])
    assert exc.value.code == "confirmation_mismatch"
    assert env.api.videos_db[vid]["status"]["privacyStatus"] == "private"


def test_add_to_playlist_exposure_is_bound_to_the_approval(env):
    pid = env.api.add_playlist("List", "private")
    vid = video(env, "unlisted")
    preview = env.service.playlists.add_to_playlist(pid, vid)
    assert not preview["warnings"]
    env.api.playlists_db[pid]["status"]["privacyStatus"] = "public"  # made public elsewhere after the preview
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.playlists.add_to_playlist(pid, vid, confirmation_token=preview["confirmation_token"])
    assert exc.value.code == "confirmation_mismatch" and env.api.playlist_items[pid] == []


# --- M2: concurrent add_to_playlist from several local processes -----------------------------

def test_concurrent_adds_create_only_one_entry(env):
    import threading
    import time

    pid = env.api.add_playlist("List", "private")
    vid = video(env)
    tokens = [env.service.playlists.add_to_playlist(pid, vid)["confirmation_token"] for _ in range(2)]
    client = env.service.client
    real_insert = client.insert_playlist_item

    def slow_insert(body):  # widen the race window between the duplicate check and the insert
        time.sleep(0.2)
        return real_insert(body)

    client.insert_playlist_item = slow_insert
    outcomes = []

    def run(token):
        try:
            outcomes.append(env.service.playlists.add_to_playlist(pid, vid, confirmation_token=token)["status"])
        except YouTubeMCPError as err:
            outcomes.append(err.code)

    threads = [threading.Thread(target=run, args=(t,)) for t in tokens]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["added", "playlist_duplicate"]
    assert len(env.api.playlist_items[pid]) == 1 and env.api.calls.count("playlistItems.insert") == 1


def test_recent_local_add_counts_even_when_youtube_list_lags(env):
    pid = env.api.add_playlist("List", "private")
    vid = video(env)
    env.api.read_lag = 50  # YouTube's list does not show the new entry yet
    assert approve(env.service.playlists.add_to_playlist, pid, vid)["verified"] is False
    preview = env.service.playlists.add_to_playlist(pid, vid)  # the (lagging) preview cannot see it
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.playlists.add_to_playlist(pid, vid, confirmation_token=preview["confirmation_token"])
    assert exc.value.code == "playlist_duplicate" and exc.value.details["pending"] is True
    assert env.api.calls.count("playlistItems.insert") == 1


def test_exclusive_lock_is_shared_between_history_instances(env):
    import threading

    from youtube_mcp.history import History

    other = History(env.service.history.db_path)  # a second process's view of the same database
    entered = threading.Event()
    released = threading.Event()
    order = []

    def holder():
        with env.service.history.exclusive("k"):
            entered.set()
            released.wait(2)
            order.append("first")

    t = threading.Thread(target=holder)
    t.start()
    entered.wait(2)
    released.set()
    with other.exclusive("k"):
        order.append("second")
    t.join()
    assert order == ["first", "second"]


# --- M3: uncertain upload outcomes are preserved until the user decides ---------------------------

def _attempt(env, path, **kw):
    preview = env.service.upload_video(path, title="T", **kw)
    return env.service.upload_video(path, title="T", confirmation_token=preview["confirmation_token"],
                                    background=False, **kw)


def test_network_failure_after_sending_needs_explicit_decision_before_reupload(env):
    from youtube_mcp import history as h

    path = env.video()
    env.api.upload_failures = [ConnectionError("reset")] * 20
    _attempt(env, path)
    assert env.history.list_uploads()[0]["error"]["outcome"] == h.OUTCOME_UNCERTAIN
    inserts = env.api.calls.count("videos.insert.chunk")
    with pytest.raises(YouTubeMCPError) as exc:  # nothing on the channel matches, but that proves nothing
        env.service.upload_video(path, title="T")
    assert exc.value.code == "upload_outcome_uncertain"
    assert exc.value.details["attempts"][0]["status"] == h.FAILED
    assert env.api.calls.count("videos.insert.chunk") == inserts  # nothing re-sent
    env.api.upload_failures = []  # the network is back
    assert _attempt(env, path, allow_duplicate=True)["status"] == h.COMPLETED  # explicit decision


def test_failure_before_sending_is_definite_and_retry_proceeds(env):
    from youtube_mcp import history as h

    path = env.video()
    client = env.service.uploads.client
    real = client.create_upload_request
    client.create_upload_request = lambda *a, **k: (_ for _ in ()).throw(
        YouTubeMCPError("network_error", "no route", retryable=True))
    _attempt(env, path)
    assert env.history.list_uploads()[0]["error"]["outcome"] == h.OUTCOME_NOT_SENT
    client.create_upload_request = real
    calls = len(env.api.calls)
    assert _attempt(env, path)["status"] == h.COMPLETED
    assert "channels.list" not in env.api.calls[calls:]  # no reconciliation needed


def test_youtube_refusal_is_definite_and_retry_proceeds(env):
    from tests.fakes import http_error
    from youtube_mcp import history as h

    path = env.video()
    env.api.upload_failures = [http_error(403, "quotaExceeded", "quota")]
    _attempt(env, path)
    rec = env.history.list_uploads()[0]
    assert rec["error"]["code"] == "quota_exceeded" and rec["error"]["outcome"] == h.OUTCOME_REJECTED
    assert _attempt(env, path)["status"] == h.COMPLETED


def test_legacy_records_without_outcome_are_classified_conservatively():
    from youtube_mcp import history as h

    assert h.attempt_outcome({"error": {"code": "quota_exceeded"}}) == h.OUTCOME_REJECTED
    assert h.attempt_outcome({"error": {"code": "network_error"}}) == h.OUTCOME_UNCERTAIN
    assert h.attempt_outcome({"error": {"code": "upload_interrupted"}}) == h.OUTCOME_UNCERTAIN
    assert h.attempt_outcome({"error": None}) == h.OUTCOME_UNCERTAIN


def test_quick_action_reports_uncertain_outcome_with_explicit_upload_again_command(env):
    from tests.test_quick_action import make_action

    path = env.video()
    env.api.upload_failures = [ConnectionError("reset")] * 20
    _attempt(env, path)
    action, notes = make_action(env)
    result = action.process(path)
    assert result.outcome == "failed" and "upload_outcome_uncertain" in result.message
    assert "--allow-duplicate" in result.message and "YouTube Studio" in result.message
    env.api.upload_failures = []
    action.allow_duplicate = True  # the explicit Terminal decision
    assert action.process(path).outcome == "uploaded"


# --- M4: an installed package never keeps credentials or data in site-packages -----------------

def test_source_checkout_keeps_project_folder_as_home(monkeypatch, tmp_path):
    from youtube_mcp import config

    root = tmp_path / "proj"
    root.mkdir()
    (root / "pyproject.toml").write_text("")
    assert config.is_source_checkout(root)
    site = tmp_path / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    (site / "pyproject.toml").write_text("")  # even with a stray marker, site-packages is never a home
    assert not config.is_source_checkout(site)
    assert not config.is_source_checkout(tmp_path / "empty")


def test_installed_package_uses_user_owned_directory(monkeypatch, tmp_path):
    from youtube_mcp import config

    for key in ("YOUTUBE_MCP_HOME", "YOUTUBE_MCP_TOKEN", "YOUTUBE_MCP_CLIENT_SECRET", "YOUTUBE_MCP_DATA_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(config, "is_source_checkout", lambda root=None: False)
    monkeypatch.setattr(config, "user_data_dir", lambda: tmp_path / "Application Support" / "youtube-mcp")
    s = config.Settings.from_env()
    assert s.token_file == tmp_path / "Application Support" / "youtube-mcp" / "token_youtube_mcp.json"
    assert s.data_dir.parent == s.token_file.parent and "site-packages" not in str(s.token_file)
    assert config.cli_command("youtube_mcp.authorize", "authorize.py")[1:] == ["-I", "-m", "youtube_mcp.authorize"]


def test_user_data_dir_per_platform():
    from youtube_mcp.config import user_data_dir

    assert str(user_data_dir("darwin", {"HOME": "/h"})) == "/h/Library/Application Support/youtube-mcp"
    assert str(user_data_dir("linux", {"HOME": "/h"})) == "/h/.local/share/youtube-mcp"
    assert str(user_data_dir("linux", {"HOME": "/h", "XDG_DATA_HOME": "/x"})) == "/x/youtube-mcp"


def test_new_token_directory_is_owner_only(tmp_path):
    from youtube_mcp.auth import save_credentials

    class Creds:
        def to_json(self):
            return "{}"

    target = tmp_path / "fresh" / "token_youtube_mcp.json"
    save_credentials(target, Creds())
    assert (target.parent.stat().st_mode & 0o777) == 0o700 and (target.stat().st_mode & 0o777) == 0o600


def test_current_checkout_is_unchanged():
    from youtube_mcp import config

    assert config.is_source_checkout() and config.default_home() == config.PROJECT_ROOT
