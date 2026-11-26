"""YouTube Analytics (API v2, reports.query): channel, per-video and top-video reports.

Optional feature: needs the YouTube Analytics API enabled in the Cloud project and the opt-in
read-only scope yt-analytics.readonly (or youtube.readonly), granted with
`authorize.py --analytics`. Without it, these tools return `analytics_not_enabled`; nothing
else in the server is affected. No monetary/revenue metrics are requested.

Reporting dates are Pacific Time (America/Los_Angeles). Data typically lags 48-72 hours, so
the most recent days in a range may be missing or incomplete.

Report shapes used (channel/video report tables in the API docs):
- totals: no dimension (optionally filters=video==ID)
- daily: dimensions=day, sort=day
- traffic sources: dimensions=insightTrafficSourceType
- audience retention: dimensions=elapsedVideoTimeRatio, single video filter only
- top videos: dimensions=video, sort=-<metric>, maxResults <= 200
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable
from zoneinfo import ZoneInfo

from .errors import YouTubeMCPError, authorize_command, translate_exception
from .youtube import invalid, parse_video_id

if TYPE_CHECKING:
    from .service import YouTubeMCPService

PACIFIC = ZoneInfo("America/Los_Angeles")
PRESETS = {"last_7_days": 7, "last_28_days": 28, "last_90_days": 90}
DEFAULT_PRESET = "last_28_days"
CORE_METRICS = ("views", "estimatedMinutesWatched", "averageViewDuration", "averageViewPercentage",
                "subscribersGained", "subscribersLost", "likes", "dislikes", "comments", "shares", "engagedViews")
TOP_VIDEO_METRICS = ("views", "estimatedMinutesWatched", "averageViewDuration", "averageViewPercentage",
                     "likes", "comments", "shares", "subscribersGained", "engagedViews")
TOP_SORTS = ("views", "engagedViews", "estimatedMinutesWatched", "subscribersGained", "likes", "comments", "shares")
FRIENDLY = {
    "views": "views", "estimatedMinutesWatched": "watch_time_minutes", "averageViewDuration": "average_view_duration_seconds",
    "averageViewPercentage": "average_view_percentage", "subscribersGained": "subscribers_gained",
    "subscribersLost": "subscribers_lost", "likes": "likes", "dislikes": "dislikes", "comments": "comments",
    "shares": "shares", "engagedViews": "engaged_views", "day": "date", "insightTrafficSourceType": "source", "video": "video_id",
    "elapsedVideoTimeRatio": "elapsed_ratio", "audienceWatchRatio": "audience_watch_ratio",
    "relativeRetentionPerformance": "relative_retention_performance",
}
# Official definitions (developers.google.com/youtube/analytics/metrics and revision_history).
# Values are passed through exactly as YouTube returns them; only fields in DERIVED are calculated here.
METRIC_DEFINITIONS = {
    "views": {"youtube_metric": "views", "unit": "count",
              "definition": "Plays counted from the first frame; for Shorts, starts and replays are counted "
                            "(YouTube change effective 2025-04-30 for this API). Includes TV co-views."},
    "engaged_views": {"youtube_metric": "engagedViews", "unit": "count",
                      "definition": "Views under YouTube's stricter, previous counting method; excludes co-views."},
    "watch_time_minutes": {"youtube_metric": "estimatedMinutesWatched", "unit": "minutes",
                           "definition": "YouTube's ESTIMATE of minutes watched; returned as whole minutes, so small "
                                         "values are coarse (e.g. 24 seconds of viewing can report 0)."},
    "average_view_duration_seconds": {"youtube_metric": "averageViewDuration", "unit": "seconds",
                                      "definition": "Average length of a playback; excludes looping-clip traffic "
                                                    "since 2021-12-13."},
    "average_view_percentage": {"youtube_metric": "averageViewPercentage", "unit": "percent (0-100)",
                                "definition": "Average share of the video watched per playback; excludes looping clips."},
    "subscribers_gained": {"youtube_metric": "subscribersGained", "unit": "count",
                           "definition": "When filtered to a video, only subscriptions from that video's watch page."},
}
CONSISTENCY_NOTE = ("views, watch_time_minutes and average_view_duration_seconds are independent YouTube metrics with "
                    "different definitions (views include Shorts replays; average duration excludes looping clips; "
                    "watch time is a whole-minute estimate). views x average duration will generally NOT equal "
                    "watch time, and this server does not reconcile them.")
DERIVED = {"watch_time_hours": "watch_time_minutes / 60 (inherits the whole-minute rounding)",
           "net_subscribers": "subscribers_gained - subscribers_lost",
           "engagement_interactions": "likes + comments + shares",
           "engagement_rate_percent": "engagement_interactions / views x 100 (not a YouTube metric)"}
TIME_NOTE = ("Report dates are Pacific Time (America/Los_Angeles, UTC-8 / UTC-7 in daylight time). "
             "YouTube Analytics data typically lags 48-72 hours, so the latest days may be missing or incomplete.")
# Errors that make the whole request pointless (vs. one optional section being unavailable).
FATAL = {"analytics_not_enabled", "api_not_enabled", "insufficient_scope", "auth_expired", "not_authorized",
         "token_invalid", "network_error", "quota_exceeded", "rate_limited", "youtube_unavailable"}


class AnalyticsClient:
    """Thin wrapper over youtubeAnalytics v2 reports.query with error translation."""

    def __init__(self, service_factory: Callable[[], Any], num_retries: int = 3) -> None:
        self._factory = service_factory
        self.num_retries = num_retries

    def query(self, **params: Any) -> dict[str, Any]:
        try:
            request = self._factory().reports().query(ids="channel==MINE", **params)
            return request.execute(num_retries=self.num_retries)
        except Exception as exc:
            raise _analytics_error(translate_exception(exc)) from None


def _analytics_error(err: YouTubeMCPError) -> YouTubeMCPError:
    if err.code == "api_not_enabled":
        return YouTubeMCPError(
            "api_not_enabled",
            "The YouTube Analytics API is not enabled in this Google Cloud project (setup step, not a bug).",
            action="Google Cloud Console > APIs & Services > Library > 'YouTube Analytics API' > Enable; "
                   "wait a few minutes and retry.", details=err.details)
    if err.code == "insufficient_scope":
        return YouTubeMCPError(
            "insufficient_scope",
            "Google rejected the analytics request for missing permission. Google's docs list youtube.readonly "
            "as also required for reports.query.",
            action=f"Ask the user to run in Terminal: {authorize_command()} --analytics --youtube-readonly",
            details=err.details)
    if err.code in ("invalid_request", "youtube_api_error") and "not supported" in err.message.lower():
        return YouTubeMCPError("unsupported_by_youtube", f"YouTube Analytics does not support this report: {err.message}",
                               details=err.details)
    return err


def resolve_range(preset: str | None, start_date: str | None, end_date: str | None,
                  today: date | None = None) -> dict[str, Any]:
    today = today or datetime.now(PACIFIC).date()
    if start_date or end_date:
        if not (start_date and end_date):
            raise invalid("Pass both start_date and end_date (YYYY-MM-DD), or a preset.")
        try:
            start, end = date.fromisoformat(start_date.strip()), date.fromisoformat(end_date.strip())
        except ValueError:
            raise invalid("Dates must be in YYYY-MM-DD format.") from None
        if start > end:
            raise invalid("start_date must be on or before end_date.")
        if end > today:
            raise invalid(f"end_date {end} is in the future (today is {today} Pacific Time).")
        label = "custom"
    else:
        label = (preset or DEFAULT_PRESET).strip().lower()
        if label not in PRESETS:
            raise invalid(f"Unknown preset '{preset}'.", f"Use one of: {', '.join(PRESETS)}, or start_date/end_date.")
        end = today
        start = end - timedelta(days=PRESETS[label] - 1)
    return {"preset": label, "start_date": start.isoformat(), "end_date": end.isoformat(),
            "timezone": "America/Los_Angeles (Pacific Time)", "days": (end - start).days + 1}


def _rows(resp: dict[str, Any]) -> list[dict[str, Any]]:
    names = [FRIENDLY.get(h["name"], h["name"]) for h in resp.get("columnHeaders") or []]
    return [dict(zip(names, row)) for row in resp.get("rows") or []]


def _with_derived(totals: dict[str, Any]) -> dict[str, Any]:
    out = dict(totals)
    if "watch_time_minutes" in out and out["watch_time_minutes"] is not None:
        out["watch_time_hours"] = round(float(out["watch_time_minutes"]) / 60, 2)
    if "subscribers_gained" in out and "subscribers_lost" in out:
        out["net_subscribers"] = (out["subscribers_gained"] or 0) - (out["subscribers_lost"] or 0)
    interactions = sum(out.get(k) or 0 for k in ("likes", "comments", "shares"))
    if any(k in out for k in ("likes", "comments", "shares")):
        out["engagement_interactions"] = interactions
        views = out.get("views") or 0
        out["engagement_rate_percent"] = round(100 * interactions / views, 2) if views else None
    return out


class AnalyticsService:
    def __init__(self, svc: "YouTubeMCPService", client: AnalyticsClient | None,
                 today: Callable[[], date] | None = None) -> None:
        self.svc = svc
        self.client = client
        self.today = today

    def _require(self) -> AnalyticsClient:
        if self.client is None:
            from .errors import analytics_not_enabled
            raise analytics_not_enabled()
        return self.client

    def _section(self, unavailable: list[dict[str, Any]], name: str, **params: Any) -> list[dict[str, Any]] | None:
        try:
            return _rows(self._require().query(**params))
        except YouTubeMCPError as err:
            if err.code in FATAL:
                raise
            unavailable.append({"section": name, "code": err.code, "message": err.message})
            return None

    def _range(self, preset: str | None, start_date: str | None, end_date: str | None) -> dict[str, Any]:
        return resolve_range(preset, start_date, end_date, self.today() if self.today else None)

    def _report(self, rng: dict[str, Any], filters: str | None, include_daily: bool,
                include_traffic_sources: bool) -> dict[str, Any]:
        base = {"startDate": rng["start_date"], "endDate": rng["end_date"]}
        if filters:
            base["filters"] = filters
        client = self._require()
        totals_rows = _rows(client.query(metrics=",".join(CORE_METRICS), **base))  # fatal on any error
        unavailable: list[dict[str, Any]] = []
        result: dict[str, Any] = {"ok": True, "date_range": rng,
                                  "totals": _with_derived(totals_rows[0]) if totals_rows else {},
                                  "derived_fields": DERIVED,
                                  "metric_definitions": METRIC_DEFINITIONS,
                                  "notes": [TIME_NOTE, CONSISTENCY_NOTE]}
        if not totals_rows:
            result["notes"].append("YouTube returned no data for this range (no views, or data not processed yet).")
        if include_traffic_sources:
            rows = self._section(unavailable, "traffic_sources", dimensions="insightTrafficSourceType",
                                 metrics="views,estimatedMinutesWatched", sort="-views", **base)
            if rows is not None:
                result["traffic_sources"] = rows
        daily = self._section(unavailable, "daily", dimensions="day", sort="day",
                              metrics=",".join(CORE_METRICS) if include_daily else "views", **base)
        if daily is not None:
            if include_daily:
                result["daily"] = daily
            latest = daily[-1]["date"] if daily else None
            result["data_coverage"] = {"requested_end_date": rng["end_date"], "latest_day_reported": latest,
                                       "days_reported": len(daily)}
            if latest is None or latest < rng["end_date"]:
                result["notes"].append(
                    f"YouTube has reported data only through {latest or 'no day yet'} in this range (processing "
                    "lag); totals cover the reported days only and were not extrapolated.")
        result["unavailable"] = unavailable
        return result

    # tools ------------------------------------------------------------------------

    def get_channel_analytics(self, preset: str | None = DEFAULT_PRESET, start_date: str | None = None,
                              end_date: str | None = None, include_daily: bool = False,
                              include_traffic_sources: bool = True) -> dict[str, Any]:
        rng = self._range(preset, start_date, end_date)
        return {**self._report(rng, None, include_daily, include_traffic_sources), "scope": "channel"}

    def get_video_analytics(self, video_id: str, preset: str | None = DEFAULT_PRESET, start_date: str | None = None,
                            end_date: str | None = None, include_daily: bool = False,
                            include_traffic_sources: bool = True, include_retention: bool = True) -> dict[str, Any]:
        video_id = parse_video_id(video_id)
        rng = self._range(preset, start_date, end_date)
        result = self._report(rng, f"video=={video_id}", include_daily, include_traffic_sources)
        result.update({"scope": "video", "video_id": video_id})
        if include_retention:
            rows = self._section(result["unavailable"], "audience_retention", dimensions="elapsedVideoTimeRatio",
                                 metrics="audienceWatchRatio,relativeRetentionPerformance",
                                 filters=f"video=={video_id}", startDate=rng["start_date"], endDate=rng["end_date"])
            if rows is not None:
                result["audience_retention"] = {
                    "description": ("elapsed_ratio is the position in the video (0-1); audience_watch_ratio is the "
                                    "share of viewers still watching (can exceed 1 with rewatches); "
                                    "relative_retention_performance compares with similar-length videos (0-1)."),
                    "points": rows}
        return result

    def get_top_videos(self, preset: str | None = DEFAULT_PRESET, start_date: str | None = None,
                       end_date: str | None = None, sort_by: str = "views", max_results: int = 10) -> dict[str, Any]:
        if sort_by not in TOP_SORTS:
            raise invalid(f"sort_by must be one of: {', '.join(TOP_SORTS)}.")
        max_results = max(1, min(int(max_results), 200))
        rng = self._range(preset, start_date, end_date)
        rows = _rows(self._require().query(startDate=rng["start_date"], endDate=rng["end_date"], dimensions="video",
                                           metrics=",".join(TOP_VIDEO_METRICS), sort=f"-{sort_by}",
                                           maxResults=max_results))
        ids = [r["video_id"] for r in rows if r.get("video_id")]
        titles: dict[str, str] = {}
        try:
            for i in range(0, len(ids), 50):
                found = self.svc.client.get_videos(ids[i:i + 50], parts="snippet")
                titles.update({vid: v.get("snippet", {}).get("title") for vid, v in found.items()})
        except YouTubeMCPError:
            pass  # titles are a convenience; the analytics data is still valid
        videos = [{"rank": n, **r, "title": titles.get(r.get("video_id")),
                   "url": f"https://youtu.be/{r['video_id']}" if r.get("video_id") else None}
                  for n, r in enumerate(rows, 1)]
        return {"ok": True, "date_range": rng, "sort_by": sort_by, "count": len(videos), "videos": videos,
                "metric_definitions": METRIC_DEFINITIONS,
                "notes": [TIME_NOTE, CONSISTENCY_NOTE, "Deleted videos are omitted from per-video rankings."]}
