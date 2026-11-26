"""Playlist management for the authorized channel.

API facts (YouTube Data API v3 reference): playlists.list / playlistItems.list cost 1 unit;
playlists.insert/update and playlistItems.insert/delete cost 50 units. playlists.update replaces
the parts it is sent (an omitted description is deleted), so the current snippet is read and
merged first. snippet.position on insert only works for manually sorted playlists.

Every change requires the two-step approval. New playlists are PRIVATE by default; creating or
switching a playlist to public additionally needs confirm_public=true.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from . import approval
from .verify import UNVERIFIABLE, write_report
from .config import VISIBILITIES
from .errors import YouTubeMCPError
from .youtube import invalid, parse_video_id, validate_description, validate_title

if TYPE_CHECKING:
    from .service import YouTubeMCPService

DEFAULT_PLAYLIST_PRIVACY = "private"
PLAYLIST_SNIPPET_FIELDS = ("title", "description", "defaultLanguage")
MAX_PLAYLIST_TITLE = 150
RECENT_ADD_WINDOW = timedelta(minutes=5)
MAX_EXPOSURE_PAGES = 100  # 5,000 entries: YouTube's maximum playlist size


def _playlist_id(value: str) -> str:
    pid = (value or "").strip()
    if "list=" in pid:
        pid = pid.split("list=", 1)[1].split("&", 1)[0]
    if not pid or not all(c.isalnum() or c in "-_" for c in pid):
        raise invalid(f"'{value}' is not a valid playlist ID.", "Use list_playlists to find the playlist ID.")
    return pid


def _privacy(value: str) -> str:
    v = (value or "").strip().lower()
    if v not in VISIBILITIES:
        raise invalid(f"Playlist privacy must be one of {', '.join(VISIBILITIES)}; got '{value}'.")
    return v


def _playlist_title(title: str) -> str:
    title = (title or "").strip()
    if not title:
        raise invalid("A playlist title is required.")
    if len(title) > MAX_PLAYLIST_TITLE:
        raise invalid(f"Playlist title is {len(title)} characters; the maximum is {MAX_PLAYLIST_TITLE}.")
    if "<" in title or ">" in title:
        raise invalid("Playlist titles cannot contain '<' or '>'.")
    return title


def _summary(p: dict[str, Any]) -> dict[str, Any]:
    sn, st = p.get("snippet", {}), p.get("status", {})
    pid = p.get("id")
    return {"playlist_id": pid, "title": sn.get("title"), "description": sn.get("description", ""),
            "privacy": st.get("privacyStatus"), "item_count": p.get("contentDetails", {}).get("itemCount"),
            "published_at": sn.get("publishedAt"), "url": f"https://www.youtube.com/playlist?list={pid}"}


def _item(i: dict[str, Any]) -> dict[str, Any]:
    sn, cd = i.get("snippet", {}), i.get("contentDetails", {})
    vid = cd.get("videoId") or sn.get("resourceId", {}).get("videoId")
    return {"playlist_item_id": i.get("id"), "video_id": vid, "title": sn.get("title"),
            "position": sn.get("position"), "privacy": i.get("status", {}).get("privacyStatus"),
            "video_owner_channel": sn.get("videoOwnerChannelTitle"),
            "url": f"https://youtu.be/{vid}" if vid else None}


def _page(resp: dict[str, Any], items: list[dict[str, Any]], page_token: str | None, why: str) -> dict[str, Any]:
    """Pagination with YouTube's reported total kept separate from what was actually returned."""
    reported = resp.get("pageInfo", {}).get("totalResults")
    nxt = resp.get("nextPageToken")
    out: dict[str, Any] = {"ok": True, "items": items, "returned_count": len(items),
                           "api_reported_total": reported, "has_more": bool(nxt), "next_page_token": nxt,
                           "total": reported}  # deprecated alias of api_reported_total
    if not nxt and page_token is None and reported is not None and len(items) < int(reported):
        out["discrepancy"] = {"api_reported_total": reported, "returned_count": len(items),
                              "message": f"YouTube reports {reported} but returned {len(items)} with no further pages. "
                                         + why}
    elif not nxt and page_token is not None and reported is not None:
        out["note"] = "Last page. Compare the number of items you received across all pages with api_reported_total."
    return out


