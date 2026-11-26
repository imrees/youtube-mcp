"""Regression tests for defects found in the Phase 8 security / accuracy / reliability audit."""

from __future__ import annotations

import io
import logging
import subprocess
import sys
import threading
from datetime import date, datetime, timezone

import pytest

from tests.fakes import FakeAnalytics
from youtube_mcp import history as h
from youtube_mcp.analytics import AnalyticsClient, AnalyticsService
from youtube_mcp.errors import RedactingFilter, RedactingFormatter, YouTubeMCPError
from youtube_mcp.history import History
from youtube_mcp.scheduling import parse_publish_time
from zoneinfo import ZoneInfo


# --- Reliability: cross-process duplicate race (MCP server vs Finder Quick Action) -------

def test_duplicate_check_and_insert_are_atomic_across_processes(tmp_path):
    db = tmp_path / "history.sqlite3"
    History(db)
    barrier = threading.Barrier(8)
    created, refused = [], []

    def no_active(records):
        if any(r["status"] in h.ACTIVE_STATUSES or r["status"] == h.COMPLETED for r in records):
            raise YouTubeMCPError("upload_in_progress", "already")

    def worker():
        hist = History(db)  # separate object = separate in-process lock, like a separate process
        barrier.wait()
        try:
            created.append(hist.create_upload_checked(
                no_active, file_path="/x.mp4", file_name="x.mp4", file_size=1, sha256="same", title="t",
                category_id="28", requested_visibility="unlisted"))
        except YouTubeMCPError:
            refused.append(1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(created) == 1 and len(refused) == 7
    assert History(db).count_uploads() == 1


def test_second_service_instance_cannot_start_same_file(env):
    """Two services sharing one DB (MCP + Quick Action): the second confirmation is refused."""
    from youtube_mcp.confirmations import ConfirmationStore
    from youtube_mcp.service import YouTubeMCPService
    from youtube_mcp.uploads import UploadManager

    other_history = History(env.history.db_path)
    other = YouTubeMCPService(env.service.settings, env.service.client, other_history,
                              UploadManager(env.service.client, other_history, sleep=lambda s: None),
                              ConfirmationStore(600), clipboard=lambda t: True)
    path = env.video()
    p1 = env.service.upload_video(path, title="A")
    p2 = other.upload_video(path, title="A")
    # First process starts but its worker is not run yet (status queued = active).
    env.service.uploads.start = lambda *a, **k: None
    env.service.upload_video(path, title="A", confirmation_token=p1["confirmation_token"])
    with pytest.raises(YouTubeMCPError) as exc:
        other.upload_video(path, title="A", confirmation_token=p2["confirmation_token"])
    assert exc.value.code == "upload_in_progress"
    assert env.history.count_uploads() == 1


# --- Reliability: ambiguous outcome (YouTube may have accepted the upload) ----------------

def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _failed_attempt(env, path):
    from pathlib import Path
    p = Path(path)
    return env.history.create_upload(file_path=str(p), file_name=p.name, file_size=p.stat().st_size,
                                     sha256=env.service.file_sha256(p), title="T", category_id="28",
                                     requested_visibility="unlisted", status=h.FAILED)


def test_failed_attempt_that_reached_youtube_is_recovered_not_reuploaded(env):
    path = env.video("clip.mp4", data=b"x" * 5000)
    upload_id = _failed_attempt(env, path)
    vid = env.api.add_video({"snippet": {"title": "T"}, "status": {"privacyStatus": "unlisted"}})
    env.api.videos_db[vid]["fileDetails"] = {"fileSize": "5000"}
    env.api.videos_db[vid]["snippet"]["publishedAt"] = _now_iso()

    with pytest.raises(YouTubeMCPError) as exc:
        env.service.upload_video(path, title="T")
    assert exc.value.code == "duplicate_upload" and exc.value.details["video_id"] == vid
    rec = env.history.get_upload(upload_id)
    assert rec["status"] == h.COMPLETED and rec["video_id"] == vid
    assert rec["warnings"][-1]["code"] == "recovered_after_failure"
    assert env.api.inserts == []


@pytest.mark.parametrize("videos", [
    [("5000", "T")],                 # size+title match but the description differs below -> not confirmed
    [("5000", "Other")],             # size matches, title differs
    [("4999", "T")],                 # title matches, size differs
    [("5000", "T"), ("5000", "T")],  # two equally good candidates
])
def test_unconfirmed_identity_returns_ambiguous_upload(env, videos):
    path = env.video("clip.mp4", data=b"x" * 5000)
    upload_id = _failed_attempt(env, path)
    for n, (size, title) in enumerate(videos):
        desc = "different" if len(videos) == 1 and title == "T" else ""
        vid = env.api.add_video({"snippet": {"title": title, "description": desc},
                                 "status": {"privacyStatus": "unlisted"}})
        env.api.videos_db[vid]["fileDetails"] = {"fileSize": size}
        env.api.videos_db[vid]["snippet"]["publishedAt"] = _now_iso()
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.upload_video(path, title="T")
    assert exc.value.code == "ambiguous_upload" and exc.value.details["candidates"]
    assert env.history.get_upload(upload_id)["status"] == h.FAILED  # never silently linked
    assert env.api.inserts == []  # and never silently re-uploaded
    # The user's explicit decision after manual review allows a new upload.
    assert env.service.upload_video(path, title="T", allow_duplicate=True)["status"] == "confirmation_required"


def _uncertain_then_explicit_decision(env, path):
    """No evidence is not proof of failure (review M3): an uncertain attempt needs the user's explicit decision."""
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.upload_video(path, title="T")
    assert exc.value.code == "upload_outcome_uncertain" and "allow_duplicate=true" in exc.value.action
    assert env.service.upload_video(path, title="T", allow_duplicate=True)["status"] == "confirmation_required"


def test_no_evidence_keeps_the_outcome_uncertain(env):
    path = env.video("clip.mp4", data=b"x" * 5000)
    _failed_attempt(env, path)
    vid = env.api.add_video({"snippet": {"title": "Unrelated"}, "status": {"privacyStatus": "unlisted"}})
    env.api.videos_db[vid]["fileDetails"] = {"fileSize": "123"}
    env.api.videos_db[vid]["snippet"]["publishedAt"] = _now_iso()
    _uncertain_then_explicit_decision(env, path)


def test_video_already_linked_to_another_record_is_never_adopted(env):
    path = env.video("clip.mp4", data=b"x" * 5000)
    _failed_attempt(env, path)
    vid = env.api.add_video({"snippet": {"title": "T"}, "status": {"privacyStatus": "unlisted"}})
    env.api.videos_db[vid]["fileDetails"] = {"fileSize": "5000"}
    env.api.videos_db[vid]["snippet"]["publishedAt"] = _now_iso()
    env.history.create_upload(file_path="/other.mp4", file_name="other.mp4", file_size=5000, sha256="other",
                              title="T", category_id="28", requested_visibility="unlisted",
                              status=h.COMPLETED, video_id=vid)
    _uncertain_then_explicit_decision(env, path)
    assert env.history.find_by_sha256(env.service.file_sha256(__import__("pathlib").Path(path)))[0]["video_id"] is None


def test_duration_mismatch_blocks_recovery(env, monkeypatch):
    import youtube_mcp.service as svc_mod
    monkeypatch.setattr(svc_mod, "local_duration_ms", lambda p: 60_000)
    path = env.video("clip.mp4", data=b"x" * 5000)
    _failed_attempt(env, path)
    vid = env.api.add_video({"snippet": {"title": "T"}, "status": {"privacyStatus": "unlisted"}})
    env.api.videos_db[vid]["fileDetails"] = {"fileSize": "5000", "durationMs": "10000"}
    env.api.videos_db[vid]["snippet"]["publishedAt"] = _now_iso()
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.upload_video(path, title="T")
    assert exc.value.code == "ambiguous_upload"


def test_video_older_than_attempt_is_not_adopted(env):
    path = env.video("clip.mp4", data=b"x" * 5000)
    _failed_attempt(env, path)
    vid = env.api.add_video({"snippet": {"title": "T"}, "status": {"privacyStatus": "unlisted"}})
    env.api.videos_db[vid]["fileDetails"] = {"fileSize": "5000"}
    env.api.videos_db[vid]["snippet"]["publishedAt"] = "2020-01-01T00:00:00Z"
    _uncertain_then_explicit_decision(env, path)


def test_failed_upload_after_transmission_is_recorded_as_uncertain(env):
    # Superseded "Retrying is safe" (review finding M3): a network failure mid-upload is an uncertain outcome.
    env.api.upload_failures = [ConnectionError("reset")] * 20
    path = env.video()
    preview = env.service.upload_video(path, title="T")
    env.service.upload_video(path, title="T", confirmation_token=preview["confirmation_token"], background=False)
    rec = env.history.list_uploads()[0]
    assert rec["status"] == h.FAILED and rec["error"]["outcome"] == h.OUTCOME_UNCERTAIN
    assert "may have received" in rec["error"]["action"] and "allow_duplicate" in rec["error"]["action"]


# --- Reliability: stale records and PID reuse -------------------------------------------

def test_stale_record_with_reused_pid_is_recovered(env):
    proc = subprocess.Popen(["sleep", "30"])  # a live process that is not ours
    try:
        uid = env.history.create_upload(file_path="/x.mp4", file_name="x.mp4", file_size=1, sha256="s",
                                        title="t", category_id="28", requested_visibility="unlisted",
                                        status=h.UPLOADING, pid=proc.pid)
        assert env.history.recover_interrupted() == 1
        assert env.history.get_upload(uid)["status"] == h.INTERRUPTED
    finally:
        proc.kill()


def test_live_owner_process_is_not_marked_interrupted(env):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)  # youtube_mcp worker"])
    try:
        uid = env.history.create_upload(file_path="/x.mp4", file_name="x.mp4", file_size=1, sha256="s",
                                        title="t", category_id="28", requested_visibility="unlisted",
                                        status=h.UPLOADING, pid=proc.pid)
        assert env.history.recover_interrupted() == 0
        assert env.history.get_upload(uid)["status"] == h.UPLOADING
    finally:
        proc.kill()


