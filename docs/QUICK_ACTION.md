# Finder Quick Action: "Upload & Share" (optional, macOS)

Right-click a video in Finder and choose **Quick Actions > Upload & Share**. The video is uploaded to your channel
as **unlisted** and its `youtu.be` link is copied to the clipboard.

It doesn't need an MCP client or any AI model to be running. It's an Automator Quick Action
(`~/Library/Services/Upload & Share.workflow`) that runs `upload_and_share.py`, a thin front end over the same
service code, OAuth token (`token_youtube_mcp.json`), settings and upload history (`data/history.sqlite3`) as the
MCP server. Complete [setup steps 1–3](SETUP.md) first; step 4 (connecting an MCP client) isn't needed.

## Install

**With the installer:** run `./install.sh --with-quick-action` (or answer "y" when it asks). No keyboard shortcut is
assigned; you can add one yourself in System Settings. The uninstaller removes it again.

**Manual installation (project folder):**

```bash
cd ~/youtube-mcp
.venv/bin/python upload_and_share.py install   # writes ~/Library/Services/Upload & Share.workflow
.venv/bin/python upload_and_share.py status    # shows install state and checks authorization
```

If **Upload & Share** doesn't appear in Finder's menu, enable it in **System Settings > Keyboard > Keyboard
Shortcuts > Services > Files and Folders** (on some macOS versions, **General > Login Items & Extensions >
Finder**), or right-click any file and choose **Quick Actions > Customize…**. You can also give it a keyboard
shortcut there.

The workflow stores absolute paths. If you move the project or rebuild `.venv`, run `install` again.

## Use

1. In Finder, select one or more video files, right-click and choose **Quick Actions > Upload & Share**.
   Choosing the action is your approval to upload; no second confirmation is shown.
2. A notification appears when the upload starts, then at 25/50/75% (at most one every 8 seconds). A spinning
   gear in the menu bar shows that the action is running; click its stop button to cancel.
3. When the upload finishes, YouTube is checked for the video's **actual visibility**, the link is copied to the
   clipboard, and a notification shows the verified visibility. With several files, all links are copied, one
   per line.

## Behaviour

- **Visibility** is always unlisted; the Quick Action can't upload publicly. If YouTube reports the video as
  public anyway, it's switched back to unlisted. If YouTube forces it to private (an unaudited Cloud project), the
  notification warns you.
- **Title** is the file name without its extension, with `<` and `>` removed, cut to 100 characters. The category
  is your configured default (`YOUTUBE_MCP_DEFAULT_CATEGORY`).
- **Duplicates.** If the exact same file (by SHA-256) was already uploaded, by the Quick Action or through the MCP
  server, nothing is uploaded; the existing link is copied instead, with its current visibility checked on
  YouTube. If that video has since been deleted from YouTube, you're told so and nothing is uploaded.
- **Unclear earlier attempts.** If an earlier attempt for the same file was cut off after data was sent (for
  example the connection dropped), YouTube may already have the video. If the channel's recent uploads can't settle
  it, nothing is uploaded: the notification asks you to check YouTube Studio and gives a Terminal command, with
  `--allow-duplicate`, to upload it anyway once you've confirmed it isn't there. The Finder action itself never
  adds that option.
- **One at a time.** Overlapping runs are queued.
- **Formats.** It appears for movie files (`public.movie`: .mp4, .mov, .m4v, .avi, .mpg, .mts, .webm, .mkv…), and
  each file is then checked against the server's list of supported extensions.
- **Your original file** is only ever read. It is never moved, renamed, modified or deleted.
- **Nothing runs automatically.** No folder is watched (Downloads included) and nothing runs in the background.
  Only files you select are uploaded.
- **Failures** (expired or revoked sign-in, no internet, quota, unsupported file) show a notification saying what
  to do. For sign-in problems, run `.venv/bin/python authorize.py`. Network errors are retried with backoff. A failure
  before anything was sent, or a refusal from YouTube, never blocks a retry; see "Unclear earlier attempts" above.

Details of each run are written to `data/quick_action.log`, with credentials redacted.

**First run:** macOS may ask to allow access to the folder the video is in (for example Documents).
Notifications are sent through `osascript`; if none appear, enable notifications for **Script Editor** in
**System Settings > Notifications**.

From Terminal, the same command uploads a file: `.venv/bin/python upload_and_share.py upload /path/to/video.mp4`
(add `--allow-duplicate` before the file only after checking YouTube Studio, as described above).

## Uninstall

```bash
.venv/bin/python upload_and_share.py uninstall
```

This removes only the workflow bundle, and refuses to remove one that this installer didn't create. Your upload
history, sign-in and the MCP server are unchanged. You can also delete `~/Library/Services/Upload & Share.workflow`
in Finder.
