"""Pre-release safeguards: native public confirmation (H1), consistent approval (M5),
verified upload snapshots (L4). Ambiguous-upload recovery (M1) is in test_audit_regressions.py."""

from __future__ import annotations

import os
import subprocess
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from youtube_mcp import history as h
from youtube_mcp import native_confirm as nc
from youtube_mcp.errors import YouTubeMCPError
from youtube_mcp.staging import Staging

SRT = "1\n00:00:01,000 --> 00:00:02,000\nHi\n"


def approve(fn, *args, **kwargs):
    preview = fn(*args, **kwargs)
    assert preview["status"] == "confirmation_required", preview
    return fn(*args, confirmation_token=preview["confirmation_token"], **kwargs)


def video(env, privacy="unlisted", title="Demo"):
    return env.api.add_video({"snippet": {"title": title}, "status": {"privacyStatus": privacy}})


# --- H1: every public-exposing action asks natively ---------------------------------------

def _public_actions(env):
    """(name, callable performing the fully approved action, check that nothing changed)."""
    now = datetime.now(timezone.utc)
    env.service.scheduling.now = lambda: now
    sched_vid = video(env, "private", "Launch")
    vis_vid = video(env, "unlisted", "Clip")
    pl_private = env.api.add_playlist("Mine", "private")
    pl_public = env.api.add_playlist("Showcase", "public")
    add_vid = video(env, "unlisted", "Hidden gem")
    cap_vid = video(env, "public", "Live talk")
    srt = env.tmp / "c.srt"
    srt.write_text(SRT)
    when = (now + timedelta(days=3)).strftime("%Y-%m-%d %H:%M")
    upload_path = env.video("pub.mp4")

    def fresh(fn, *a, **k):
        p = fn(*a, **k)
        return fn(*a, confirmation_token=p["confirmation_token"], **k)

    return [
        ("upload", lambda: fresh(env.service.upload_video, upload_path, title="Pub", visibility="public",
                                 confirm_public=True, background=False),
         lambda: env.api.inserts == []),
        ("set_visibility", lambda: fresh(env.service.set_visibility, vis_vid, "public", confirm_public=True),
         lambda: env.api.videos_db[vis_vid]["status"]["privacyStatus"] == "unlisted"),
        ("schedule", lambda: fresh(env.service.scheduling.schedule_publish, sched_vid, when, confirm_public=True),
         lambda: "publishAt" not in env.api.videos_db[sched_vid]["status"]),
        ("create_playlist", lambda: fresh(env.service.playlists.create_playlist, "Open", privacy="public",
                                          confirm_public=True),
         lambda: len(env.api.playlists_db) == 2),
        ("update_playlist", lambda: fresh(env.service.playlists.update_playlist, pl_private, privacy="public",
                                          confirm_public=True),
         lambda: env.api.playlists_db[pl_private]["status"]["privacyStatus"] == "private"),
        ("add_unlisted_to_public_playlist", lambda: fresh(env.service.playlists.add_to_playlist, pl_public, add_vid),
         lambda: env.api.playlist_items[pl_public] == []),
        ("captions_on_public_video", lambda: fresh(env.service.captions.upload_captions, cap_vid, str(srt)),
         lambda: env.api.captions_db == {}),
    ]


@pytest.mark.parametrize("answer,code", [("cancel", "native_confirmation_denied"),
                                         ("timeout", "native_confirmation_timeout"),
                                         ("unavailable", "native_confirmation_unavailable")])
def test_public_actions_fail_closed_without_native_allow(env, answer, code):
    env.native_answer = answer
    for name, action, unchanged in _public_actions(env):
        with pytest.raises(YouTubeMCPError) as exc:
            action()
        assert exc.value.code == code, name
        assert unchanged(), f"{name} changed something despite {answer}"
    assert len(env.native_prompts) == 7
    assert sum(a["action"] == "native_confirmation_refused" for a in env.history.list_actions()) == 7


def test_public_actions_proceed_after_native_allow_and_show_details(env):
    for name, action, unchanged in _public_actions(env):
        action()
        assert not unchanged(), name
    for action_text, details in env.native_prompts:
        d = dict(details)
        assert "PUBLIC" in action_text and d["Channel"].startswith("Test Channel")
    by_action = {a: dict(d) for a, d in env.native_prompts}
    assert by_action["Upload a video as PUBLIC"]["Video title"] == "Pub"
    sched = by_action["Schedule a video to become PUBLIC"]
    assert sched["Video title"] == "Launch" and "+0545" in sched["Publishes at"] and sched["UTC"].endswith("Z")
    assert by_action["Make a playlist PUBLIC"]["Playlist title"] == "Mine"
    assert by_action["Add an unlisted video to a PUBLIC playlist"]["Video title"] == "Hidden gem"


