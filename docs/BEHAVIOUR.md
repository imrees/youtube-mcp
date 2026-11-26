# How it behaves

Details of what the tools do, for users who want to know exactly what to expect.

## Uploads

1. `upload_video` validates the file and metadata, computes the file's SHA-256 and checks the local history for
   duplicates. It returns a preview and a confirmation token.
2. After you approve, the file is snapshotted (see [Security](SECURITY.md)) and uploaded in a background thread as
   a **resumable upload** in 8 MB chunks. Transient failures (network drops, HTTP 5xx, 429) are retried up to 8
   times with exponential backoff; before each retry the server asks YouTube how many bytes it already has.
3. The tool waits up to `wait_seconds` (default 20) and then returns progress. Use `get_upload_progress` for longer
   uploads. On macOS a notification appears when the upload finishes or fails.
4. When the upload completes, the server reads back the video's actual visibility, corrects it if YouTube made it
   public unexpectedly, and records the result in the history.

**Duplicate protection.** A file whose content matches a completed upload is refused (`duplicate_upload`, with
the existing link) unless you pass `allow_duplicate=true`. Failed and interrupted uploads don't count.

**Interruptions.** If the server stops mid-upload (for example when the MCP client quits), the record is marked
`interrupted` the next time the server starts. YouTube can't resume a session from a previous process, so the
file has to be uploaded again (see [Unclear outcomes](#unclear-outcomes)). An upload left "active" by a crashed process is treated as stale only when its process is gone,
or when that process ID now belongs to a different program.

**Several processes.** All server instances and the Finder Quick Action share `data/history.sqlite3`. The
duplicate check and the new record are written in one SQLite `IMMEDIATE` transaction, so the same file can't be
started twice at once.

### Unclear outcomes

Every failed or interrupted attempt is recorded with what is known about it (`error.outcome`):

| Outcome | When | Uploading the file again |
|---|---|---|
| `not_sent` | it failed before any video data was sent (for example sign-in or file errors, or it was still queued) | proceeds normally |
| `rejected` | YouTube answered with a refusal (HTTP 4xx, for example `quota_exceeded`) | proceeds normally |
| `uncertain` | data was sent but no definite answer arrived (connection lost, server error, interrupted mid-transfer) | checked first, as below |

An `uncertain` attempt may still have reached YouTube (for example when the connection drops after the last byte).
Before the same file is uploaded again, the server checks the channel's 50 most recent uploads published after
the failed attempt began, comparing independent signals:

- the exact byte size (`fileDetails.fileSize`);
- the exact title and description that were sent;
- the duration, if macOS Spotlight knows it;
- whether the video is already linked to another history record.

The check costs about 3 quota units.

- **Exactly one video matches every signal, and nothing else partly matches:** the earlier attempt is recorded as
  completed, with a `recovered_after_failure` warning listing the evidence. You get that link instead of a new
  upload.
- **A video matches only partly, or several match:** you get an `ambiguous_upload` error listing the candidates.
  Nothing is linked or uploaded. After checking YouTube Studio, use `allow_duplicate=true` if you still want to
  upload the file again.
- **No evidence:** you get an **`upload_outcome_uncertain`** error and nothing is uploaded. Not finding the video
  doesn't prove YouTube never received it: new videos can take a while to be listed. Check YouTube Studio; if the
  video isn't there and you want to upload the file again, use `allow_duplicate=true` (with the Finder Quick
  Action, run the Terminal command shown in its notification, which adds `--allow-duplicate`).

Records written by older versions don't store an outcome; they're treated as `rejected` when the error code is a
definite refusal, and as `uncertain` otherwise.

## Verification after writes

YouTube's API is *eventually consistent*: right after a change, reads can still show the old state, or "not found"
for something just created. Every write tool therefore reports the write and its verification separately:

| Field | Meaning |
|---|---|
| `write_accepted: true` | YouTube's API returned success for the write |
| `verified: true` / `verification.status: "confirmed"` | A later read showed exactly the expected state |
| `verification.status: "not_confirmed"` | Reads worked but still showed a different state after about 15 s; usually propagation delay. Comes with a `verification_pending` warning |
| `verification.status: "unverifiable"` | The reads themselves failed (for example, the network) |
| `requires_manual_attention: true` | An unconfirmed change to **privacy or publishing**: check it in YouTube Studio |
| error `write_outcome_uncertain` | The connection failed or YouTube errored **during** the write; it may or may not have been applied |
| any other error | YouTube rejected the write, so nothing changed |

The one write the server makes on its own is the correction of an upload that YouTube unexpectedly reports as
**public**: it's sent once, and "corrected" is only reported when a read-back confirms the requested visibility.
Otherwise the upload's result carries an urgent `unexpectedly_public` warning with the YouTube Studio link (and on
macOS a "CHECK VISIBILITY NOW" notification).