def _crashed_record(env, path, status):
    return env.history.create_upload(file_path=path, file_name="demo.mp4", file_size=4096,
                                     sha256=env.service.file_sha256(__import__("pathlib").Path(path)),
                                     title="T", category_id="28", requested_visibility="unlisted",
                                     status=status, pid=999_999)  # dead process


def test_crashed_upload_that_never_started_does_not_block_mcp_upload(env):
    path = env.video()
    preview = env.service.upload_video(path, title="T")
    rec_id = _crashed_record(env, path, h.QUEUED)
    result = env.service.upload_video(path, title="T", confirmation_token=preview["confirmation_token"],
                                      background=False)
    assert result["status"] == h.COMPLETED
    rec = env.history.get_upload(rec_id)
    assert rec["status"] == h.INTERRUPTED and rec["error"]["outcome"] == h.OUTCOME_NOT_SENT


def test_crashed_upload_mid_transfer_is_uncertain_not_in_progress(env):
    path = env.video()
    rec_id = _crashed_record(env, path, h.UPLOADING)
    with pytest.raises(YouTubeMCPError) as exc:  # not "upload_in_progress": the stale record is recovered
        env.service.upload_video(path, title="T")
    assert exc.value.code == "upload_outcome_uncertain"
    assert env.history.get_upload(rec_id)["error"]["outcome"] == h.OUTCOME_UNCERTAIN