class PlaylistsService:
    def __init__(self, svc: "YouTubeMCPService") -> None:
        self.svc = svc

    @property
    def client(self) -> Any:
        return self.svc.client

    # read-only --------------------------------------------------------------------

    def list_playlists(self, max_results: int = 25, page_token: str | None = None) -> dict[str, Any]:
        resp = self.client.list_my_playlists(max(1, min(int(max_results), 50)), page_token)
        return _page(resp, [_summary(p) for p in resp.get("items") or []], page_token,
                     "YouTube's totalResults for playlists can exceed the playlists it actually returns "
                     "(observed live: 22 reported, 15 returned across all pages). The difference is not "
                     "explained by the API docs; this server does not invent the missing entries.")

    def list_playlist_items(self, playlist_id: str, max_results: int = 25,
                            page_token: str | None = None) -> dict[str, Any]:
        playlist_id = _playlist_id(playlist_id)
        resp = self.client.list_playlist_entries(playlist_id, max(1, min(int(max_results), 50)), page_token)
        return {"playlist_id": playlist_id,
                **_page(resp, [_item(i) for i in resp.get("items") or []], page_token,
                        "YouTube's totalResults can include entries it does not return (e.g. deleted or private "
                        "videos). Nothing is invented.")}

    def _entries_for_video(self, playlist_id: str, video_id: str) -> list[dict[str, Any]]:
        return self.client.list_playlist_entries(playlist_id, 50, None, video_id=video_id).get("items") or []

    def _find_item(self, playlist_id: str, playlist_item_id: str, max_pages: int = 100) -> dict[str, Any] | None:
        token = None
        for _ in range(max_pages):
            resp = self.client.list_playlist_entries(playlist_id, 50, token)
            for entry in resp.get("items") or []:
                if entry.get("id") == playlist_item_id:
                    return entry
            token = resp.get("nextPageToken")
            if not token:
                return None
        return None

    # exposure checks (read-only) -------------------------------------------------------
    # An UNLISTED video in a PUBLIC playlist is visible to anyone who views the playlist. These helpers
    # find such indirect exposure so it can be disclosed, bound to the approval and natively confirmed.

    def unlisted_videos_in(self, playlist_id: str, max_pages: int = MAX_EXPOSURE_PAGES) -> dict[str, Any]:
        """Unlisted videos in a playlist (all pages, up to max_pages x 50 entries)."""
        ids: list[str] = []
        token, complete = None, False
        for _ in range(max_pages):
            resp = self.client.list_playlist_entries(playlist_id, 50, token)
            ids += [e.get("contentDetails", {}).get("videoId") or e.get("snippet", {}).get("resourceId", {}).get("videoId")
                    for e in resp.get("items") or []]
            token = resp.get("nextPageToken")
            if not token:
                complete = True
                break
        ids = [i for i in dict.fromkeys(ids) if i]
        videos: dict[str, dict[str, Any]] = {}
        for start in range(0, len(ids), 50):
            videos.update(self.client.get_videos(ids[start:start + 50], parts="snippet,status"))
        unlisted = [{"video_id": vid, "title": v.get("snippet", {}).get("title")}
                    for vid, v in videos.items() if v.get("status", {}).get("privacyStatus") == "unlisted"]
        unlisted.sort(key=lambda x: x["video_id"])
        return {"videos": unlisted, "checked_entries": len(ids), "complete": complete}

    def public_playlists_containing(self, video_id: str, max_pages: int = MAX_EXPOSURE_PAGES) -> dict[str, Any]:
        """This channel's PUBLIC playlists that contain the video. Other channels' playlists can't be listed."""
        found: list[dict[str, Any]] = []
        token, complete = None, False
        for _ in range(max_pages):
            resp = self.client.list_my_playlists(50, token)
            for pl in resp.get("items") or []:
                if pl.get("status", {}).get("privacyStatus") == "public" and self._entries_for_video(pl["id"], video_id):
                    found.append({"playlist_id": pl["id"], "title": pl.get("snippet", {}).get("title")})
            token = resp.get("nextPageToken")
            if not token:
                complete = True
                break
        found.sort(key=lambda x: x["playlist_id"])
        return {"playlists": found, "complete": complete}

    # create -----------------------------------------------------------------------

    def create_playlist(self, title: str, description: str = "", privacy: str = DEFAULT_PLAYLIST_PRIVACY,
                        confirmation_token: str | None = None, confirm_public: bool = False) -> dict[str, Any]:
        title = _playlist_title(title)
        description = validate_description(description)
        privacy = _privacy(privacy or DEFAULT_PLAYLIST_PRIVACY)
        params = {"title": title, "description": description, "privacy": privacy}
        public = privacy == "public"
        if not confirmation_token:
            return approval.confirmation_required(
                self.svc.confirmations, "create_playlist", params,
                {"title": title, "description": description, "privacy": privacy}, public=public,
                warnings=["PLAYLIST WILL BE PUBLIC: anyone can find it."] if public else None)
        approval.consume(self.svc.confirmations, "create_playlist", confirmation_token, params,
                         public=public, confirm_public=confirm_public)
        if public:
            self.svc.confirm_public("Create a PUBLIC playlist", [("Playlist title", title)])
        created = self.client.insert_playlist({"snippet": {"title": title, "description": description},
                                               "status": {"privacyStatus": privacy}})
        pid = created.get("id")
        self.svc.history.log_action("create_playlist", None, {"playlist_id": pid, "title": title, "privacy": privacy})
        # A new playlist is often "not found" for a few seconds: retry the READ, never the insert.
        ver = self.svc.verifier.wait_for(
            lambda: self.client.get_playlist(pid),
            lambda p: p.get("snippet", {}).get("title") == title and p.get("status", {}).get("privacyStatus") == privacy)
        summary = _summary(ver.observed if ver.observed else created)
        return {"ok": True, "status": "created", **summary,
                **write_report(ver, resource={"playlist_id": pid}, check_with="list_playlists",
                               manual_attention=privacy == "public")}

    # update -----------------------------------------------------------------------

    def update_playlist(self, playlist_id: str, title: str | None = None, description: str | None = None,
                        privacy: str | None = None, confirmation_token: str | None = None,
                        confirm_public: bool = False) -> dict[str, Any]:
        playlist_id = _playlist_id(playlist_id)
        changes: dict[str, Any] = {}
        if title is not None:
            changes["title"] = _playlist_title(title)
        if description is not None:
            changes["description"] = validate_description(description)
        new_privacy = _privacy(privacy) if privacy is not None else None
        if not changes and new_privacy is None:
            raise invalid("Nothing to update.", "Pass title, description and/or privacy.")

        current = self.client.get_playlist(playlist_id)
        snippet = {k: current.get("snippet", {})[k] for k in PLAYLIST_SNIPPET_FIELDS if k in current.get("snippet", {})}
        status = {k: v for k, v in current.get("status", {}).items() if k in ("privacyStatus", "podcastStatus")}
        new_snippet = {**snippet, **changes}
        new_status = {**status, **({"privacyStatus": new_privacy} if new_privacy else {})}
        if new_status.get("podcastStatus") == "unspecified":
            new_status.pop("podcastStatus")
        diff = {k: {"before": snippet.get(k), "after": new_snippet.get(k)}
                for k in changes if snippet.get(k) != new_snippet.get(k)}
        if new_privacy and new_privacy != status.get("privacyStatus"):
            diff["privacy"] = {"before": status.get("privacyStatus"), "after": new_privacy}
        if not diff:
            return {"ok": True, "status": "no_change", "playlist_id": playlist_id}
        public = new_privacy == "public" and status.get("privacyStatus") != "public"
        params: dict[str, Any] = {"playlist_id": playlist_id, "snippet": new_snippet, "status": new_status}
        exposed: dict[str, Any] | None = None
        if public:
            # Unlisted videos in the playlist become visible to anyone viewing it. The exposed set is bound to
            # the approval: if it changes before confirmation, the token no longer matches (fresh preview needed).
            exposed = self.unlisted_videos_in(playlist_id)
            params["exposed_unlisted_video_ids"] = [v["video_id"] for v in exposed["videos"]]
            params["exposure_check_complete"] = exposed["complete"]
        if not confirmation_token:
            preview: dict[str, Any] = {"playlist_id": playlist_id, "title": snippet.get("title"), "changes": diff}
            warnings = []
            if public:
                assert exposed is not None
                preview["unlisted_videos_exposed"] = {"count": len(exposed["videos"]),
                                                      "videos": exposed["videos"][:25],
                                                      "check_complete": exposed["complete"]}
                warnings.append("PLAYLIST WILL BECOME PUBLIC.")
                if exposed["videos"]:
                    warnings.append(f"{len(exposed['videos'])} UNLISTED video(s) in this playlist will become visible "
                                    "to anyone viewing it.")
                if not exposed["complete"]:
                    warnings.append("The playlist is too large to check every entry; more unlisted videos may be "
                                    "exposed.")
            return approval.confirmation_required(
                self.svc.confirmations, "update_playlist", params, preview, public=public,
                warnings=warnings or None)
        approval.consume(self.svc.confirmations, "update_playlist", confirmation_token, params,
                         public=public, confirm_public=confirm_public)
        if public:
            assert exposed is not None
            details = [("Playlist title", new_snippet.get("title")), ("Current privacy", status.get("privacyStatus")),
                       ("Unlisted videos exposed", str(len(exposed["videos"]))
                        + ("" if exposed["complete"] else " or more (not all entries checked)"))]
            details += [("Exposed video", v["title"] or v["video_id"]) for v in exposed["videos"][:5]]
            self.svc.confirm_public("Make a playlist PUBLIC", details)
        self.client.update_playlist({"id": playlist_id, "snippet": new_snippet, "status": new_status})
        self.svc.history.log_action("update_playlist", None, {"playlist_id": playlist_id, "changed": diff})
        ver = self.svc.verifier.wait_for(
            lambda: self.client.get_playlist(playlist_id),
            lambda p: all(p.get("snippet", {}).get(k, "") == new_snippet.get(k, "") for k in ("title", "description"))
            and p.get("status", {}).get("privacyStatus") == new_status.get("privacyStatus"))
        # Report what YouTube actually shows; if unconfirmed this may still be the old state.
        after = _summary(ver.observed) if ver.observed else {"playlist_id": playlist_id}
        return {"ok": True, "status": "updated", "changed": diff, "observed": after,
                **{k: v for k, v in after.items() if k != "playlist_id"},
                **write_report(ver, resource={"playlist_id": playlist_id}, check_with="list_playlists",
                               manual_attention="privacy" in diff)}

    # items ------------------------------------------------------------------------

    def add_to_playlist(self, playlist_id: str, video_id: str, position: int | None = None,
                        confirmation_token: str | None = None) -> dict[str, Any]:
        playlist_id = _playlist_id(playlist_id)
        video_id = parse_video_id(video_id)
        if position is not None and int(position) < 0:
            raise invalid("position must be 0 or greater.")

        def check_duplicate() -> None:
            existing = self._entries_for_video(playlist_id, video_id)
            if existing:
                raise YouTubeMCPError(
                    "playlist_duplicate", "This video is already in the playlist; it was not added again.",
                    action="Nothing to do. Use list_playlist_items to see the playlist.",
                    details={"playlist_item_id": existing[0].get("id"), "playlist_id": playlist_id,
                             "video_id": video_id})

        params = {"playlist_id": playlist_id, "video_id": video_id, "position": position}

        def exposure() -> tuple[dict[str, Any], dict[str, Any], bool]:
            playlist = self.client.get_playlist(playlist_id)
            video = self.client.get_video(video_id, parts="snippet,status")
            # An unlisted video in a PUBLIC playlist becomes visible to anyone viewing the playlist.
            exposes = (playlist.get("status", {}).get("privacyStatus") == "public"
                       and video.get("status", {}).get("privacyStatus") == "unlisted")
            return playlist, video, exposes

        playlist, video, exposes = exposure()
        params["exposes_unlisted_video"] = exposes  # bound to the approval; a change needs a fresh preview
        if not confirmation_token:
            check_duplicate()
            return approval.confirmation_required(
                self.svc.confirmations, "add_to_playlist", params,
                {"playlist_id": playlist_id, "playlist_title": playlist.get("snippet", {}).get("title"),
                 "playlist_privacy": playlist.get("status", {}).get("privacyStatus"),
                 "video_id": video_id, "video_title": video.get("snippet", {}).get("title"),
                 "video_visibility": video.get("status", {}).get("privacyStatus"),
                 "position": "end of playlist" if position is None else position},
                warnings=(["This unlisted video will become visible to anyone viewing this PUBLIC playlist."]
                          if exposes else None))
        approval.consume(self.svc.confirmations, "add_to_playlist", confirmation_token, params)
        if exposes:
            self.svc.confirm_public("Add an unlisted video to a PUBLIC playlist", [
                ("Playlist", playlist.get("snippet", {}).get("title")),
                ("Video title", video.get("snippet", {}).get("title")), ("Video visibility", "unlisted")])
        snippet: dict[str, Any] = {"playlistId": playlist_id,
                                   "resourceId": {"kind": "youtube#video", "videoId": video_id}}
        if position is not None:
            snippet["position"] = int(position)
        # Serialise check-and-insert across local processes. YouTube's list can lag behind a just-made insert,
        # so the local record of a recent add by another process also counts as "already added".
        with self.svc.history.exclusive(f"playlist:{playlist_id}:{video_id}"):
            check_duplicate()  # re-check right before writing
            self._check_recent_local_add(playlist_id, video_id)
            created = self.client.insert_playlist_item({"snippet": snippet})
            item_id = created.get("id")
            self.svc.history.log_action("add_to_playlist", video_id, {"playlist_id": playlist_id,
                                                                      "playlist_item_id": item_id})
        ver = self.svc.verifier.wait_for(lambda: self._entries_for_video(playlist_id, video_id),
                                         lambda entries: any(e.get("id") == item_id for e in entries))
        return {"ok": True, "status": "added", "position": created.get("snippet", {}).get("position"),
                **write_report(ver, resource={"playlist_id": playlist_id, "video_id": video_id,
                                              "playlist_item_id": item_id},
                               check_with="list_playlist_items", manual_attention=exposes)}

    def _check_recent_local_add(self, playlist_id: str, video_id: str) -> None:
        last = self.svc.history.last_playlist_change(playlist_id, video_id)
        if not last or last["action"] != "add_to_playlist":
            return
        when = datetime.fromisoformat(last["ts"])
        if datetime.now(timezone.utc) - when > RECENT_ADD_WINDOW:
            return  # old enough that YouTube's list is authoritative (it may have been removed elsewhere)
        raise YouTubeMCPError(
            "playlist_duplicate",
            "This video was added to the playlist moments ago (possibly by another request), and YouTube may not "
            "show it yet. It was not added again.",
            action="Do not repeat the request. Check again in a minute with list_playlist_items.",
            details={"playlist_id": playlist_id, "video_id": video_id,
                     "playlist_item_id": last["details"].get("playlist_item_id"), "pending": True})

    def remove_from_playlist(self, playlist_id: str, video_id: str | None = None,
                             playlist_item_id: str | None = None,
                             confirmation_token: str | None = None) -> dict[str, Any]:
        """Remove an entry from a playlist. The video itself is never deleted."""
        playlist_id = _playlist_id(playlist_id)
        if not video_id and not playlist_item_id:
            raise invalid("Pass video_id or playlist_item_id.")
        target: dict[str, Any] | None = None
        if playlist_item_id:
            target = self._find_item(playlist_id, playlist_item_id.strip())
            if target is None:
                raise YouTubeMCPError("playlist_item_not_found", "That item is not in the playlist.",
                                      action="Use list_playlist_items to find the correct playlist_item_id.")
        else:
            vid = parse_video_id(video_id or "")
            matches = self._entries_for_video(playlist_id, vid)
            if len(matches) > 1:
                raise invalid("This video appears more than once in the playlist.",
                              "Pass the specific playlist_item_id (see list_playlist_items).",
                              playlist_item_ids=[m.get("id") for m in matches])
            target = matches[0] if matches else None
        if target is None:
            return {"ok": True, "status": "no_change", "message": "The video is not in this playlist."}
        item = _item(target)
        params = {"playlist_id": playlist_id, "playlist_item_id": item["playlist_item_id"]}
        if not confirmation_token:
            return approval.confirmation_required(
                self.svc.confirmations, "remove_from_playlist", params,
                {"playlist_id": playlist_id, **item,
                 "effect": "Removes the entry from the playlist only. The video itself is not deleted."})
        approval.consume(self.svc.confirmations, "remove_from_playlist", confirmation_token, params)
        self.client.delete_playlist_item(item["playlist_item_id"])
        self.svc.history.log_action("remove_from_playlist", item["video_id"], params)
        ver = self.svc.verifier.wait_for(
            lambda: self._entries_for_video(playlist_id, item["video_id"]) if item["video_id"] else [],
            lambda entries: not any(e.get("id") == item["playlist_item_id"] for e in entries))
        return {"ok": True, "status": "removed",
                **write_report(ver, resource={**params, "video_id": item["video_id"]},
                               check_with="list_playlist_items")}