Only **reads** are retried: up to six reads with exponential backoff (0.5, 1, 2, 4 and 8 s between them, about 15 s
in total). A write is never repeated. An unconfirmed change comes with a warning telling the assistant *not* to
repeat it, and to check again later with the read tool it names (`get_video_status`, `get_publish_schedule`,
`list_playlists`, `list_playlist_items` or `list_captions`). Each result includes the affected IDs (`video_id`,
`playlist_id`, `playlist_item_id`, `caption_id`).

Some things can't be fully verified:

- **Thumbnails.** `set_thumbnail` reports `accepted_by_youtube: true` and `image_independently_verified: false`.
  The thumbnail URLs don't change and YouTube re-encodes the image, so the API can't show which image is served.
- **Caption text.** Caption tracks are verified by their metadata (draft status, and `lastUpdated` when a file is
  replaced). With `verify_content=true` (shown in the preview and bound to the approval), the server also downloads
  the track (200 extra quota units) and compares the text of each cue. YouTube re-times cues, so timings are not
  compared.

## Pagination

List tools return:

| Field | Meaning |
|---|---|
| `returned_count` | items in this response |
| `api_reported_total` | YouTube's `pageInfo.totalResults` |
| `has_more`, `next_page_token` | pass the token back to get the next page |
| `total` | deprecated alias of `api_reported_total` |

**YouTube's reported total can be larger than what it actually returns.** On one live channel `playlists.list`
reported `totalResults: 22`, but walking every page (5 per page, and 1 per page) returned the same 15 unique
playlists, with no further page tokens. The raw API responses showed the gap, so it comes from YouTube, not from
this server's paging. YouTube's documentation doesn't explain it; possible causes include playlists that the API
counts but doesn't list (for example system or deleted playlists), but this is unconfirmed. When the final page
returns fewer items than the reported total, the response includes a `discrepancy` object describing the gap.
Missing entries are never invented, and the reported total is never "corrected".

Similarly, `get_channel_info` returns `public_video_count` (YouTube's `statistics.videoCount`, which counts
**public** videos only) separately from `total_uploads` (which includes unlisted and private videos).

## Captions

`upload_captions` takes a UTF-8 SubRip (`.srt`) or WebVTT (`.vtt`) file, a BCP-47 `language` (default `en`) and an
optional track `name`. The file is checked locally first (header and cue timestamps). If a track with the same
language and name already exists you get `caption_exists`; use `update_captions` to replace that track's file.
YouTube's `captions.update` can only replace the file and change the draft status; it can't rename a track or
change its language (`unsupported_by_youtube`). Automatic (`asr`) tracks are listed but can't be edited.

`list_captions` returns metadata only. `download_captions` returns a track's text (srt, vtt, sbv, ttml or scc,
up to 200,000 characters) for videos on your own channel.

## Playlists

New playlists are **private** unless you ask otherwise. `add_to_playlist` refuses duplicates, checking again right
before writing in case the video was added after the preview. The check and the insert run under a lock shared by
every local process (all server instances and the Quick Action), and an add recorded locally in the last 5 minutes
also counts, because YouTube's list can take a moment to show a new entry; such a refusal is marked `pending`.
**Limitation:** changes made at the same moment from outside this computer (YouTube Studio, another app) can't be
coordinated, so a duplicate entry is still possible in that case.

**Indirect exposure.** An unlisted video in a **public** playlist can be watched by anyone viewing the playlist.
So:

- Making a playlist public: the preview lists the unlisted videos in it that would become visible, and the native
  dialog shows how many.
- Adding an unlisted video to a public playlist: native dialog.
- Changing a video from private to unlisted when it's in one of the channel's public playlists: the preview lists
  those playlists, and the change needs `confirm_public=true` and the native dialog. Only this channel's playlists
  can be checked; playlists on other channels can't be listed through the API.

