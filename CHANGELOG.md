# Changelog

All notable changes to this project. Versions follow [Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-11-26

First public release.

### Fixed
- Write tools could report a failure after a successful write because YouTube is eventually consistent:
  `create_playlist` (`playlist_not_found`), `update_playlist` (old description returned),
  `schedule_publish` (`schedule_not_verified`) and `cancel_publish_schedule` (`cancel_not_verified`).
  All write tools now share bounded, read-only verification with exponential backoff (~15 s).
- Writes could be sent more than once: the Google client library retried POST/PUT/DELETE on server and connection
  errors. Writes are now sent exactly once. A failure mid-write returns `write_outcome_uncertain`.
- An upload whose new video was briefly "not found" was reported with a `verification_failed` warning.
- Playlist list responses now separate YouTube's reported total from the returned items and flag discrepancies.

### Installer
- `install.sh` / `install.py` / `uninstall.sh`: per-user installation on macOS without sudo; isolated Python
  environment from a hash-locked, wheel-only dependency set (`requirements.lock.txt` now carries SHA-256 hashes);
  source verification against `SHA256SUMS`; self-test before switching versions; safe repeat, upgrade, downgrade
  refusal and recovery from interrupted runs; optional Quick Action and Claude Desktop configuration (backed up);
  uninstall keeps user data unless `--delete-data`. Linux is untested and requires an explicit flag.
- Upgrades switch versions through a journalled commit point: an interruption at any moment (power loss, kill)
  is recovered to one complete version, either all previous or all new, never new dependencies with old code.
  The previous release's installer can no longer reuse an environment built for a newer version.
- Installed commands, the Quick Action and the MCP client entry run Python in isolated mode (`-I`), so a
  `youtube_mcp` folder in the working directory can't shadow the installed code.
- `scripts/build_release.py` builds the credential-free source archive with `SHA256SUMS`;
  `scripts/lock_hashes.py` regenerates the hash lock.

### Security hardening (independent review)
- Indirect public exposure through playlists: making a playlist public lists the unlisted videos it would expose;
  changing a private video to unlisted checks the channel's public playlists and, if it's in one, needs
  `confirm_public` and the native dialog. The exposure found is bound to the approval (`add_to_playlist` too).
- Thumbnails and caption files are sent from a verified private snapshot (as uploads already were), and
  `verify_content` compares against that snapshot.
- Correcting an unexpectedly public upload is verified by read-back; if unconfirmed, an urgent
  `unexpectedly_public` warning with the Studio link is returned instead of claiming success.
- `add_to_playlist` is serialised across local processes and treats a very recent local add as already done.
- Failed uploads are classified `not_sent`, `rejected` or `uncertain`. An uncertain attempt that can't be matched
  on the channel now returns `upload_outcome_uncertain` and needs an explicit `allow_duplicate=true`
  (Quick Action: `upload --allow-duplicate` from Terminal) instead of uploading again automatically.
- An installed package (outside a source checkout) keeps credentials and data in a per-user directory, never in
  site-packages. Running from a project folder is unchanged.

### Changed
- Personal defaults replaced with generic ones: the scheduling timezone defaults to the computer's timezone,
  the category region to `US` and the upload category to `22` (People & Blogs). Preferences can be kept in an
  optional local `youtube_mcp.env` file, read by both the server and the Finder Quick Action.
- `verify_content` is shown in caption upload/update previews and bound to the confirmation token.

### Added
- MIT licence, step-by-step setup guide, generated tool reference (`docs/TOOLS.md`), security and behaviour docs.
- Privacy policy (`docs/PRIVACY.md`): data flows, local storage and retention, revoking Google access, deleting
  local data. No telemetry of any kind.
- `download_captions` (read-only) to read caption text. `verify_content` option for `upload_captions` and
  `update_captions`.
- `get_video_status` now returns description, tags, category, default languages, thumbnails, licence and scheduling.
- Write responses include `write_accepted`, `verified`, `verification`, `requires_manual_attention` and resource IDs.
- `set_thumbnail` distinguishes YouTube's acceptance from independent verification of the image.

### Security hardening

#### Added
- Native macOS "Allow Public" dialog (default Cancel, fails closed) for every action that exposes content publicly.
- Verified private snapshots of approved upload files (APFS clone or copy) so the uploaded bytes match the approval.
- `ambiguous_upload`: failed uploads are only linked to a YouTube video on multi-signal evidence.

#### Changed (breaking)
- `update_metadata` and every `set_visibility` change are now two-step (preview + confirmation token).

#### Removed
- The legacy `upload.py` script and its `token.json`.

### Accuracy and reliability audit

#### Fixed
- Cross-process duplicate-upload race (MCP server instances and the Finder Quick Action).
- Stale "active" uploads (crashed processes, PID reuse) no longer block new uploads.
- Tracebacks are redacted in logs. Schedule times without a time of day, or in a DST gap, are rejected.
- `get_channel_info` separates YouTube's public video count from total uploads.

#### Added
- Analytics responses include official metric definitions, `engaged_views` and `data_coverage`.

## [0.2.0] - Captions, playlists, categories, scheduling, analytics

## [0.1.0] - Upload server and Finder Quick Action "Upload & Share"