def test_cancelled_dialog_burns_the_token(env):
    env.native_answer = "cancel"
    vid = video(env)
    preview = env.service.set_visibility(vid, "public")
    with pytest.raises(YouTubeMCPError):
        env.service.set_visibility(vid, "public", confirmation_token=preview["confirmation_token"], confirm_public=True)
    env.native_answer = "allow"
    with pytest.raises(YouTubeMCPError) as exc:  # cannot simply retry with the same token
        env.service.set_visibility(vid, "public", confirmation_token=preview["confirmation_token"], confirm_public=True)
    assert exc.value.code == "confirmation_invalid"


def test_non_public_actions_never_prompt(env):
    vid = video(env, "public")
    approve(env.service.set_visibility, vid, "unlisted")
    approve(env.service.update_metadata, vid, title="New")
    pid = env.api.add_playlist("P", "private")
    approve(env.service.playlists.add_to_playlist, pid, video(env))
    path = env.video()
    p = env.service.upload_video(path, title="T")
    env.service.upload_video(path, title="T", confirmation_token=p["confirmation_token"], background=False)
    srt = env.tmp / "d.srt"
    srt.write_text(SRT)
    approve(env.service.captions.upload_captions, video(env, "public"), str(srt), is_draft=True)  # draft: hidden
    assert env.native_prompts == []


def test_native_dialog_implementation_is_fail_closed(monkeypatch):
    calls = []

    def fake_run(result=None, exc=None):
        def run(args, **kwargs):
            calls.append(args)
            if exc:
                raise exc
            return result
        monkeypatch.setattr(nc, "subprocess", types.SimpleNamespace(run=run, SubprocessError=subprocess.SubprocessError))

    monkeypatch.setattr(nc.sys, "platform", "darwin")
    monkeypatch.setattr(nc.shutil, "which", lambda name: "/usr/bin/osascript")
    ok = subprocess.CompletedProcess([], 0, stdout="Allow Public\n", stderr="")
    fake_run(ok)
    nc.macos_confirm_public("Make a video PUBLIC", [("Video title", 'A "quoted" <title> & end tell')])
    args = calls[-1]
    script = " ".join(a for a in args[1:] if not a.startswith(("A", "MAKE")))
    assert 'end tell' not in script  # the title is argv data, never part of the script
    assert args[args.index("YouTubeMCP: public publishing") + 1] == "Cancel"  # default/cancel button
    for result, code in [
        (subprocess.CompletedProcess([], 0, stdout="TIMEOUT\n", stderr=""), "native_confirmation_timeout"),
        (subprocess.CompletedProcess([], 1, stdout="", stderr="execution error: User canceled. (-128)"),
         "native_confirmation_denied"),
        (subprocess.CompletedProcess([], 1, stdout="", stderr="No user interaction allowed. (-1713)"),
         "native_confirmation_unavailable"),
        (subprocess.CompletedProcess([], 0, stdout="Something else\n", stderr=""), "native_confirmation_unavailable"),
    ]:
        fake_run(result)
        with pytest.raises(YouTubeMCPError) as exc:
            nc.macos_confirm_public("X", [])
        assert exc.value.code == code
    fake_run(exc=subprocess.TimeoutExpired("osascript", 1))
    with pytest.raises(YouTubeMCPError) as exc:
        nc.macos_confirm_public("X", [])
    assert exc.value.code == "native_confirmation_unavailable"
    monkeypatch.setattr(nc.sys, "platform", "linux")
    with pytest.raises(YouTubeMCPError) as exc:
        nc.macos_confirm_public("X", [])
    assert exc.value.code == "native_confirmation_unavailable"


def test_timeout_setting_is_bounded_and_cannot_disable(monkeypatch):
    for value, expected in [("0", 10), ("-5", 10), ("99999", 300), ("abc", 60), ("45", 45)]:
        monkeypatch.setenv("YOUTUBE_MCP_CONFIRM_TIMEOUT", value)
        assert nc._timeout() == expected


def test_message_shows_action_and_details():
    text = nc.build_message("Schedule a video to become PUBLIC",
                            [("Channel", "Example Channel"), ("Video title", "Launch"), ("Publishes at", "2026-12-01 18:30 CET")])
    assert "SCHEDULE A VIDEO TO BECOME PUBLIC" in text and "Video title: Launch" in text and "18:30 CET" in text


# --- M5: one approval model for every write ------------------------------------------------

def test_update_metadata_token_is_bound_to_the_changes(env):
    vid = video(env)
    preview = env.service.update_metadata(vid, title="A")
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.update_metadata(vid, title="B", confirmation_token=preview["confirmation_token"])
    assert exc.value.code == "confirmation_mismatch"
    assert env.api.videos_db[vid]["snippet"]["title"] == "Demo"