What was found is part of the approval: if it changes between the preview and the confirmation (for example a video
is added to the playlist), the token no longer matches and a fresh preview is needed. `position` only works for playlists sorted manually
in YouTube Studio. `update_playlist` reads the playlist and merges your changes before writing, because YouTube's
update replaces whole sections.

## Scheduled publishing

YouTube's rules (`status.publishAt`): a schedule can only be set on a video that is **private** and has **never
been published**. At the scheduled time, YouTube makes the video **public**.

- Times without an offset are read in `YOUTUBE_MCP_TIMEZONE` (default: your computer's timezone), for example
  `2026-12-01 18:30`. An explicit offset or `Z` is also accepted. Date-only values and times that don't exist
  because of a daylight-saving change are rejected.
- The time must be at least 10 minutes in the future. It is checked when the preview is made and again immediately
  before the API request, because YouTube publishes straight away if the time has passed.
- **The server never changes a video's visibility to make it eligible.** If the video is unlisted or public, the
  request is refused (`unsupported_by_youtube`). Uploads are unlisted by default, so to schedule one, upload it
  with `visibility="private"`.
- YouTube's documentation doesn't describe how to unschedule. `cancel_publish_schedule` re-sends the status without
  `publishAt`, keeps the video private, and confirms the removal by reading it back.

## Analytics

**Date ranges.** Presets `last_7_days`, `last_28_days` (default) and `last_90_days`, or a custom `start_date` /
`end_date` (`YYYY-MM-DD`). Reporting dates are in **Pacific Time** (America/Los_Angeles) and presets end on
today's Pacific date. Data typically lags 48–72 hours; `data_coverage` gives the last day YouTube has actually
reported, and totals are never extrapolated.

**Fields.** Values are returned exactly as YouTube sends them. A few fields are calculated by this server and are
listed in `derived_fields`: `watch_time_hours`, `net_subscribers`, `engagement_interactions` (likes + comments +
shares) and `engagement_rate_percent`.

**Why the numbers don't multiply out.** `views`, `watch_time_minutes` and `average_view_duration_seconds` are
separate YouTube metrics with different definitions: `views` counts plays from the first frame including Shorts
replays; average duration is averaged per playback and excludes looping-clip traffic; `watch_time_minutes`
(`estimatedMinutesWatched`) is an estimate in whole minutes. So views × average duration usually isn't equal to
watch time, and the server never adjusts the values to make them agree. Each response includes
`metric_definitions`, and `engaged_views` (YouTube's stricter view count) is shown alongside `views`.

**Partial results.** If YouTube doesn't support one optional section (for example retention for a very short
video), that section appears under `unavailable` with its reason, and the rest of the report is still returned.
Audience retention is available for a single video only.

## Errors

Failed tool calls return an error result with a JSON body:

```json
{"ok": false, "error": {"code": "auth_expired", "message": "...", "action": "Ask the user to re-authorize by running in Terminal: ...", "retryable": false}}
```

| Group | Codes |
|---|---|
| Sign-in and setup | `not_authorized`, `auth_expired`, `insufficient_scope`, `api_not_enabled`, `analytics_not_enabled`, `no_channel` |
| Input | `invalid_input`, `invalid_metadata`, `invalid_schedule`, `file_not_found` |
| Approval | `confirmation_invalid`, `confirmation_expired`, `confirmation_mismatch`, `public_requires_approval`, `native_confirmation_denied`, `native_confirmation_timeout`, `native_confirmation_unavailable` |
| Uploads | `duplicate_upload`, `upload_in_progress`, `ambiguous_upload`, `upload_outcome_uncertain`, `file_changed_since_approval`, `insufficient_disk_space`, `upload_limit_exceeded`, `upload_not_allowed` |
| Resources | `video_not_found`, `caption_exists`, `caption_not_found`, `playlist_duplicate`, `playlist_not_found`, `playlist_item_not_found`, `playlist_full`, `thumbnail_not_allowed`, `permission_denied` |
| YouTube and network | `busy` (another local process is making the same change), `quota_exceeded`, `rate_limited`, `network_error`, `youtube_unavailable`, `write_outcome_uncertain`, `youtube_api_error` |
| Outside this server | `unsupported_by_youtube` (YouTube doesn't allow it, for example renaming a caption track or scheduling an unlisted video) |

Every error includes an `action` telling the assistant (and you) what to do next.
