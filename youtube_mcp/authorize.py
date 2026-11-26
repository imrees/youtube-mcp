"""Authorize the YouTubeMCP server (one-time browser consent), or check its status.

Usage:
    python authorize.py            # open the browser and grant access
    python authorize.py --status   # show authorization status (no secrets printed)
    python authorize.py --no-browser  # print the consent URL instead of opening a browser
    python authorize.py --analytics   # also grant the optional read-only YouTube Analytics permission
    python authorize.py --analytics --youtube-readonly   # add youtube.readonly too (only if Google requires it)

Optional permissions already granted are kept when re-authorizing. If an optional permission is
not granted, the existing token file is left untouched.

This writes the server's own token file (default: token_youtube_mcp.json).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .auth import CredentialManager, save_credentials
from .config import ANALYTICS_ACCEPTED_SCOPES, ANALYTICS_SCOPE, SCOPES, YOUTUBE_READONLY_SCOPE, Settings
from .errors import YouTubeMCPError, redact, translate_exception
from .youtube import YouTubeClient


def _channel_summary(manager: CredentialManager) -> str:
    try:
        ch = YouTubeClient(manager.build_service).get_my_channel()
        return f"{ch['snippet'].get('title')} (channel ID {ch.get('id')})"
    except YouTubeMCPError as err:
        return f"could not read channel: {err}"


def status(settings: Settings) -> int:
    print(f"Client secret file : {settings.client_secret_file} ({'found' if settings.client_secret_file.exists() else 'MISSING'})")
    print(f"Token file         : {settings.token_file} ({'found' if settings.token_file.exists() else 'not created yet'})")
    if not settings.token_file.exists():
        print("\nNot authorized yet. Run this command without --status.")
        return 1
    manager = CredentialManager(settings)
    try:
        creds = manager.get_credentials()
    except YouTubeMCPError as err:
        print(f"\nAuthorization problem: {err}\n{err.action or ''}")
        return 1
    print(f"Scopes             : {', '.join(s.rsplit('/', 1)[-1] for s in creds.scopes or [])}")
    print(f"Access token valid : until {creds.expiry:%Y-%m-%d %H:%M} UTC (refreshes automatically)")
    has_analytics = bool(set(creds.scopes or []) & set(ANALYTICS_ACCEPTED_SCOPES))
    print(f"Analytics (optional): {'enabled' if has_analytics else 'not enabled (run with --analytics)'}")
    print(f"Channel            : {_channel_summary(manager)}")
    return 0


def _existing_optional_scopes(settings: Settings) -> set[str]:
    """Optional scopes in the current token, so re-authorizing does not silently drop them."""
    try:
        data = json.loads(settings.token_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    scopes = data.get("scopes") or []
    return {s for s in scopes if s in (ANALYTICS_SCOPE, YOUTUBE_READONLY_SCOPE)}


def authorize(settings: Settings, open_browser: bool = True, analytics: bool = False,
              youtube_readonly: bool = False) -> int:
    from google_auth_oauthlib.flow import InstalledAppFlow

    optional = _existing_optional_scopes(settings)
    if analytics:
        optional.add(ANALYTICS_SCOPE)
    if youtube_readonly:
        optional.add(YOUTUBE_READONLY_SCOPE)
    requested = list(SCOPES) + sorted(optional)

    if not settings.client_secret_file.exists():
        print(f"OAuth client file not found: {settings.client_secret_file}\n"
              "Download the Desktop OAuth client JSON from Google Cloud Console > APIs & Services > Credentials,\n"
              "save it there (or set YOUTUBE_MCP_CLIENT_SECRET), and run this again.", file=sys.stderr)
        return 1

    # Google may grant fewer scopes than requested (granular consent); we check explicitly below.
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    flow = InstalledAppFlow.from_client_secrets_file(str(settings.client_secret_file), requested)
    print("Opening Google sign-in in your browser. Choose the account that owns your YouTube channel")
    print("and tick ALL requested permissions.")
    if optional:
        print("Optional read-only permissions requested: " + ", ".join(s.rsplit("/", 1)[-1] for s in sorted(optional)))
    print()
    try:
        creds = flow.run_local_server(
            port=0, open_browser=open_browser, prompt="consent", access_type="offline",
            authorization_prompt_message="If the browser does not open, visit this URL:\n{url}\n",
            success_message="Authorization complete. You can close this tab and return to Terminal.",
        )
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
    except Exception as exc:
        print(f"\nAuthorization failed: {redact(str(translate_exception(exc)))}", file=sys.stderr)
        return 1

    granted = set(getattr(creds, "granted_scopes", None) or creds.scopes or [])
    missing = [s for s in SCOPES if s not in granted]
    if missing:
        print("\nNot all permissions were granted. Missing:\n  " + "\n  ".join(missing), file=sys.stderr)
        print("Run this command again and tick every checkbox on the consent screen.", file=sys.stderr)
        return 1
    missing_optional = sorted(s for s in optional if s not in granted)
    if missing_optional:
        print("\nThe optional permission(s) " + ", ".join(m.rsplit("/", 1)[-1] for m in missing_optional)
              + " were not granted.", file=sys.stderr)
        print("Your existing authorization was left unchanged" if settings.token_file.exists()
              else "Nothing was saved", file=sys.stderr, end="")
        print(". Run again and tick every checkbox, or run without --analytics.", file=sys.stderr)
        return 1
    if not creds.refresh_token:
        print("\nGoogle did not return a refresh token. Remove the app's access at "
              "https://myaccount.google.com/permissions and run this again.", file=sys.stderr)
        return 1

    save_credentials(settings.token_file, creds)
    print(f"\nSaved authorization to {settings.token_file} (permissions 600).")
    print(f"Connected channel: {_channel_summary(CredentialManager(settings))}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Authorize the YouTubeMCP server with your Google account.")
    parser.add_argument("--status", action="store_true", help="Show authorization status without changing anything.")
    parser.add_argument("--no-browser", action="store_true", help="Print the consent URL instead of opening a browser.")
    parser.add_argument("--analytics", action="store_true",
                        help="Also grant the optional read-only YouTube Analytics permission (yt-analytics.readonly).")
    parser.add_argument("--youtube-readonly", action="store_true",
                        help="Also grant youtube.readonly (only needed if Google rejects analytics without it).")
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    if args.status:
        return status(settings)
    return authorize(settings, open_browser=not args.no_browser, analytics=args.analytics,
                     youtube_readonly=args.youtube_readonly)


if __name__ == "__main__":
    sys.exit(main())
