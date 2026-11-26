# Clean installation test report

**Version:** 1.0.0 · **Date:** 2026-11-26 · **Tester:** project maintainer (automated scenarios)

## Environment

| Item | Value |
|---|---|
| Operating system | macOS 26.6.2 |
| Architecture | Apple silicon (arm64) |
| Python used by the installer | 3.12.16 (Homebrew), found automatically by `install.sh` |
| Also present | Apple `/usr/bin/python3` 3.9.6 (used to test the "too old" path) |
| Isolation | Every scenario ran with a throwaway `HOME`, so the app, data, `~/Library/Services` and Claude Desktop configuration were all temporary. The tester's existing installation, credentials and Claude Desktop configuration were checksummed before and after and were **byte-identical**. |
| Input | The release archive built by `scripts/build_release.py`, unzipped (the "upgrade" releases were copies with a changed version and re-generated `SHA256SUMS`) |

## Not tested

- **Linux**: the installer refuses without `--allow-untested-platform`; nothing about Linux has been verified.
- **Intel Macs (x86_64)** and **Python 3.10, 3.11 and 3.13**: accepted by the installer, but not run.
- **Windows**: not supported.
- **No Python installed at all**: `install.sh`'s "no supported Python" message could not be triggered on this Mac
  (Homebrew Python is found by absolute path); the Python 3.9 refusal was tested instead.
- **Finder integration of the Quick Action**: the workflow was installed into a temporary `~/Library/Services` and
  its contents checked; whether Finder lists it was not re-checked, because that needs the real user folder. (The
  same workflow format is in daily use from a manual installation.)
- **Live YouTube calls**: none were made (no credentials in the test environment, by design).

## Results

### Real scenarios (32/32 passed)

Run with `install.sh` against the release archive. Packages were installed with
`pip --require-hashes --only-binary=:all: --index-url https://pypi.org/simple`, served from a local copy of pip's
download cache (every file still checked against the locked SHA-256 hashes); see "Download from PyPI" below for an
uncached run.

| # | Scenario | Result |
|---|---|---|
| 1 | Fresh installation | Pass (self-test: server started, 26 tools) |
| 2 | Manifest records version and data folder | Pass |
| 3 | Data folder created private (`0700`), containing only `youtube_mcp.env.example` | Pass |
| 4 | No credentials inside the app folder | Pass |
| 5 | pip used `--require-hashes --only-binary=:all:` and pypi.org only | Pass |
| 6 | MCP server starts from the printed client configuration (run from the source folder, to prove it can't be shadowed) | Pass |
| 7 | All 26 tool definitions present, names as expected | Pass |
| 8 | Missing credentials: a tool call returns `not_authorized` with the exact sign-in command | Pass |
| 9 | Missing credentials: `youtube-mcp-authorize --status` explains the missing OAuth client, no traceback | Pass |
| 10 | Repeated installation succeeds and reuses the environment (0.4 s) | Pass |
| 11 | Repeated installation preserves user data byte-for-byte (OAuth client, token, settings, history database) | Pass |
| 12 | Installer output never prints the seeded secrets | Pass |
| 13 | Upgrade 1.0.0 → 1.0.1, same dependencies: environment reused, new code active | Pass |
| 14 | Upgrade preserves user data | Pass |
| 15 | Upgrade with a changed dependency lock rebuilds the environment | Pass |
| 16 | No leftover `.old` / `.new` folders after upgrades | Pass |
| 17 | Downgrade 1.0.2 → 1.0.0 refused; 1.0.2 stays active | Pass |
| 18 | Network failure (unreachable proxy, empty cache): stops with a clear message, no traceback | Pass |
| 19 | After the network failure, the previous version still starts and lists 26 tools | Pass |
| 20 | Tampered source file rejected by `SHA256SUMS` verification before anything changes | Pass |
| 21 | Installer killed (SIGKILL) mid-build; the next run cleans up and completes | Pass |
| 22 | User data preserved through every failure | Pass |
| 23 | Quick Action not installed unless selected | Pass |
| 24 | Quick Action installed with `--with-quick-action`; runs the installed app (`-I -m youtube_mcp.quick_action`) | Pass |
| 25 | No keyboard shortcut assigned | Pass |
| 26 | Quick Action `status` works from the installed app and uses the data folder | Pass |
| 27 | `--configure-claude-desktop`: entry added, other servers and settings preserved, `0600` backup made, other servers' secrets never printed | Pass |
| 28 | Uninstall removes the app, the Quick Action and only our client entry | Pass |
| 29 | Uninstall keeps user data byte-for-byte | Pass |
| 30 | Reinstall after uninstall finds the existing data ("OAuth client found") | Pass |
| 31 | `uninstall.sh --delete-data --yes` removes the data folder | Pass |
| 32 | Python 3.9 refused with installation instructions; nothing created | Pass |

### Download from PyPI (uncached)

**Pass.** A separate fresh installation of the genuine 1.0.0 release archive with an **empty** pip cache and a new
`HOME`: all 48 package files were downloaded from pypi.org, checked against the locked hashes and installed; the
self-test started the server and listed all 26 tools. It took 11 minutes 39 seconds on a connection running at
roughly 20 kB/s at the time (most of it downloading); on a typical connection this step takes about a minute.

### Automated tests

`pytest`: **275 passed** (the 239 existing tests plus 36 installer tests in `tests/test_installer.py`, which run
offline and cover the same areas: layout, isolation, missing credentials, repeat, upgrade, downgrade, build and
self-test failures, interrupted runs (a simulated hard stop at every step of the switch-over), concurrent runs, `SHA256SUMS` verification, platform checks, client
configuration merging and refusal cases, Quick Action install/uninstall ownership, data preservation and deletion).

### Final 1.0.0 re-check

The installer's switch-over changed after the scenarios above (problem 4 below). Fresh install, repeat install,
an upgrade that rebuilt the environment from PyPI, and uninstall were re-run from the final release archive in a
throwaway `HOME`. All passed, with no unfinished or previous-version folders left behind.

## Problems found and fixed during testing

1. **Installed code could be shadowed by the working folder.** `python -m youtube_mcp` puts the current folder first
   on Python's import path, so a client (or the self-test) started from a folder containing a `youtube_mcp`
   directory imported that copy instead of the installed one. All installed commands, the Quick Action and the MCP
   client entry now run Python in isolated mode (`-I`); regression test added.
2. **The installer reported its own version instead of the release being installed** (only visible in upgrades).
   Fixed: the version is read from the source being installed.
3. **pip time limit too short for slow connections.** At about 20 kB/s the first attempt hit a 15-minute limit and
   showed a raw traceback. The limit is now 60 minutes and a timeout is reported as a clear, retryable message. The
   failed attempt also confirmed the recovery path: the unfinished environment was removed and the data folder was
   untouched.
4. **An interrupted upgrade could leave new dependencies with old code** (found in the final review). If the
   installer was stopped after the new environment went live but before the new code did, recovery kept both and
   the old manifest. Switching versions now goes through a journalled commit point: an interruption before it
   leaves the previous version untouched; after it, recovery completes the new version. Regression tests
   simulate a hard stop at every step.
