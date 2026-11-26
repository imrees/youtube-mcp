# Security model and limitations

The server acts on your YouTube channel on behalf of an AI assistant. It protects the channel with several
**independent layers**. Each one has limits, which are listed honestly below.

## Layers of protection

1. **Unlisted by default.** New uploads are unlisted unless you ask otherwise. The default is fixed in code and
   can't be changed by a setting.

2. **Every change is two-step.** Every tool that changes something on YouTube first returns a *preview* and a
   `confirmation_token`: `upload_video`, `update_metadata`, `set_thumbnail`, `set_visibility` (to any visibility),
   and every caption, playlist and scheduling change. The assistant is instructed to show you the preview and wait
   for your approval before calling again with the token. Tokens:
   - are single-use, expire after 10 minutes and are lost when the server restarts;
   - are bound to the tool and its exact parameters (including options such as `verify_content`), and for files
     to the file's SHA-256. If anything changes after the preview, the token is rejected.

3. **Public actions need a native macOS confirmation.** Any action that would make content public also requires
   `confirm_public=true` *and* a native macOS dialog that only you can answer:
   - a public upload, or changing a video to public;
   - `schedule_publish` (a schedule always ends in public);
   - creating a public playlist, or making a playlist public;
   - adding an unlisted video to a public playlist (the playlist's viewers can then see it);
   - changing a private video to unlisted when it's in one of the channel's public playlists;
   - publishing a non-draft caption track on a public video.

   Making a playlist public also lists the unlisted videos it contains, since they become visible to its viewers.
   What was found is bound to the approval, so a change after the preview requires a fresh one.

   The dialog shows the action, the channel, the video or playlist title and, for schedules, the publication time
   in your timezone and in UTC. **Cancel is the default button.** The dialog **fails closed**: if you click Cancel,
   if it times out (60 s by default; `YOUTUBE_MCP_CONFIRM_TIMEOUT` sets 10–300 s), or if it can't be shown at all
   (no macOS desktop session, `osascript` unavailable, not macOS), nothing is made public. Cancelling also uses up
   the token, so the assistant must ask you again from the start. No tool parameter, environment variable or
   setting skips the dialog. Every answer is recorded in the local audit log.

4. **The approved file is the uploaded file.** When you approve an upload, a thumbnail or a caption file, the file is
   snapshotted into a private directory (`data/staging`, mode `0700`). On APFS this is an instant copy-on-write clone using no extra space;
   otherwise it's a full copy after a free-space check. The upload is refused (`file_changed_since_approval`)
   unless the snapshot's hash matches the approved file, and only the snapshot is sent (and, for `verify_content`,
   compared). It is deleted when the write completes or fails, and leftovers from crashes are removed at the next start. Your original file is
   only ever read: never moved, renamed, modified or deleted.

5. **Writes are sent once and verified.** The Google client library's automatic retries are switched off for
   writes, so a change YouTube already accepted is never sent again. Each write is then checked by reading it back
   (see [Verification after writes](BEHAVIOUR.md#verification-after-writes)). If YouTube reports a new upload as
   public when you asked for unlisted or private, the server changes it back immediately and only reports success
   once a read-back confirms it; otherwise you get an urgent warning with the YouTube Studio link.

6. **No accidental duplicates.** The duplicate check and the new history record are one SQLite transaction shared
   by every process (several server instances and the Finder Quick Action), and `add_to_playlist` is serialised the
   same way. An upload whose outcome is uncertain is never repeated without your explicit decision (see
   [Unclear outcomes](BEHAVIOUR.md#unclear-outcomes)).

7. **No destructive tools.** There are no tools to delete videos, captions or playlists, or to read or manage
   comments. `remove_from_playlist` removes only a playlist entry.

8. **Credentials stay local.** No tool returns a token or secret. Logs and error messages, including tracebacks,
   pass through a redaction filter. The token, history and log files are `0600` and the data directory is `0700`.
   The server reads only its own `client_secret.json` and `token_youtube_mcp.json`, never any other application's
   credentials, and talks only to Google's APIs. The only permissions requested are `youtube.upload` and
   `youtube.force-ssl`, plus the read-only `yt-analytics.readonly` (and, if needed, `youtube.readonly`) if you opt
   in to analytics.

## Limitations

- **The preview and token protect against mistakes, not against a hostile model.** The assistant receives the
  token and sets `confirm_public` itself. They stop an assistant from acting by accident or on a misunderstanding,
  not one that is deliberately misbehaving (for example after a prompt injection). The native dialog exists for
  that case, but only for public exposure: non-public changes (metadata, thumbnails, private/unlisted visibility,
  private playlists, captions on non-public videos) rely on the two-step flow and on your MCP client's own
  per-tool approval prompts.
- **The native dialog is an additional safeguard, not an absolute boundary.** Anything that can control your
  Mac's screen, keyboard or mouse could click "Allow Public": an AI agent with computer-use or accessibility
  access, remote-control software, or malware running as your user. Don't let such tools run while a public
  action is pending.
- **The OAuth token is protected only by file permissions.** Anyone who can read your user account's files can
  use it. It isn't encrypted or stored in the macOS Keychain.
- **Indirect exposure is checked on this channel only.** Playlists on other channels can't be listed through the
  API, and changes made outside this server (YouTube Studio, other apps) aren't coordinated with it.
- **Untrusted content reaches the assistant.** Video titles, descriptions, caption text and playlist names are
  returned to the assistant as data. Text written by others could try to instruct it; the approval steps above
  are the defence.
- **The Finder Quick Action treats your click as approval.** It can only upload **unlisted** videos, so it never
  shows the dialog. See [QUICK_ACTION.md](QUICK_ACTION.md).

## What is stored on your computer

| File | Contents | Permissions |
|---|---|---|
| `client_secret.json` | Your OAuth client (from Google Cloud) | keep private |
| `token_youtube_mcp.json` | The OAuth refresh/access token | `0600` |
| `youtube_mcp.env` | Optional preferences (no secrets) | – |
| `data/history.sqlite3` | Upload history (file paths, sizes, SHA-256, metadata, video IDs, errors) and an append-only audit log of every change and approval-dialog answer | `0600`, directory `0700` |
| `data/quick_action.log` | Quick Action run log (redacted) | `0600` |
| `data/staging/` | Temporary snapshots of approved videos, thumbnails and caption files, deleted after each write | `0700` |
| `data/lock-*.lock` | Empty lock files that coordinate local processes | `0600` |

None of these files should be shared or committed to version control; the included `.gitignore` excludes them.
Nothing is sent anywhere except to Google's APIs (and, as tool results, to your MCP client). Retention, deletion
and revoking Google access: [PRIVACY.md](PRIVACY.md).

## Reporting a vulnerability

Please report security problems privately, not in a public issue: on GitHub, open the repository's **Security**
tab and choose **Report a vulnerability**. Include steps to reproduce. Don't include your token, client secret or
other credentials in the report.