# --- Security: redaction of tracebacks ---------------------------------------------------

def test_tracebacks_are_redacted():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter("%(message)s"))
    handler.addFilter(RedactingFilter())
    logger = logging.getLogger("redaction-test")
    logger.addHandler(handler)
    try:
        raise RuntimeError("refresh failed for ya29.SECRETaccessTOKEN1234 and 1//0gSECRETrefreshTOKEN12345")
    except RuntimeError:
        logger.exception("boom")
    finally:
        logger.removeHandler(handler)
    out = stream.getvalue()
    assert "Traceback" in out and "SECRET" not in out and "[REDACTED]" in out


# --- Accuracy: scheduling input ----------------------------------------------------------

def test_date_without_time_is_rejected():
    with pytest.raises(YouTubeMCPError):
        parse_publish_time("2026-12-01", ZoneInfo("Asia/Kathmandu"))


def test_nonexistent_dst_time_is_rejected():
    with pytest.raises(YouTubeMCPError) as exc:
        parse_publish_time("2027-03-14 02:30", ZoneInfo("America/New_York"))
    assert "does not exist" in exc.value.message
    assert parse_publish_time("2027-03-14 03:30", ZoneInfo("America/New_York")).hour == 7


# --- Accuracy: analytics -----------------------------------------------------------------

@pytest.fixture
def analytics(env):
    fake = FakeAnalytics()
    env.service.analytics = AnalyticsService(env.service, AnalyticsClient(lambda: fake),
                                             today=lambda: date(2026, 11, 26))
    return env, fake


def test_analytics_values_are_raw_and_definitions_explicit(analytics):
    env, fake = analytics
    out = env.service.analytics.get_video_analytics("abcDEF12345", include_traffic_sources=False,
                                                    include_retention=False)
    totals_query = fake.queries[0]
    metrics = totals_query["metrics"].split(",")
    # Every official metric is returned unchanged (fake value = 100 + column index).
    for n, metric in enumerate(metrics):
        from youtube_mcp.analytics import FRIENDLY
        assert out["totals"][FRIENDLY.get(metric, metric)] == 100 + n
    assert "engaged_views" in out["totals"]
    assert set(out["derived_fields"]) == {"watch_time_hours", "net_subscribers", "engagement_interactions",
                                          "engagement_rate_percent"}
    assert out["metric_definitions"]["watch_time_minutes"]["youtube_metric"] == "estimatedMinutesWatched"
    assert any("will generally NOT equal" in n for n in out["notes"])


def test_analytics_reports_data_coverage_when_youtube_lags(analytics):
    env, fake = analytics
    out = env.service.analytics.get_channel_analytics("last_7_days")
    assert out["data_coverage"] == {"requested_end_date": "2026-11-26", "latest_day_reported": "2026-11-21",
                                    "days_reported": 2}
    assert any("only through 2026-11-21" in n for n in out["notes"])
    assert "daily" not in out  # coverage probe does not add the daily breakdown unless asked


# --- Accuracy: channel video counts ------------------------------------------------------

def test_channel_info_distinguishes_public_count_from_total_uploads(env):
    for privacy in ("public", "unlisted", "private"):
        env.api.add_video({"snippet": {"title": privacy}, "status": {"privacyStatus": privacy}})
    env.api.channel["statistics"]["videoCount"] = "1"  # YouTube's statistic counts public videos only
    info = env.service.get_channel_info()
    assert info["public_video_count"] == 1 and info["video_count"] == 1 and info["total_uploads"] == 3
    assert "public" in info["video_count_note"]
