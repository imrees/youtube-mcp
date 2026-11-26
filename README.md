# YouTubeMCP

A local [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server that lets an AI assistant upload
and manage videos on **your own** YouTube channel through the official YouTube Data API v3, with your approval
for every change.

- **Runs on your computer** over stdio. There is no hosted service, no browser automation, no subscription and
  no telemetry; it talks only to Google's APIs, using a Google Cloud project and OAuth client that you create.
  See the [privacy policy](docs/PRIVACY.md).
- **Works with any MCP client** that can start a local stdio server. It doesn't depend on any particular
  assistant, recording app or editor.
- **Safe by default.** Uploads are **unlisted** unless you ask otherwise. Every change is previewed first and
  needs your approval, and anything that would make content **public** also needs a native macOS "Allow Public"
  dialog that only you can answer.
- **Reliable.** Resumable background uploads with retries, protection against duplicate uploads, and every
  write checked afterwards by reading it back from YouTube.
- **Optional extras:** captions, playlists, scheduled publishing, read-only YouTube Analytics, and a macOS
  Finder Quick Action, **Upload & Share**, that uploads a video as unlisted and copies its link.

> **v1.0.0.** All 26 tools have been tested against a live channel. See [CHANGELOG.md](CHANGELOG.md).

## Quick start (macOS)

You don't need any prior experience with Python, Google Cloud, OAuth or MCP. Allow about 20 minutes.

1. **Install Python 3.12** from <https://www.python.org/downloads/> (or `brew install python@3.12`).
2. **Download** `youtube-mcp-1.0.0-source.zip` from the [Releases page](../../releases), unzip it, and
   **run the installer** from that folder, in Terminal:

   ```bash
   cd ~/Downloads/youtube-mcp-1.0.0-source
   ./install.sh
   ```

   It installs into your home folder (no administrator password), tests itself, and asks whether you also want
   the optional Finder Quick Action and, if Claude Desktop is installed, whether to configure it. Both default to no.
3. **Create your own Google OAuth client** (free; [step-by-step guide](docs/SETUP.md#2-create-a-google-cloud-project))
   and save it as `~/Library/Application Support/youtube-mcp/client_secret.json`.
4. **Sign in once:** `"$HOME/Library/Application Support/youtube-mcp-app/bin/youtube-mcp-authorize"`
5. **Connect your MCP client** using the JSON the installer printed, and restart the client.

Then ask your assistant something like *"Upload ~/Movies/demo.mp4 to YouTube as unlisted, titled 'Product
demo'"*. It shows you a preview, and nothing happens until you approve it.

**Upgrade:** run `./install.sh` from the new release; your credentials and history are kept. **Uninstall:**
`"$HOME/Library/Application Support/youtube-mcp-app/bin/youtube-mcp-uninstall"`; your data is kept unless you
ask. Details, options and other platforms: **[docs/INSTALL.md](docs/INSTALL.md)**. The installer is tested on
macOS (Apple silicon, Python 3.12); Linux is untested, and Windows isn't supported.

## What it can do

| Area | Tools |
|---|---|
| Videos | `upload_video`, `get_upload_progress`, `update_metadata`, `set_thumbnail`, `set_visibility`, `get_video_status`, `get_share_link`, `list_uploads`, `get_channel_info` |
| Captions | `upload_captions`, `list_captions`, `download_captions`, `update_captions` |
| Playlists | `list_playlists`, `create_playlist`, `update_playlist`, `list_playlist_items`, `add_to_playlist`, `remove_from_playlist` |
| Categories | `list_categories` |
| Scheduled publishing | `schedule_publish`, `get_publish_schedule`, `cancel_publish_schedule` |
| Analytics (optional, read-only) | `get_channel_analytics`, `get_video_analytics`, `get_top_videos` |

Every tool and parameter is documented in the **[tool reference](docs/TOOLS.md)**.

There are deliberately **no** tools to delete videos, captions or playlists, or to read or manage comments.
`remove_from_playlist` removes a playlist entry; the video itself is never deleted.

## How approval works

1. **Preview.** Every tool that changes something on YouTube first returns a preview and a single-use
   `confirmation_token`. The token expires after 10 minutes and is bound to the exact parameters, and for
   files to the file's contents.
2. **Approve.** Your assistant should show you the preview and wait for you to approve it. Only then does it
   call the tool again with the token.
3. **Public needs more.** Anything that would make content public also needs `confirm_public=true` *and* a
   click on **Allow Public** in a native macOS dialog. Cancel is the default; if the dialog can't be shown or
   times out, nothing is made public.
4. **Verify.** After each write, the server reads the result back from YouTube and tells you whether it was
   confirmed. A write is never sent twice.

This protects against mistakes. It is **not** a complete security boundary: see
[Security model and limitations](docs/SECURITY.md).

## YouTube API quota

Google gives every Cloud project a free daily quota, which resets at midnight Pacific Time. At the time of
writing, a project gets **100 video uploads a day** plus **10,000 units a day** for everything else. Typical
costs in units:

| Action | Units |
|---|---|
| Reading a video, playlist, channel or category list | 1 |
| Changing metadata or visibility, setting a thumbnail, any playlist change, scheduling | 50 |
| Listing a video's caption tracks | 50 |
| Uploading / replacing a caption track | 400 / 450 |
| Downloading a caption track (`download_captions`, `verify_content`) | 200 |
| Uploading a video | 1 call of the separate upload allowance |

Google changes these figures from time to time; the
[official quota table](https://developers.google.com/youtube/v3/determine_quota_cost) is authoritative.
Analytics uses the separate YouTube Analytics API and its own quota.

## Known limitations

- **macOS only is tested.** Tested on macOS 26 on Apple silicon with Python 3.12. Intel Macs and Python
  3.10, 3.11 and 3.13 are accepted but untested. Linux is untested and needs an explicit installer flag. There, and
  in any session without a macOS desktop, the public-approval dialog, notifications, clipboard and Quick Action
  are unavailable, so nothing can be made public. Windows isn't supported.
- **Unaudited API projects.** YouTube locks videos uploaded by Cloud projects that haven't passed its
  [API compliance audit](https://support.google.com/youtube/contact/yt_api_form) to **private**. The server
  detects and reports this; you can change the visibility in YouTube Studio.
- **Delayed consistency.** YouTube can take a while to show a change. If it isn't visible after about 15
  seconds, the tool returns a "verification pending" warning instead of retrying the write.
- **Things the API can't confirm:** which thumbnail image is served, and caption timings (YouTube re-times
  cues; the text can be compared with `verify_content`).
- **Reported totals.** YouTube's reported totals can be larger than the items it actually returns. On one
  channel, `playlists.list` reported 22 playlists but returned 15 across all pages. The server reports both
  numbers and never invents missing items. See [Pagination](docs/BEHAVIOUR.md#pagination).
- **Testing-mode tokens expire weekly.** While your OAuth app is in Google's *Testing* mode, sign-ins expire
  after 7 days. See [Troubleshooting](docs/SETUP.md#troubleshooting).
- **Token storage.** The OAuth token is protected by file permissions only; it isn't encrypted or kept in the
  macOS Keychain.

## Documentation

| Document | Contents |
|---|---|
| [docs/INSTALL.md](docs/INSTALL.md) | Installer: supported platforms, install, upgrade, uninstall, options, troubleshooting |
| [docs/SETUP.md](docs/SETUP.md) | Google Cloud and OAuth setup step by step, manual installation, settings, analytics, troubleshooting |
| [docs/TOOLS.md](docs/TOOLS.md) | Every tool and parameter |
| [docs/PRIVACY.md](docs/PRIVACY.md) | Privacy policy: what data goes where, what is stored locally and for how long, revoking Google access, deleting your data |
| [docs/SECURITY.md](docs/SECURITY.md) | Approval model, native dialog, what is stored locally, limitations, reporting a vulnerability |
| [docs/BEHAVIOUR.md](docs/BEHAVIOUR.md) | How uploads, duplicate protection, verification, pagination, captions, playlists, scheduling and analytics behave; error codes |
| [docs/QUICK_ACTION.md](docs/QUICK_ACTION.md) | The optional macOS Finder Quick Action, "Upload & Share" |
| [docs/INSTALL_TEST_REPORT.md](docs/INSTALL_TEST_REPORT.md) | Clean-installation test results |
| [CHANGELOG.md](CHANGELOG.md) | Release history |

## Development

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

The tests use an in-memory fake of the YouTube API (`tests/fakes.py`) that mimics real behaviour, including
delayed consistency, and never touch the network or real credentials. They also drive the server end to end
through the MCP protocol, both in-process and as a real stdio subprocess. After changing a tool's definition,
run `.venv/bin/python scripts/generate_tool_reference.py` to regenerate `docs/TOOLS.md`; a test fails if it is
out of date.

```
youtube_mcp/
  server.py         MCP tool definitions (stdio)
  service.py        upload, metadata, visibility and status logic
  captions.py, playlists.py, categories.py, scheduling.py, analytics.py
  approval.py, confirmations.py   two-step approval and single-use tokens
  native_confirm.py               macOS "Allow Public" dialog
  verify.py         read-back verification after writes
  uploads.py        background resumable uploads
  staging.py        verified snapshots of approved files
  history.py        SQLite upload history and audit log
  youtube.py        YouTube API wrapper and input validation
  auth.py, authorize.py, config.py, errors.py, macos.py, quick_action.py
install.sh, install.py, uninstall.sh   installer (standard library only)
youtube_mcp_server.py   launcher for MCP clients (manual installation)
authorize.py            launcher for the sign-in command (manual installation)
upload_and_share.py     launcher for the Finder Quick Action (manual installation)
scripts/                tool reference generator, dependency hash lock, release builder
```

## Licence

[MIT](LICENSE). This project is not affiliated with or endorsed by Google or YouTube. YouTube is a trademark
of Google LLC. Your use of the YouTube API is subject to the
[YouTube API Services Terms of Service](https://developers.google.com/youtube/terms/api-services-terms-of-service).
