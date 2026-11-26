# Setup guide

This guide assumes no previous experience with Google Cloud, OAuth or MCP. It takes about 20 minutes.

**Contents:** [What you need](#what-you-need) · [1. Install](#1-install) ·
[2. Google Cloud project](#2-create-a-google-cloud-project) · [3. Sign in](#3-sign-in-authorize) ·
[4. Connect your MCP client](#4-connect-your-mcp-client) · [5. Try it](#5-try-it) · [Settings](#settings) ·
[Analytics](#optional-youtube-analytics) · [Troubleshooting](#troubleshooting) · [Updating](#updating) ·
[Uninstalling](#uninstalling)

## A few terms

- **MCP (Model Context Protocol)** is a standard way for AI assistants to use tools. An *MCP client* (a desktop
  assistant app, a coding assistant, an IDE) starts this server on your computer and talks to it.
- **Google Cloud project.** Google only lets programs use the YouTube API through a "project" that you own in
  Google Cloud Console. It's free for this use; no billing account is needed.
- **OAuth** is how you let the program act on your channel without giving it your password. You sign in once in
  your browser, and Google gives the program a *token* that it stores on your computer.

## What you need

- A Mac. Only macOS (Apple silicon) has been tested. Linux is untested; there, the approval dialog for public
  actions, notifications and the Finder Quick Action are unavailable, so nothing can be made public. Windows
  isn't supported. See [Supported platforms](INSTALL.md#supported-platforms).
- **Python 3.10 to 3.13** (3.12 is the tested version). Check with `python3 --version` in Terminal. On macOS you can install it from
  [python.org](https://www.python.org/downloads/) or with Homebrew (`brew install python@3.12`).
- A Google account that owns (or manages) a YouTube channel.
- An MCP client that can run local "stdio" servers.

## 1. Install

**Recommended:** use the installer, which does all of this step for you: see [INSTALL.md](INSTALL.md). With the
installer, your files live in `~/Library/Application Support/youtube-mcp` (save `client_secret.json` there in step
2.4), sign-in is `"$HOME/Library/Application Support/youtube-mcp-app/bin/youtube-mcp-authorize"` (step 3), and it
prints the client configuration for step 4. Then continue with [step 2](#2-create-a-google-cloud-project).

**Manual installation (developers).** Put the project folder somewhere permanent; the configuration refers to it by its full path. The examples use
`~/youtube-mcp`. In Terminal:

```bash
cd ~/youtube-mcp
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

This creates a private Python environment in `.venv` and installs the pinned dependencies. For an exact,
fully pinned install use `requirements.lock.txt` instead.

To check: `.venv/bin/python -c "import youtube_mcp; print(youtube_mcp.__version__)"` prints the version.

## 2. Create a Google Cloud project

Google's console changes its layout from time to time. If a name below doesn't match exactly, look for the
closest equivalent.

### 2.1 Create the project

1. Open [Google Cloud Console](https://console.cloud.google.com/) and sign in. Any Google account works; it
   doesn't have to be the channel's account.
2. Click the project picker at the top of the page, then **New project**. Give it any name (for example
   "YouTubeMCP") and click **Create**. Make sure the new project is selected in the picker.

### 2.2 Turn on the YouTube Data API

1. Go to **APIs & Services > Library**.
2. Search for **YouTube Data API v3**, open it and click **Enable**.

### 2.3 Configure the consent screen

This is the screen Google shows when you sign in.

1. Go to **Google Auth Platform** (older consoles: **APIs & Services > OAuth consent screen**) and click
   **Get started**.
2. **App information:** enter an app name (for example "YouTubeMCP") and your email address as the support email.
3. **Audience:** choose **External** (unless you have a Google Workspace organisation and only use accounts in it,
   in which case **Internal** is fine).
4. **Contact information:** your email address. Accept the policy and click **Create**.
5. Open **Audience** and, under **Test users**, click **Add users** and add the Google account that owns your
   channel. While the app is in *Testing* mode, only test users can sign in.

You don't need to add scopes here; the sign-in command requests them. You also don't need Google's app
verification for personal use: you'll see an "unverified app" warning when you sign in, which you can pass
because you're the developer.

**Testing vs. production.** In *Testing* mode Google expires your sign-in after **7 days**, so you'd need to
re-run the sign-in weekly. To avoid that, open **Audience** and click **Publish app**. For a personal app
you don't have to complete verification; the "unverified app" warning just keeps appearing at sign-in.

### 2.4 Create the OAuth client

1. Go to **Google Auth Platform > Clients** (older consoles: **APIs & Services > Credentials > Create credentials >
   OAuth client ID**) and click **Create client**.
2. **Application type:** **Desktop app**. Name it anything and click **Create**.
3. Click **Download JSON** (shown after creation, or later from the client's page via the download icon).
4. Save the file as `client_secret.json` in the project folder (`~/youtube-mcp/client_secret.json`).

`client_secret.json` identifies your app to Google. Keep it private: don't share it or commit it to Git
(the included `.gitignore` already excludes it).

## 3. Sign in (authorize)

In Terminal:

```bash
cd ~/youtube-mcp
.venv/bin/python authorize.py
```

Your browser opens Google's sign-in page.

1. Choose the account that owns the channel. If it manages several channels (brand accounts), pick the channel
   you want the server to use.
2. If you see **"Google hasn't verified this app"**, click **Advanced**, then **Go to … (unsafe)**. This is your
   own app.
3. **Tick every permission checkbox** and click **Continue**.

The server asks for two permissions:

| Permission (scope) | Why |
|---|---|
| `youtube.upload` | upload videos |
| `youtube.force-ssl` | read and change your videos, thumbnails, captions and playlists; read channel info |

The token is saved as `token_youtube_mcp.json` in the project folder, readable only by your user account.

Check the result at any time (no secrets are printed):

```bash
.venv/bin/python authorize.py --status
```

On a computer without a browser, `authorize.py --no-browser` prints the sign-in address to open elsewhere.

The server itself never opens a browser. If the sign-in is missing or has expired, every tool returns a
`not_authorized` or `auth_expired` error that contains the command to run.

## 4. Connect your MCP client

Every MCP client needs the same two things: the **command** (the Python inside `.venv`) and the **argument**
(`youtube_mcp_server.py`), both as full paths. Find them with:

```bash
cd ~/youtube-mcp && echo "$PWD/.venv/bin/python" && echo "$PWD/youtube_mcp_server.py"
```

Most desktop clients use a JSON configuration with an `mcpServers` section. Add an entry, keeping any servers
that are already there:

```json
{
  "mcpServers": {
    "YouTube": {
      "command": "/Users/you/youtube-mcp/.venv/bin/python",
      "args": ["/Users/you/youtube-mcp/youtube_mcp_server.py"]
    }
  }
}
```

Replace `/Users/you/youtube-mcp` with your own path, then **fully quit and restart** the client. Clients that
add servers from the command line take the same command and argument. Check your client's documentation for
where its configuration lives.

**Logs.** The server writes logs to stderr, which most clients save to a log file; stdout carries only MCP
messages. Set `YOUTUBE_MCP_LOG_LEVEL=DEBUG` for more detail. Logs never contain tokens or secrets.

**Recommended:** if your client lets you choose per tool whether to "always allow" or "ask every time", keep the
tools that change YouTube on *ask every time* as an extra layer.

## 5. Try it

Ask your assistant:

- *"Which YouTube channel are you connected to?"* (`get_channel_info`, read-only)
- *"List my five most recent uploads."* (`list_uploads`)
- *"Upload /Users/you/Movies/test.mp4 as a private video called 'Test upload'."* You'll see a preview with a
  confirmation token; approve it and the upload runs in the background.

## Settings

Everything is optional. Settings can be environment variables (for example in your MCP client's `env` section
for the server) or lines in an optional file named **`youtube_mcp.env`** in the project folder. The file is read
by both the server and the Finder Quick Action, so it's the easiest place for preferences. Environment variables
take priority. To start, copy `youtube_mcp.env.example` to `youtube_mcp.env` and edit it.

```
# youtube_mcp.env (preferences only; never put secrets here)
YOUTUBE_MCP_TIMEZONE=Europe/London
YOUTUBE_MCP_REGION=GB
YOUTUBE_MCP_DEFAULT_CATEGORY=28
```

| Setting | Default | Meaning |
|---|---|---|
| `YOUTUBE_MCP_TIMEZONE` | your computer's timezone (UTC if unknown) | timezone for `schedule_publish` times written without an offset |
| `YOUTUBE_MCP_REGION` | `US` | region for `list_categories` and category checks |
| `YOUTUBE_MCP_DEFAULT_CATEGORY` | `22` (People & Blogs) | category for uploads that don't specify one; `list_categories` shows the IDs |
| `YOUTUBE_MCP_CONFIRM_TIMEOUT` | `60` | seconds the "Allow Public" dialog waits (10–300; it can't disable the dialog) |
| `YOUTUBE_MCP_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `YOUTUBE_MCP_CLIENT_SECRET` | `<project>/client_secret.json` | OAuth client file |
| `YOUTUBE_MCP_TOKEN` | `<project>/token_youtube_mcp.json` | where the sign-in token is stored |
| `YOUTUBE_MCP_DATA_DIR` | `<project>/data` | upload history, logs and temporary snapshots |
| `YOUTUBE_MCP_HOME` | the project folder (see below) | base folder for the three paths above and `youtube_mcp.env` (environment variable only) |

The default visibility, **unlisted**, is fixed in code and can't be changed by a setting.

**Where files are kept.** When you run the server from a project folder, as in this guide, credentials, settings and
data live in that folder. If the package is instead installed into a Python environment (for example with
`pip install .`), it never stores anything inside the installation; it uses a per-user folder:
`~/Library/Application Support/youtube-mcp` on macOS, or `$XDG_DATA_HOME/youtube-mcp` (usually
`~/.local/share/youtube-mcp`) on Linux. Put `client_secret.json` there, and run the sign-in with
`python -m youtube_mcp.authorize`. To keep using an existing project folder instead, set `YOUTUBE_MCP_HOME` to it.

## Optional: YouTube Analytics

The three analytics tools need one extra Google API and one extra **read-only** permission. Everything else
works without them. No revenue or monetary permission is ever requested.

1. **Enable the API.** In Cloud Console, in the same project, go to **APIs & Services > Library**, search for
   **YouTube Analytics API** and click **Enable**. It's separate from the YouTube Data API.
2. **Add the permission to the consent screen.** Open **Google Auth Platform > Data Access** (older consoles:
   **OAuth consent screen > Scopes**), click **Add or remove scopes**, tick
   `https://www.googleapis.com/auth/yt-analytics.readonly` ("View YouTube Analytics reports for your YouTube
   content") and save.
3. **Sign in again with analytics:** `.venv/bin/python authorize.py --analytics`, and tick every box. If the
   analytics box isn't granted, or the sign-in fails, your existing token is left untouched.
4. **Check:** `.venv/bin/python authorize.py --status` shows `Analytics (optional): enabled`. Restart your MCP
   client so the server loads the new token.

If analytics calls then fail with `insufficient_scope`, run `authorize.py --analytics --youtube-readonly`
(Google's `reports.query` documentation also accepts the read-only `youtube.readonly` scope). Permissions you
already granted are kept whenever you sign in again.

## Troubleshooting

| Problem | What to do |
|---|---|
| `not_authorized` | Run `.venv/bin/python authorize.py`. |
| `auth_expired` about every 7 days | Your app is in *Testing* mode. Sign in again, or publish the app (see [2.3](#23-configure-the-consent-screen)). |
| `insufficient_scope` | You didn't tick every box at sign-in. Run `authorize.py` again and tick them all. |
| `api_not_enabled` / `accessNotConfigured` | Enable the API (step 2.2, or Analytics step 1) in the **same** project as your OAuth client. Allow a few minutes. |
| "Access blocked: app has not completed the Google verification process" | Add your account as a **test user** (step 2.3, item 5). |
| The server doesn't appear in the client | Check both paths are absolute and correct, the JSON is valid, and you fully restarted the client. Running the command in Terminal should print a "starting (stdio)" log line and wait. |
| Uploads end up **private** (`visibility_restricted`) | Your Cloud project hasn't passed YouTube's [API compliance audit](https://support.google.com/youtube/contact/yt_api_form). Change the visibility in YouTube Studio, or apply for the audit. |
| `thumbnail_not_allowed` / videos over 15 minutes rejected | Verify your channel at <https://www.youtube.com/verify>. `get_channel_info` shows `long_uploads_status`. |
| `quota_exceeded` | Daily quota used up; it resets at midnight Pacific Time. See [quota](../README.md#youtube-api-quota). |
| `native_confirmation_unavailable` | The "Allow Public" dialog couldn't be shown (no macOS desktop session). Public actions are refused; use YouTube Studio. |
| No notifications appear (macOS) | Notifications are sent through `osascript`; enable notifications for **Script Editor** in **System Settings > Notifications**. |
| `pip` or `.venv/bin/...` stops working after moving the folder | Use `.venv/bin/python -m pip`, update the paths in your client configuration, and re-run `upload_and_share.py install` if you use the Quick Action. If needed, delete `.venv` and repeat step 1. |

Error codes are listed in [BEHAVIOUR.md](BEHAVIOUR.md#errors).

## Updating

Replace the code files (keeping `client_secret.json`, `token_youtube_mcp.json`, `youtube_mcp.env` and `data/`),
then run `.venv/bin/python -m pip install -r requirements.txt` and restart your MCP client. If you use the Quick
Action, run `.venv/bin/python upload_and_share.py install` again.

## Uninstalling

1. **Quick Action** (if installed): `.venv/bin/python upload_and_share.py uninstall`.
2. **MCP client:** remove the `YouTube` entry from its configuration and restart it.
3. **Google access:** open <https://myaccount.google.com/permissions>, select your app and choose **Delete all
   connections** (or **Remove access**). Step by step: [Revoking Google access](PRIVACY.md#revoking-google-access).
4. **Files:** delete the project folder. This removes the token, your OAuth client file, the upload history in
   `data/` and the virtual environment. Nothing is installed elsewhere, apart from the Quick Action in step 1.
5. **Optional:** delete the Cloud project in Cloud Console (**IAM & Admin > Settings > Shut down**).

Your videos on YouTube are not affected by any of these steps.
