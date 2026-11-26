"""Scheduled publishing, YouTube Analytics and the optional analytics authorization (Phase 7)."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from tests.fakes import FakeAnalytics, http_error
from tests.test_auth_errors import write_token
from youtube_mcp import authorize as authz
from youtube_mcp.analytics import AnalyticsClient, AnalyticsService, resolve_range
from youtube_mcp.auth import CredentialManager
from youtube_mcp.config import ANALYTICS_SCOPE, SCOPES, Settings
from youtube_mcp.errors import YouTubeMCPError

NOW = datetime(2026, 11, 26, 6, 0, tzinfo=timezone.utc)  # 11:45 in Asia/Kathmandu


@pytest.fixture
def sched(env):
    clock = {"now": NOW}
    env.service.scheduling.now = lambda: clock["now"]
    return env, clock


def private_video(env, published_before=False):
    vid = env.api.add_video({"snippet": {"title": "Launch"}, "status": {"privacyStatus": "private"}})
    env.api.videos_db[vid]["_published_before"] = published_before
    return vid


# --- scheduling ------------------------------------------------------------------------

def test_schedule_uses_configured_timezone_and_needs_public_approval(sched):
    env, _ = sched
    vid = private_video(env)
    preview = env.service.scheduling.schedule_publish(vid, "2026-12-01 18:30")
    assert preview["preview"]["publish_at"]["utc"] == "2026-12-01T12:45:00Z"
    assert "+0545" in preview["preview"]["publish_at"]["local"] and "PUBLIC" in preview["warnings"][0]
    assert "confirm_public=true" in preview["next_step"]
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.scheduling.schedule_publish(vid, "2026-12-01 18:30", confirmation_token=preview["confirmation_token"])
    assert exc.value.code == "public_requires_approval"
    assert "publishAt" not in env.api.videos_db[vid]["status"]


def test_schedule_publish_verified_by_readback(sched):
    env, _ = sched
    vid = private_video(env)
    preview = env.service.scheduling.schedule_publish(vid, "2026-12-01T13:00:00Z")
    done = env.service.scheduling.schedule_publish(vid, "2026-12-01T13:00:00Z", confirm_public=True,
                                                   confirmation_token=preview["confirmation_token"])
    assert done["status"] == "scheduled" and done["verified"] and done["visibility_now"] == "private"
    status = env.api.videos_db[vid]["status"]
    assert status["publishAt"] == "2026-12-01T13:00:00Z" and status["privacyStatus"] == "private"
    assert status["license"] == "youtube"  # other status fields preserved
    got = env.service.scheduling.get_publish_schedule(vid)
    assert got["scheduled"] and got["publish_at"]["timezone"] == "Asia/Kathmandu"


def test_explicit_offset_and_other_timezone(sched):
    env, _ = sched
    vid = private_video(env)
    p = env.service.scheduling.schedule_publish(vid, "2026-12-01 08:00", timezone_name="Europe/London")
    assert p["preview"]["publish_at"]["utc"] == "2026-12-01T08:00:00Z"
    p = env.service.scheduling.schedule_publish(vid, "2026-12-01T08:00:00-05:00")
    assert p["preview"]["publish_at"]["utc"] == "2026-12-01T13:00:00Z"
    with pytest.raises(YouTubeMCPError):
        env.service.scheduling.schedule_publish(vid, "2026-12-01 08:00", timezone_name="Mars/Base")


def test_past_or_too_soon_is_rejected(sched):
    env, _ = sched
    vid = private_video(env)
    for when in ("2026-11-25T00:00:00Z", (NOW + timedelta(minutes=5)).isoformat()):
        with pytest.raises(YouTubeMCPError) as exc:
            env.service.scheduling.schedule_publish(vid, when)
        assert exc.value.code == "invalid_schedule"


def test_time_revalidated_immediately_before_write(sched):
    env, clock = sched
    vid = private_video(env)
    when = (NOW + timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    preview = env.service.scheduling.schedule_publish(vid, when)
    clock["now"] = NOW + timedelta(minutes=15)  # approval took too long: now < 10 min ahead
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.scheduling.schedule_publish(vid, when, confirm_public=True,
                                                confirmation_token=preview["confirmation_token"])
    assert exc.value.code == "invalid_schedule"
    assert "videos.update" not in env.api.calls


@pytest.mark.parametrize("privacy", ["unlisted", "public"])
def test_unlisted_or_public_never_changed_to_private(sched, privacy):
    env, _ = sched
    vid = env.api.add_video({"snippet": {"title": "X"}, "status": {"privacyStatus": privacy}})
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.scheduling.schedule_publish(vid, "2026-12-01 18:30")
    assert exc.value.code == "unsupported_by_youtube" and exc.value.details["current_visibility"] == privacy
    assert env.api.videos_db[vid]["status"]["privacyStatus"] == privacy
    assert "videos.update" not in env.api.calls


def test_video_made_unlisted_after_preview_is_not_changed(sched):
    env, _ = sched
    vid = private_video(env)
    preview = env.service.scheduling.schedule_publish(vid, "2026-12-01 18:30")
    env.api.videos_db[vid]["status"]["privacyStatus"] = "unlisted"
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.scheduling.schedule_publish(vid, "2026-12-01 18:30", confirm_public=True,
                                                confirmation_token=preview["confirmation_token"])
    assert exc.value.code == "unsupported_by_youtube"
    assert env.api.videos_db[vid]["status"]["privacyStatus"] == "unlisted"


def test_previously_published_video_rejected_by_youtube_is_reported_as_unsupported(sched):
    env, _ = sched
    vid = private_video(env, published_before=True)
    preview = env.service.scheduling.schedule_publish(vid, "2026-12-01 18:30")
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.scheduling.schedule_publish(vid, "2026-12-01 18:30", confirm_public=True,
                                                confirmation_token=preview["confirmation_token"])
    assert exc.value.code == "unsupported_by_youtube"
    assert exc.value.details["youtube_error"]["details"]["youtube_reason"] == "invalidPublishAt"


def test_cancel_schedule_keeps_private_and_verifies(sched):
    env, _ = sched
    vid = private_video(env)
    env.api.videos_db[vid]["status"]["publishAt"] = "2026-12-01T13:00:00Z"
    preview = env.service.scheduling.cancel_publish_schedule(vid)
    assert preview["preview"]["visibility_after"].startswith("private")
    done = env.service.scheduling.cancel_publish_schedule(vid, confirmation_token=preview["confirmation_token"])
    assert done["status"] == "cancelled" and done["verified"]
    assert "publishAt" not in env.api.videos_db[vid]["status"]
    assert env.api.videos_db[vid]["status"]["privacyStatus"] == "private"
    assert env.service.scheduling.cancel_publish_schedule(vid)["status"] == "no_change"
    assert not env.service.scheduling.get_publish_schedule(vid)["scheduled"]


def test_cancel_not_yet_visible_is_a_warning_not_an_error(sched):
    env, _ = sched
    vid = private_video(env)
    env.api.videos_db[vid]["status"]["publishAt"] = "2026-12-01T13:00:00Z"
    preview = env.service.scheduling.cancel_publish_schedule(vid)
    env.api.read_lag = 50  # YouTube keeps showing the old schedule for longer than the verification window
    done = env.service.scheduling.cancel_publish_schedule(vid, confirmation_token=preview["confirmation_token"])
    assert done["ok"] and done["write_accepted"] and done["status"] == "accepted_unverified"
    assert done["verified"] is False and done["requires_manual_attention"] is True
    assert done["warnings"][0]["code"] == "verification_pending" and "Do NOT repeat" in done["warnings"][0]["action"]
    assert env.api.calls.count("videos.update") == 1


# --- analytics -------------------------------------------------------------------------

@pytest.fixture
def analytics(env):
    fake = FakeAnalytics()
    env.service.analytics = AnalyticsService(env.service, AnalyticsClient(lambda: fake),
                                             today=lambda: date(2026, 11, 26))
    return env, fake


def test_date_presets_and_custom_ranges():
    today = date(2026, 11, 26)
    assert resolve_range("last_7_days", None, None, today)["start_date"] == "2026-11-20"
    r = resolve_range(None, None, None, today)
    assert (r["preset"], r["start_date"], r["end_date"], r["days"]) == ("last_28_days", "2026-10-30", "2026-11-26", 28)
    assert resolve_range("last_90_days", None, None, today)["days"] == 90
    assert "Pacific" in r["timezone"]
    assert resolve_range(None, "2026-01-01", "2026-01-31", today)["preset"] == "custom"
    for args in (("bogus", None, None), (None, "2026-02-01", "2026-01-01"), (None, "2026-01-01", None),
                 (None, "2026-01-01", "2026-12-31"), (None, "01/01/2026", "2026-01-31")):
        with pytest.raises(YouTubeMCPError):
            resolve_range(*args, today)


def test_channel_analytics(analytics):
    env, fake = analytics
    out = env.service.analytics.get_channel_analytics("last_7_days", include_daily=True)
    t = out["totals"]
    assert t["views"] == 100 and t["watch_time_minutes"] == 101 and t["watch_time_hours"] == round(101 / 60, 2)
    assert t["net_subscribers"] == t["subscribers_gained"] - t["subscribers_lost"]
    assert t["engagement_interactions"] == t["likes"] + t["comments"] + t["shares"]
    assert out["traffic_sources"][0]["source"] == "YT_SEARCH" and len(out["daily"]) == 2
    assert any("Pacific Time" in n for n in out["notes"])
    q = fake.queries[0]
    assert q["ids"] == "channel==MINE" and q["startDate"] == "2026-11-20" and "filters" not in q
    assert not any("revenue" in m.lower() for q in fake.queries for m in q["metrics"].split(","))


def test_video_analytics_with_retention(analytics):
    env, fake = analytics
    out = env.service.analytics.get_video_analytics("https://youtu.be/abcDEF12345")
    assert out["video_id"] == "abcDEF12345" and out["totals"]["views"] == 100
    assert out["audience_retention"]["points"][0] == {"elapsed_ratio": 0.01, "audience_watch_ratio": 1.0,
                                                      "relative_retention_performance": 0.5}
    assert all(q["filters"] == "video==abcDEF12345" for q in fake.queries)


def test_unsupported_section_is_reported_not_fatal(analytics):
    env, fake = analytics
    fake.fail_for_dimension["elapsedVideoTimeRatio"] = http_error(400, "badRequest", "The query is not supported.")
    out = env.service.analytics.get_video_analytics("abcDEF12345")
    assert "audience_retention" not in out and out["totals"]
    assert out["unavailable"] == [{"section": "audience_retention", "code": "unsupported_by_youtube",
                                   "message": out["unavailable"][0]["message"]}]


def test_top_videos_with_titles(analytics):
    env, fake = analytics
    vid = env.api.add_video({"snippet": {"title": "Best"}, "status": {"privacyStatus": "unlisted"}}, "vid00000001")
    out = env.service.analytics.get_top_videos("last_90_days", sort_by="estimatedMinutesWatched", max_results=3)
    assert out["videos"][0]["rank"] == 1 and out["videos"][0]["video_id"] == vid and out["videos"][0]["title"] == "Best"
    assert fake.queries[0]["sort"] == "-estimatedMinutesWatched" and fake.queries[0]["maxResults"] == 3
    with pytest.raises(YouTubeMCPError):
        env.service.analytics.get_top_videos(sort_by="estimatedRevenue")


def test_analytics_api_disabled_is_a_setup_error(analytics):
    env, fake = analytics
    fake.fail_all = http_error(403, "accessNotConfigured", "YouTube Analytics API has not been used in project 1")
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.analytics.get_channel_analytics()
    assert exc.value.code == "api_not_enabled" and "YouTube Analytics API" in exc.value.action


def test_analytics_scope_error_suggests_youtube_readonly(analytics):
    env, fake = analytics
    fake.fail_all = http_error(403, "insufficientPermissions", "Insufficient Permission")
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.analytics.get_top_videos()
    assert exc.value.code == "insufficient_scope" and "--analytics --youtube-readonly" in exc.value.action


def test_analytics_not_configured_without_client(env):
    with pytest.raises(YouTubeMCPError) as exc:
        env.service.analytics.get_channel_analytics()
    assert exc.value.code == "analytics_not_enabled"


# --- optional analytics authorization --------------------------------------------------

def make_settings(tmp_path):
    return Settings(client_secret_file=tmp_path / "cs.json", token_file=tmp_path / "token_youtube_mcp.json",
                    data_dir=tmp_path / "data")


def test_existing_token_without_analytics_still_works(tmp_path):
    settings = make_settings(tmp_path)
    write_token(settings.token_file, expired=False)
    manager = CredentialManager(settings)
    assert manager.get_credentials().valid  # core tools unaffected
    assert manager.has_analytics_scope() is False
    with pytest.raises(YouTubeMCPError) as exc:
        manager.build_analytics_service()
    assert exc.value.code == "analytics_not_enabled" and "--analytics" in exc.value.action


def test_token_with_analytics_scope_builds_client(tmp_path):
    settings = make_settings(tmp_path)
    write_token(settings.token_file, scopes=(*SCOPES, ANALYTICS_SCOPE), expired=False)
    manager = CredentialManager(settings)
    assert manager.has_analytics_scope()
    assert hasattr(manager.build_analytics_service(), "reports")


class _FakeFlow:
    def __init__(self, granted, scopes_requested):
        self.granted, self.requested = granted, scopes_requested

    def run_local_server(self, **_):
        granted = self.granted

        class Creds:
            granted_scopes = granted
            scopes = granted
            refresh_token = "1//0gFAKErefreshTOKENvalue1234567890"

            def to_json(self):
                return json.dumps({"scopes": granted, "refresh_token": "x"})
        return Creds()


@pytest.fixture
def fake_flow(monkeypatch):
    holder = {}

    def factory(granted):
        def from_client_secrets_file(path, scopes):
            holder["requested"] = scopes
            return _FakeFlow(granted, scopes)
        import google_auth_oauthlib.flow as f
        monkeypatch.setattr(f.InstalledAppFlow, "from_client_secrets_file", staticmethod(from_client_secrets_file))
        monkeypatch.setattr(authz, "_channel_summary", lambda m: "Test Channel")
        return holder
    return factory


def test_declined_analytics_preserves_existing_token(tmp_path, fake_flow):
    settings = make_settings(tmp_path)
    settings.client_secret_file.write_text("{}")
    write_token(settings.token_file, expired=False)
    before = settings.token_file.read_bytes()
    holder = fake_flow(list(SCOPES))  # user unticked the analytics box
    assert authz.authorize(settings, open_browser=False, analytics=True) == 1
    assert ANALYTICS_SCOPE in holder["requested"]
    assert settings.token_file.read_bytes() == before


def test_granted_analytics_saved_and_kept_on_reauthorize(tmp_path, fake_flow):
    settings = make_settings(tmp_path)
    settings.client_secret_file.write_text("{}")
    fake_flow([*SCOPES, ANALYTICS_SCOPE])
    assert authz.authorize(settings, open_browser=False, analytics=True) == 0
    assert ANALYTICS_SCOPE in json.loads(settings.token_file.read_text())["scopes"]
    holder = fake_flow([*SCOPES, ANALYTICS_SCOPE])
    assert authz.authorize(settings, open_browser=False) == 0  # plain re-auth keeps the optional scope
    assert ANALYTICS_SCOPE in holder["requested"]
    assert not any("monetary" in s for s in holder["requested"])
