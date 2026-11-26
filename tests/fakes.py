"""In-memory fake of the googleapiclient YouTube resource, faithful to the parts we use.

Notably, videos().update replaces the whole part (snippet/status) like the real API, so
tests catch accidental erasure of fields that were not re-sent.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import httplib2
from googleapiclient.errors import HttpError


def http_error(status: int, reason: str = "", message: str = "") -> HttpError:
    content = json.dumps({"error": {"code": status, "message": message,
                                    "errors": [{"reason": reason, "message": message}] if reason else []}})
    return HttpError(httplib2.Response({"status": status}), content.encode())


class FakeRequest:
    def __init__(self, api: "FakeYouTube", name: str, fn: Any) -> None:
        self.api, self.name, self.fn = api, name, fn

    def execute(self, num_retries: int = 0) -> Any:
        self.api.calls.append(self.name)
        self.api.retry_args.append((self.name, num_retries))
        if self.name in self.api.fail_next:
            raise self.api.fail_next.pop(self.name)
        return self.fn()


class FakeStatus:
    def __init__(self, p: float) -> None:
        self._p = p

    def progress(self) -> float:
        return self._p


class FakeUploadRequest:
    def __init__(self, api: "FakeYouTube", body: dict[str, Any], media: Any, notify: bool) -> None:
        self.api, self.body, self.media, self.notify = api, body, media, notify
        self.sent = 0
        self._in_error_state = False

    def next_chunk(self, num_retries: int = 0) -> Any:
        self.api.calls.append("videos.insert.chunk")
        if self.api.upload_failures:
            raise self.api.upload_failures.pop(0)
        self.sent += 1
        if self.sent < self.api.upload_chunks:
            return FakeStatus(self.sent / self.api.upload_chunks), None
        vid = self.api.add_video(self.body)
        return None, {"id": vid, "snippet": self.body["snippet"], "status": self.api.videos_db[vid]["status"]}


class _Videos:
    def __init__(self, api: "FakeYouTube") -> None:
        self.api = api

    def list(self, part: str, id: str, maxResults: int | None = None) -> FakeRequest:
        def run() -> dict[str, Any]:
            if maxResults is not None:  # the real API rejects maxResults together with id
                raise http_error(400, "incompatibleParameters", "maxResults is not supported with id")
            items = []
            for vid in id.split(","):
                v = self.api._view("video", vid, self.api.videos_db.get(vid))
                if v is not None:
                    items.append({"id": vid, **{p: copy.deepcopy(v[p]) for p in part.split(",") if p in v}})
            return {"items": items}
        return FakeRequest(self.api, "videos.list", run)

    def update(self, part: str, body: dict[str, Any]) -> FakeRequest:
        def run() -> dict[str, Any]:
            vid = body["id"]
            if vid not in self.api.videos_db:
                raise http_error(404, "videoNotFound", "Video not found")
            self.api.updates.append(copy.deepcopy(body))
            self.api._snapshot("video", vid, self.api.videos_db[vid])
            new = copy.deepcopy(body[part])
            if part == "status" and new.get("publishAt"):
                # Real API rules: publishAt only for private videos that were never published.
                if new.get("privacyStatus") != "private" or self.api.videos_db[vid].get("_published_before"):
                    raise http_error(400, "invalidPublishAt", "The request metadata specifies an invalid scheduled publishing time.")
            if part == "status" and self.api.lock_private:
                new["privacyStatus"] = "private"
            self.api.videos_db[vid][part] = new  # full replacement, like the real API
            return {"id": vid, part: new}
        return FakeRequest(self.api, "videos.update", run)

    def insert(self, part: str, body: dict[str, Any], media_body: Any, notifySubscribers: bool = True) -> FakeUploadRequest:
        self.api.inserts.append({"body": copy.deepcopy(body), "notify": notifySubscribers, "media": media_body})
        return FakeUploadRequest(self.api, body, media_body, notifySubscribers)


class FakeYouTube:
    def __init__(self) -> None:
        self.videos_db: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        self.updates: list[dict[str, Any]] = []
        self.inserts: list[dict[str, Any]] = []
        self.thumbnail_sets: list[str] = []
        self.thumbnail_media: list[tuple[str, bytes]] = []  # (file name sent, bytes YouTube received)
        # Called just before the API reads an uploaded file: simulates the original being changed between
        # approval and the moment the client library reads the file.
        self.before_media_read: Any = None
        self.fail_next: dict[str, Exception] = {}
        self.upload_failures: list[Exception] = []
        self.upload_chunks = 3
        self.upload_privacy_override: str | None = None  # simulate YouTube forcing a visibility
        self.lock_private = False
        # Eventual consistency: after a write, the next `read_lag` reads of that resource return the
        # state from before the write (or "not found" for something just created).
        self.read_lag = 0
        self.retry_args: list[tuple[str, int]] = []
        self._lagged: dict[tuple[str, str], list[Any]] = {}
        self.caption_text: dict[str, bytes] = {}
        self.playlists_db: dict[str, dict[str, Any]] = {}
        self.playlist_items: dict[str, list[dict[str, Any]]] = {}
        self.playlist_updates: list[dict[str, Any]] = []
        self.captions_db: dict[str, dict[str, Any]] = {}
        self.caption_media: dict[str, Any] = {}
        self.caption_updates: list[dict[str, Any]] = []
        self.categories = [("1", "Film & Animation", True), ("22", "People & Blogs", True),
                           ("27", "Education", True), ("28", "Science & Technology", True),
                           ("30", "Movies", False)]
        self._counter = 0
        self.channel = {
            "id": "UC_fake_channel", "snippet": {"title": "Test Channel", "customUrl": "@testchannel"},
            "contentDetails": {"relatedPlaylists": {"uploads": "UU_fake_uploads"}},
            "statistics": {"videoCount": "2", "subscriberCount": "10", "viewCount": "100", "hiddenSubscriberCount": False},
            "status": {"longUploadsStatus": "allowed"},
        }

    def _snapshot(self, kind: str, key: str, before: Any) -> None:
        if self.read_lag:
            self._lagged[(kind, key)] = [copy.deepcopy(before), self.read_lag]

    def _view(self, kind: str, key: str, current: Any) -> Any:
        entry = self._lagged.get((kind, key))
        if entry and entry[1] > 0:
            entry[1] -= 1
            return copy.deepcopy(entry[0])
        return current

    def add_video(self, body: dict[str, Any], video_id: str | None = None) -> str:
        self._counter += 1
        vid = video_id or f"vid{self._counter:08d}"
        status = {"uploadStatus": "uploaded", "privacyStatus": body.get("status", {}).get("privacyStatus", "private"),
                  "license": "youtube", "embeddable": True, "publicStatsViewable": True,
                  "selfDeclaredMadeForKids": False, "madeForKids": False}
        status.update({k: v for k, v in body.get("status", {}).items()})
        if self.upload_privacy_override:
            status["privacyStatus"] = self.upload_privacy_override
        snippet = {"title": "", "description": "", "categoryId": "22", "channelId": "UC_fake_channel",
                   "publishedAt": "2026-11-26T00:00:00Z", "thumbnails": {"default": {"url": "x"}}}
        snippet.update(copy.deepcopy(body.get("snippet", {})))
        self.videos_db[vid] = {"snippet": snippet, "status": status,
                               "processingDetails": {"processingStatus": "processing",
                                                     "processingProgress": {"partsTotal": "10", "partsProcessed": "4",
                                                                            "timeLeftMs": "30000"}},
                               "contentDetails": {"duration": "PT1M5S"}}
        return vid

    def videos(self) -> _Videos:
        return _Videos(self)

    def channels(self) -> Any:
        api = self

        class _C:
            def list(self, part: str, mine: bool) -> FakeRequest:
                return FakeRequest(api, "channels.list", lambda: {"items": [copy.deepcopy(api.channel)]})
        return _C()

    def playlistItems(self) -> Any:
        api = self

        class _P:
            def list(self, part: str, playlistId: str | None = None, maxResults: int = 5, pageToken: str | None = None,
                     videoId: str | None = None, id: str | None = None) -> FakeRequest:
                def run() -> dict[str, Any]:
                    start = int(pageToken or 0)
                    if playlistId == "UU_fake_uploads":
                        ids = list(api.videos_db)[::-1]
                        page = ids[start:start + maxResults]
                        items = [{"snippet": {"title": api.videos_db[v]["snippet"]["title"], "publishedAt": "2026-11-26T00:00:00Z"},
                                  "contentDetails": {"videoId": v, "videoPublishedAt": "2026-11-26T00:00:00Z"},
                                  "status": {"privacyStatus": api.videos_db[v]["status"]["privacyStatus"]}} for v in page]
                        total = len(ids)
                    else:
                        if playlistId not in api.playlists_db:
                            raise http_error(404, "playlistNotFound", "Playlist not found")
                        entries = api._view("items", playlistId, api.playlist_items[playlistId])
                        if videoId:
                            entries = [e for e in entries if e["contentDetails"]["videoId"] == videoId]
                        total = len(entries)
                        items = copy.deepcopy(entries[start:start + maxResults])
                    nxt = start + maxResults
                    return {"items": items, "pageInfo": {"totalResults": total},
                            **({"nextPageToken": str(nxt)} if nxt < total else {})}
                return FakeRequest(api, "playlistItems.list", run)

            def insert(self, part: str, body: dict[str, Any]) -> FakeRequest:
                def run() -> dict[str, Any]:
                    sn = body["snippet"]
                    pid, vid = sn["playlistId"], sn["resourceId"]["videoId"]
                    if pid not in api.playlists_db:
                        raise http_error(404, "playlistNotFound", "Playlist not found")
                    if vid not in api.videos_db:
                        raise http_error(404, "videoNotFound", "Video not found")
                    entries = api.playlist_items[pid]
                    api._snapshot("items", pid, entries)
                    api._counter += 1
                    pos = sn.get("position", len(entries))
                    item = {"id": f"PLI{api._counter:06d}", "snippet": {"playlistId": pid, "position": pos,
                            "title": api.videos_db[vid]["snippet"]["title"],
                            "resourceId": {"kind": "youtube#video", "videoId": vid}},
                            "contentDetails": {"videoId": vid}, "status": {"privacyStatus": "unlisted"}}
                    entries.insert(pos, item)
                    for n, e in enumerate(entries):
                        e["snippet"]["position"] = n
                    api.playlists_db[pid]["contentDetails"]["itemCount"] = len(entries)
                    return copy.deepcopy(item)
                return FakeRequest(api, "playlistItems.insert", run)

            def delete(self, id: str) -> FakeRequest:
                def run() -> str:
                    for pid, entries in api.playlist_items.items():
                        for e in entries:
                            if e["id"] == id:
                                api._snapshot("items", pid, entries)
                                entries.remove(e)
                                api.playlists_db[pid]["contentDetails"]["itemCount"] = len(entries)
                                return ""
                    raise http_error(404, "playlistItemNotFound", "Playlist item not found")
                return FakeRequest(api, "playlistItems.delete", run)
        return _P()

    def add_playlist(self, title: str = "PL", privacy: str = "private", description: str = "") -> str:
        self._counter += 1
        pid = f"PLfake{self._counter:06d}"
        self.playlists_db[pid] = {"id": pid, "snippet": {"title": title, "description": description,
                                                          "publishedAt": "2026-11-26T00:00:00Z", "channelId": "UC_fake_channel"},
                                  "status": {"privacyStatus": privacy}, "contentDetails": {"itemCount": 0}}
        self.playlist_items[pid] = []
        return pid

    def playlists(self) -> Any:
        api = self

        class _PL:
            def list(self, part: str, mine: bool | None = None, id: str | None = None, maxResults: int = 5,
                     pageToken: str | None = None) -> FakeRequest:
                def run() -> dict[str, Any]:
                    if id is not None:
                        found = api._view("playlist", id, api.playlists_db.get(id))
                        items = [copy.deepcopy(found)] if found else []
                        return {"items": items, "pageInfo": {"totalResults": len(items)}}
                    vals = list(api.playlists_db.values())
                    return {"items": copy.deepcopy(vals[:maxResults]), "pageInfo": {"totalResults": len(vals)}}
                return FakeRequest(api, "playlists.list", run)

            def insert(self, part: str, body: dict[str, Any]) -> FakeRequest:
                def run() -> dict[str, Any]:
                    pid = api.add_playlist(body["snippet"]["title"], body.get("status", {}).get("privacyStatus", "public"),
                                           body["snippet"].get("description", ""))
                    api._snapshot("playlist", pid, None)
                    return copy.deepcopy(api.playlists_db[pid])
                return FakeRequest(api, "playlists.insert", run)

            def update(self, part: str, body: dict[str, Any]) -> FakeRequest:
                def run() -> dict[str, Any]:
                    pid = body["id"]
                    if pid not in api.playlists_db:
                        raise http_error(404, "playlistNotFound", "Playlist not found")
                    if not body.get("snippet", {}).get("title"):
                        raise http_error(400, "playlistTitleRequired", "Title required")
                    api.playlist_updates.append(copy.deepcopy(body))
                    api._snapshot("playlist", pid, api.playlists_db[pid])
                    for p in part.split(","):
                        keep = {"publishedAt": api.playlists_db[pid]["snippet"]["publishedAt"]} if p == "snippet" else {}
                        api.playlists_db[pid][p] = {**keep, **copy.deepcopy(body[p])}  # full replacement
                    return copy.deepcopy(api.playlists_db[pid])
                return FakeRequest(api, "playlists.update", run)
        return _PL()

    def captions(self) -> Any:
        api = self

        class _CAP:
            def list(self, part: str, videoId: str, id: str | None = None) -> FakeRequest:
                def run() -> dict[str, Any]:
                    if videoId not in api.videos_db:
                        raise http_error(404, "videoNotFound", "Video not found")
                    current = [c for c in api.captions_db.values() if c["snippet"]["videoId"] == videoId]
                    items = api._view("captions", videoId, current)
                    if id:
                        items = [c for c in items if c["id"] in id.split(",")]
                    return {"items": copy.deepcopy(items)}
                return FakeRequest(api, "captions.list", run)

            def insert(self, part: str, body: dict[str, Any], media_body: Any) -> FakeRequest:
                def run() -> dict[str, Any]:
                    sn = body["snippet"]
                    if sn["videoId"] not in api.videos_db:
                        raise http_error(404, "videoNotFound", "Video not found")
                    for c in api.captions_db.values():
                        if (c["snippet"]["videoId"], c["snippet"]["language"], c["snippet"]["name"]) == (
                                sn["videoId"], sn["language"], sn["name"]):
                            raise http_error(409, "captionExists", "Caption exists")
                    api._snapshot("captions", sn["videoId"],
                                  [c for c in api.captions_db.values() if c["snippet"]["videoId"] == sn["videoId"]])
                    api._counter += 1
                    cid = f"CAP{api._counter:06d}"
                    api.caption_media[cid] = media_body
                    if api.before_media_read:
                        api.before_media_read()
                    api.caption_text[cid] = open(media_body._filename, "rb").read()
                    api.captions_db[cid] = {"id": cid, "snippet": {**copy.deepcopy(sn), "trackKind": "standard",
                                                                   "status": "serving", "lastUpdated": "2026-11-26T00:00:00Z"}}
                    return copy.deepcopy(api.captions_db[cid])
                return FakeRequest(api, "captions.insert", run)

            def update(self, part: str, body: dict[str, Any], media_body: Any = None) -> FakeRequest:
                def run() -> dict[str, Any]:
                    cid = body["id"]
                    if cid not in api.captions_db:
                        raise http_error(404, "captionNotFound", "Caption not found")
                    cur = api.captions_db[cid]["snippet"]
                    api._snapshot("captions", cur["videoId"],
                                  [c for c in api.captions_db.values() if c["snippet"]["videoId"] == cur["videoId"]])
                    api.caption_updates.append(copy.deepcopy(body))
                    cur["isDraft"] = body["snippet"].get("isDraft", False)
                    if media_body is not None:
                        api.caption_media[cid] = media_body
                        if api.before_media_read:
                            api.before_media_read()
                        api.caption_text[cid] = open(media_body._filename, "rb").read()
                        cur["lastUpdated"] = f"2026-11-26T00:00:{len(api.caption_updates):02d}Z"
                    return copy.deepcopy(api.captions_db[cid])
                return FakeRequest(api, "captions.update", run)
            def download_media(self, id: str, tfmt: str | None = None) -> FakeRequest:
                def run() -> bytes:
                    if id not in api.caption_text:
                        raise http_error(404, "captionNotFound", "Caption not found")
                    return api.caption_text[id]
                return FakeRequest(api, "captions.download", run)
        return _CAP()

    def videoCategories(self) -> Any:
        api = self

        class _VC:
            def list(self, part: str, regionCode: str, hl: str = "en_US") -> FakeRequest:
                def run() -> dict[str, Any]:
                    return {"items": [{"id": cid, "snippet": {"title": t, "assignable": a, "channelId": "UCBR"}}
                                      for cid, t, a in api.categories]}
                return FakeRequest(api, "videoCategories.list", run)
        return _VC()

    def thumbnails(self) -> Any:
        api = self

        class _T:
            def set(self, videoId: str, media_body: Any) -> FakeRequest:
                def run() -> dict[str, Any]:
                    if videoId not in api.videos_db:
                        raise http_error(404, "videoNotFound", "Video not found")
                    api.thumbnail_sets.append(videoId)
                    if api.before_media_read:
                        api.before_media_read()
                    api.thumbnail_media.append((media_body._filename, open(media_body._filename, "rb").read()))
                    return {"items": [{"default": {"url": f"https://i.ytimg.com/vi/{videoId}/default.jpg"}}]}
                return FakeRequest(api, "thumbnails.set", run)
        return _T()


class FakeAnalytics:
    """Fake youtubeAnalytics v2: reports().query(**params).execute() -> resultTable."""

    def __init__(self) -> None:
        self.queries: list[dict[str, Any]] = []
        self.fail_for_dimension: dict[str, Exception] = {}
        self.fail_all: Exception | None = None

    def reports(self) -> "FakeAnalytics":
        return self

    def query(self, **params: Any) -> Any:
        api = self

        class _R:
            def execute(self, num_retries: int = 0) -> dict[str, Any]:
                api.queries.append(params)
                if api.fail_all:
                    raise api.fail_all
                dims = params.get("dimensions")
                if dims in api.fail_for_dimension:
                    raise api.fail_for_dimension[dims]
                metrics = params["metrics"].split(",")
                headers = [{"name": d} for d in (dims.split(",") if dims else [])] + [{"name": m} for m in metrics]

                def vals(seed: int) -> list[Any]:
                    return [seed * 10 + n for n, _ in enumerate(metrics)]
                if not dims:
                    rows = [vals(10)]
                elif dims == "day":
                    rows = [["2026-11-20", *vals(1)], ["2026-11-21", *vals(2)]]
                elif dims == "insightTrafficSourceType":
                    rows = [["YT_SEARCH", *vals(5)], ["EXT_URL", *vals(2)]]
                elif dims == "elapsedVideoTimeRatio":
                    rows = [[0.01, 1.0, 0.5], [0.5, 0.6, 0.55], [1.0, 0.2, 0.4]]
                elif dims == "video":
                    rows = [[f"vid{n:08d}", *vals(10 - n)] for n in range(1, 4)][: params.get("maxResults", 10)]
                else:
                    rows = []
                return {"kind": "youtubeAnalytics#resultTable", "columnHeaders": headers, "rows": rows}
        return _R()