def test_set_visibility_to_private_and_unlisted_requires_approval(env):
    vid = video(env, "public")
    for target in ("private", "unlisted"):
        preview = env.service.set_visibility(vid, target)
        assert preview["status"] == "confirmation_required"
        assert env.api.updates == [] or env.api.updates[-1]["status"]["privacyStatus"] != target
        env.service.set_visibility(vid, target, confirmation_token=preview["confirmation_token"])
        assert env.api.videos_db[vid]["status"]["privacyStatus"] == target


# --- L4: the uploaded bytes are exactly the approved bytes ----------------------------------

def test_upload_reads_a_private_snapshot_not_the_original(env):
    path = env.video("orig.mp4", data=b"A" * 4096)
    preview = env.service.upload_video(path, title="T")
    env.service.uploads.start = lambda upload_id, staged, *a, **k: setattr(env, "staged", (staged, k["cleanup"]))
    env.service.upload_video(path, title="T", confirmation_token=preview["confirmation_token"])
    staged, cleanup = env.staged
    assert staged != Path(path) and staging_root(env) in staged.parents
    assert oct(staged.parent.stat().st_mode & 0o777) == "0o700" and oct(staged.stat().st_mode & 0o777) == "0o400"
    Path(path).write_bytes(b"B" * 4096)  # changing the original afterwards ...
    assert staged.read_bytes() == b"A" * 4096  # ... does not change what is uploaded
    cleanup()
    assert not staged.exists() and list(staging_root(env).iterdir()) == []


def staging_root(env) -> Path:
    return env.service.staging.root


def test_file_changed_after_approval_is_refused_even_with_same_size_and_mtime(env):
    path = Path(env.video("orig.mp4", data=b"A" * 4096))
    preview = env.service.upload_video(str(path), title="T")
    st = path.stat()
    path.write_bytes(b"B" * 4096)  # same size ...
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))  # ... and same mtime: the hash cache is fooled
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.upload_video(str(path), title="T", confirmation_token=preview["confirmation_token"],
                                 background=False)
    assert exc.value.code == "file_changed_since_approval"
    assert env.api.inserts == [] and env.history.count_uploads() == 0
    assert list(staging_root(env).iterdir()) == []


@pytest.mark.parametrize("fail", [False, True])
def test_snapshot_removed_after_success_and_failure(env, fail):
    if fail:
        env.api.upload_failures = [ConnectionError("x")] * 20
    path = env.video()
    preview = env.service.upload_video(path, title="T")
    result = env.service.upload_video(path, title="T", confirmation_token=preview["confirmation_token"],
                                      background=False)
    assert result["status"] == (h.FAILED if fail else h.COMPLETED)
    assert list(staging_root(env).iterdir()) == []
    assert Path(path).exists()  # the original is never touched


def test_duplicate_refusal_at_confirm_removes_snapshot(env):
    path = env.video()
    p1 = env.service.upload_video(path, title="T")
    p2 = env.service.upload_video(path, title="T")
    env.service.upload_video(path, title="T", confirmation_token=p1["confirmation_token"], background=False)
    with pytest.raises(YouTubeMCPError):
        env.service.upload_video(path, title="T", confirmation_token=p2["confirmation_token"], background=False)
    assert list(staging_root(env).iterdir()) == []


def test_copy_fallback_and_space_check(tmp_path, monkeypatch):
    src = tmp_path / "v.mp4"
    src.write_bytes(b"data" * 1000)
    import hashlib
    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    staging = Staging(tmp_path / "staging", clone=lambda s, d: False)
    staged = staging.stage(src, sha)
    assert staged.read_bytes() == src.read_bytes()
    staging.discard(staged)
    import youtube_mcp.staging as st
    monkeypatch.setattr(st.shutil, "disk_usage", lambda p: types.SimpleNamespace(free=10))
    with pytest.raises(YouTubeMCPError) as exc:
        staging.stage(src, sha)
    assert exc.value.code == "insufficient_disk_space"
    assert list((tmp_path / "staging").iterdir()) == []


def test_real_apfs_clone_is_used_when_available(tmp_path):
    src = tmp_path / "v.mp4"
    src.write_bytes(os.urandom(10000))
    import hashlib
    staging = Staging(tmp_path / "staging")
    staged = staging.stage(src, hashlib.sha256(src.read_bytes()).hexdigest())
    assert staged.read_bytes() == src.read_bytes()


def test_stale_snapshots_cleaned_and_discard_is_confined(tmp_path):
    staging = Staging(tmp_path / "staging")
    dead = tmp_path / "staging" / "up-999999-abc"
    mine = tmp_path / "staging" / f"up-{os.getpid()}-abc"
    for d in (dead, mine):
        d.mkdir()
        (d / "f.mp4").write_bytes(b"x")
    assert staging.cleanup_stale() == 1 and not dead.exists() and mine.exists()
    outside = tmp_path / "keep" / "f.mp4"
    outside.parent.mkdir()
    outside.write_bytes(b"x")
    staging.discard(outside)
    assert outside.exists()
