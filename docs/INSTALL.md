# Installing, upgrading and uninstalling

The installer sets everything up in your home folder. It never needs `sudo` or an administrator password, never
asks for your Google password, and never ships anyone else's Google credentials: you create your own (step 3).

**Contents:** [Supported platforms](#supported-platforms) · [Install](#install) · [Where things go](#where-things-go) ·
[Connect an MCP client](#connect-an-mcp-client) · [Sign in](#sign-in) · [Upgrade](#upgrade) ·
[Uninstall](#uninstall) · [Options](#installer-options) · [Troubleshooting](#troubleshooting) ·
[Manual installation](#manual-installation-for-developers)

## Supported platforms

| Platform | Status |
|---|---|
| macOS 26 on Apple silicon (arm64), Python 3.12 | **Tested** (see [INSTALL_TEST_REPORT.md](INSTALL_TEST_REPORT.md)) |
| macOS on Intel (x86_64), or Python 3.10, 3.11, 3.13 | Accepted by the installer but **not tested**. Pre-built packages exist for these, so it is expected to work. |
| Linux | **Untested.** The installer refuses unless you pass `--allow-untested-platform`. Even then, public actions are always refused (the "Allow Public" confirmation dialog needs macOS), and the Quick Action, notifications and clipboard are unavailable. |
| Windows | Not supported. |

**Public actions need macOS.** Anything that would make content public (a public upload, switching a video to
public, scheduling, public playlists...) requires you to click "Allow Public" in a native macOS dialog. Where that
dialog can't be shown (Linux, a remote or headless session, no logged-in desktop), these actions **fail closed**:
they are refused, and you can make the change in YouTube Studio instead. Private and unlisted work everywhere.

## Install

1. **Python.** You need Python 3.10–3.13 (3.12 recommended). macOS's built-in `/usr/bin/python3` is 3.9 and too
   old. Install Python 3.12 from <https://www.python.org/downloads/> (the macOS installer), or with Homebrew:
   `brew install python@3.12`. The installer looks for it automatically.
2. **Get the source.** Download and unzip the release, and check the archive against its published SHA-256 if one
   was provided: `shasum -a 256 youtube-mcp-*.zip`.
3. **Run the installer.** In Terminal:

   ```bash
   cd ~/Downloads/youtube-mcp-1.0.0-source      # wherever you unzipped it
   ./install.sh
   ```

   It asks two yes/no questions, both defaulting to **no**: whether to install the optional Finder Quick Action,
   and (if Claude Desktop is installed) whether to add the server to its configuration. To answer in advance:

   ```bash
   ./install.sh --non-interactive                       # server only
   ./install.sh --with-quick-action                     # server + Finder "Upload & Share"
   ./install.sh --configure-claude-desktop              # also add it to Claude Desktop (backed up first)
   ```

What the installer does:

1. checks your macOS version, processor type and Python version;
2. verifies every source file against the release's `SHA256SUMS`;
3. creates a private Python environment and installs the exact dependency versions from
   `requirements.lock.txt`. Each download is checked against its recorded SHA-256 hash, and only pre-built
   packages are accepted, so no third-party build scripts run. Downloads come only from the official Python
   Package Index (pypi.org);
4. copies the app into the app folder and **tests it**: it starts the server and checks that all 26 tools are
   listed;
5. switches to the new version only after all of that succeeded.

The source folder you ran it from isn't needed afterwards; you can delete it.

## Where things go

| Folder (macOS) | Contents | On upgrade | On uninstall |
|---|---|---|---|
| `~/Library/Application Support/youtube-mcp-app` | the app, its Python environment, launchers in `bin/`, `install.log` | replaced | removed |
| `~/Library/Application Support/youtube-mcp` | **your** OAuth client, sign-in token, `youtube_mcp.env` settings, upload history, logs | never touched | **kept** (unless you ask) |

The data folder is readable only by you (`0700`). The installer only ever creates it and adds
`youtube_mcp.env.example`; it never overwrites anything in it. Credentials are never stored in the app folder or in
the Python environment. (On Linux the folders are `~/.local/share/youtube-mcp-app` and `~/.local/share/youtube-mcp`,
or under `$XDG_DATA_HOME`.)

To use a different data folder, set `YOUTUBE_MCP_HOME` in your MCP client's configuration (`env` section).

## Connect an MCP client

At the end, the installer prints a JSON snippet with the exact paths for your computer:

```json
{
  "mcpServers": {
    "YouTube": {
      "command": "/Users/you/Library/Application Support/youtube-mcp-app/venv/bin/python",
      "args": ["-I", "-m", "youtube_mcp"]
    }
  }
}
```

Add that entry to your MCP client's configuration (keeping any other servers), then fully restart the client. Print
it again at any time with `./install.sh --print-client-config`. (`-I` runs Python in isolated mode, so nothing in
the client's working folder can replace the installed code.)

**Claude Desktop** can be configured automatically with `--configure-claude-desktop`. The installer:

- makes a timestamped backup first (`claude_desktop_config.json.backup-YYYYMMDD-HHMMSS`, readable only by you);
- adds or updates only its own `YouTube` entry, keeping every other server and setting as it was, and never
  printing them;
- leaves the file untouched if it isn't valid JSON, or if a `YouTube` entry already exists that points somewhere
  else (for example a manual installation); remove or rename that entry first if you want to replace it.

Other clients: use the snippet above in their own configuration format. Nothing in the server depends on a
particular client.

## Sign in

1. Create your own Google Cloud project and **Desktop app** OAuth client, as described in
   [SETUP.md, step 2](SETUP.md#2-create-a-google-cloud-project). This project never includes anyone's credentials.
2. Save the downloaded file as `~/Library/Application Support/youtube-mcp/client_secret.json`.
3. Run:

   ```bash
   "$HOME/Library/Application Support/youtube-mcp-app/bin/youtube-mcp-authorize"
   ```

   and finish the sign-in in your browser (tick every permission). Check the result with the same command plus
   `--status`; it never prints secrets.

Until you've signed in, every tool returns `not_authorized` with the exact command to run.

## Upgrade

Download the new release, unzip it, and run `./install.sh` again. The installer:

- keeps your data folder exactly as it is;
- reuses the Python environment if the dependency set hasn't changed, and builds a fresh one if it has;
- tests the new version before switching to it; if anything fails, the previous version stays active;
- refuses to install an older version over a newer one unless you pass `--allow-downgrade`;
- keeps the Quick Action and MCP client entry pointing at the right place (the paths don't change).

Running the installer again with the same release is safe: it simply re-checks and re-tests the installation.
If an installation was interrupted (power loss, Ctrl-C), just run it again: it detects and cleans up the
unfinished step.

## Uninstall

```bash
"$HOME/Library/Application Support/youtube-mcp-app/bin/youtube-mcp-uninstall"
```

(or `./uninstall.sh` from a release folder). It removes:

- the app folder;
- the Finder Quick Action, if this installation created it (a Quick Action from another copy is left alone);
- the `YouTube` entry from Claude Desktop's configuration, if this installation added it (after a backup).

It **keeps** your data folder: OAuth client, token, settings and upload history, so a later reinstall picks up
where you left off. To delete that too, add `--delete-data`; you'll be asked to type `DELETE`. To also revoke
Google's access, follow [Revoking Google access](PRIVACY.md#revoking-google-access). Your videos on YouTube are
never affected.

## Installer options

| Option | Effect |
|---|---|
| `--with-quick-action` | install the macOS Finder Quick Action "Upload & Share" (no keyboard shortcut is assigned; you can add one in System Settings if you want) |
| `--configure-claude-desktop` | add the server to Claude Desktop's configuration, after a backup |
| `--print-client-config` | print the MCP client JSON for this computer and exit |
| `--non-interactive` | don't ask questions; optional parts stay off unless their option is given |
| `--rebuild-env` | rebuild the Python environment from scratch |
| `--allow-downgrade` | install an older version over a newer one |
| `--allow-untested-platform` | try installing on Linux (untested, see above) |
| `--uninstall` | remove the app (same as `uninstall.sh`) |
| `--delete-data` | with `--uninstall`: also delete your data folder (asks you to type `DELETE`) |

`YOUTUBE_MCP_PYTHON=/path/to/python3.12 ./install.sh` picks a specific Python.

## Troubleshooting

| Message | What to do |
|---|---|
| `No supported Python was found` | Install Python 3.12 (step 1), then run `./install.sh` again. |
| `Step failed: ... pip install ...` | Usually no internet connection or a proxy blocking pypi.org. Details are in `install.log` in the app folder. Nothing was changed; run the installer again. |
| `Step timed out` | A very slow connection. Run the installer again; files already downloaded are reused. |
| `Source file failed verification` | The download is damaged or was modified. Download the release again. |
| `Another YouTubeMCP install or uninstall is running` | Wait for it to finish. |
| `... already has a server named 'YouTube' that points somewhere else` | Remove or rename that entry in the client's configuration, or add the printed snippet under a different name. |
| The Quick Action doesn't appear in Finder | See [QUICK_ACTION.md](QUICK_ACTION.md#install). |

## Manual installation (for developers)

Running from a cloned or unzipped project folder still works exactly as before
([SETUP.md](SETUP.md)): credentials and data then live in that folder, and nothing is installed elsewhere. The
installer and a manual project folder can coexist; they use separate data folders unless you point
`YOUTUBE_MCP_HOME` at the same place.
