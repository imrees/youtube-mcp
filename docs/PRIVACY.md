# Privacy policy

**Applies to:** YouTubeMCP 1.0.0 and later (the MCP server, the installer and the optional Finder Quick Action)
· **Last updated:** 2026-11-26

YouTubeMCP is software that you run on your own computer. It is not a hosted service. This page explains what
data it handles, where that data goes, what it keeps on your computer, and how to remove it.

## Summary

- **The project collects nothing.** There is no telemetry, analytics, crash reporting, update check or account.
  The maintainers run no servers and never receive your data, credentials or videos.
- **You connect it to Google with your own OAuth client.** You create the Google Cloud project and OAuth client
  yourself ([SETUP.md](SETUP.md#2-create-a-google-cloud-project)), so access to your YouTube account is granted to
  *your* client, which is stored only on your computer.
- **Data leaves your computer in only three ways:** to Google's APIs when a tool runs; to your MCP client and AI
  assistant as tool results; and, during installation only, to the Python Package Index to download pinned
  packages.

## Google user data it accesses

| Permission (OAuth scope) | Requested | Used to |
|---|---|---|
| `youtube.upload` | always | upload videos |
| `youtube.force-ssl` | always | read and change your videos' details, thumbnails, captions and playlists; read channel information |
| `yt-analytics.readonly` | only if you opt in to analytics | read your channel and video statistics |
| `youtube.readonly` | only if you add it for analytics (`--youtube-readonly`) | an alternative read-only permission that some analytics queries accept |

The software uses these permissions only to perform the tool actions you ask for, and every change needs your
approval first ([how approval works](../README.md#how-approval-works)). Although `youtube.force-ssl` would allow
it, the software has no tools to delete videos, captions or playlists, or to read or manage comments.

## Where your data goes

| Recipient | What | When |
|---|---|---|
| **Google (YouTube Data API, YouTube Analytics API, Google sign-in)** | The video files, thumbnails, caption files and metadata you approve for upload or change; read requests for videos, playlists, captions, channel and analytics | Each time a tool runs, and when you sign in. Google's handling is covered by the [Google Privacy Policy](https://policies.google.com/privacy) and the [YouTube Terms of Service](https://www.youtube.com/t/terms). |
| **Your MCP client and AI assistant** | Tool inputs and results: video titles, descriptions, tags, caption text, playlist names, analytics figures, video IDs and links, and the local file paths and names you give it | Each time the assistant calls a tool. If your assistant uses a cloud-hosted model, this content is sent to that model's provider and handled under *its* privacy policy. OAuth tokens and client secrets are never included in tool results. |
| **Python Package Index (pypi.org)** | Ordinary download requests for the pinned, hash-checked packages (your IP address and pip's user agent are visible to it); nothing about you or your channel | Only while the installer builds its Python environment |

The optional **Finder Quick Action** also copies the uploaded video's link to your clipboard and shows macOS
notifications with the file name and upload progress. Nothing else is sent anywhere.

## What is stored on your computer

With the installer, your data lives in one folder:

- macOS: `~/Library/Application Support/youtube-mcp/`
- Linux (untested): `~/.local/share/youtube-mcp/` (or `$XDG_DATA_HOME/youtube-mcp/`)
- Manual installation: the project folder

| File | Contents | Kept |
|---|---|---|
| `client_secret.json` | Your OAuth client from Google Cloud | until you delete it |
| `token_youtube_mcp.json` | Your Google sign-in (refresh and access token), readable only by your user account (`0600`) | until you delete it or revoke access |
| `youtube_mcp.env` | Optional preferences such as timezone and region (no secrets) | until you delete it |
| `data/history.sqlite3` | Upload history (local file paths, sizes, SHA-256 checksums, titles, descriptions, tags, video IDs, errors) and an audit log of every change and approval-dialog answer | until you delete it; there is no automatic expiry |
| `data/quick_action.log` | Quick Action log (local file paths, video IDs, errors; secrets redacted) | rotated at 1 MB, at most 3 files |
| `data/staging/` | Temporary private copies of the video, thumbnail or caption file you approved, so the uploaded bytes match the approval | deleted after each write; leftovers are cleaned up automatically |

The data folder is readable only by your user account (`0700`). The separate app folder
(`~/Library/Application Support/youtube-mcp-app/`) contains only the program, its Python environment, an
installation log and a manifest, never credentials.

The server writes its diagnostic log to standard error, which your MCP client may save in its own log files
(Claude Desktop, for example, keeps them in `~/Library/Logs/Claude/`). Tokens and secrets are redacted from those
messages.

The OAuth token is protected by file permissions only. It is not encrypted or stored in the macOS Keychain; see
[SECURITY.md](SECURITY.md#limitations).

## Revoking Google access

Revoking access stops this software from reaching your YouTube account. Your videos, playlists and captions on
YouTube are not changed or deleted.

1. Open <https://myaccount.google.com/permissions> (Google Account → **Security** → **Your connections to
   third-party apps & services**) and sign in with the Google account you used to authorize.
2. Select your app. It is listed under the name you gave your OAuth consent screen.
3. Choose **Delete all connections** (on some versions of the page: **Remove access**) and confirm.
4. If you picked a Brand Account channel when signing in, check that account's connections page too.
5. Delete the now-useless token file, `token_youtube_mcp.json`, from the data folder.

To revoke every sign-in made with your OAuth client at once, delete the client in Google Cloud Console
(**Google Auth Platform → Clients**), or shut down the whole project (**IAM & Admin → Settings → Shut down**).

After revoking, every tool returns `auth_expired` or `not_authorized`. To use the software again, sign in once
more with `youtube-mcp-authorize`.

## Deleting your local data

- **Everything:** uninstall with
  `"$HOME/Library/Application Support/youtube-mcp-app/bin/youtube-mcp-uninstall" --delete-data`; you'll be asked to
  type `DELETE`. Without `--delete-data`, uninstalling removes the program but keeps your data folder.
  See [INSTALL.md](INSTALL.md#uninstall).
- **Manual installation:** follow [SETUP.md](SETUP.md#uninstalling).
- **Upload history only:** delete `data/history.sqlite3` while no upload is running. Note that the history is
  also what protects you against uploading the same file twice.

Revoke Google access as well (above). Deleting the token file on its own doesn't revoke the sign-in at Google.

## If other people use your OAuth client

Each user is expected to create their own Google Cloud project and OAuth client. If you let other people sign in
with yours, you are the developer of that app in Google's eyes and are responsible for meeting Google's
[API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy) and the
[YouTube API Services Terms of Service](https://developers.google.com/youtube/terms/api-services-terms-of-service)
for them.

## Changes and questions

Changes to this policy are recorded in [CHANGELOG.md](../CHANGELOG.md) and in this file's history. Questions can
be asked in the project's GitHub issues. Please don't post tokens, client secrets, logs that contain personal
details, or your upload history.
