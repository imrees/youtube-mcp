"""OAuth credential loading, refresh and secure persistence.

The MCP server never opens a browser: if authorization is missing, expired or lacks
scopes, it raises an actionable error telling the user to run `authorize.py`.
Token contents are never logged or returned to the MCP client.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from .config import ANALYTICS_ACCEPTED_SCOPES, SCOPES, Settings
from .errors import (
    YouTubeMCPError,
    analytics_not_enabled,
    auth_expired,
    insufficient_scope,
    not_authorized,
    translate_exception,
)

log = logging.getLogger(__name__)


def save_credentials(path: Path, creds: Any) -> None:
    """Atomically write credentials with owner-only (0600) permissions."""
    if not path.parent.exists():  # e.g. a fresh per-user directory for an installed package: owner-only
        path.parent.mkdir(parents=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".token-", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(creds.to_json())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


class CredentialManager:
    """Loads and refreshes the server's own OAuth token (thread-safe)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.Lock()

    def get_credentials(self) -> Any:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        with self._lock:
            path = self.settings.token_file
            if not path.exists():
                raise not_authorized(path)
            try:
                creds = Credentials.from_authorized_user_file(str(path))
            except (ValueError, OSError) as exc:
                raise YouTubeMCPError(
                    "token_invalid",
                    f"The token file could not be read ({type(exc).__name__}).",
                    action="Re-run the authorize command to create a fresh token.",
                ) from None

            missing = [s for s in SCOPES if s not in set(creds.scopes or [])]
            if missing:
                raise insufficient_scope(missing)

            if not creds.valid:
                if not creds.refresh_token:
                    raise auth_expired("no refresh token stored")
                log.info("Access token expired; refreshing")
                try:
                    creds.refresh(Request())
                except Exception as exc:  # RefreshError, TransportError, ...
                    raise translate_exception(exc) from None
                save_credentials(path, creds)
            return creds

    def build_service(self) -> Any:
        """Build a fresh YouTube API client (httplib2 is not thread-safe, so one per thread/job)."""
        from googleapiclient.discovery import build

        return build("youtube", "v3", credentials=self.get_credentials(), cache_discovery=False)

    def has_analytics_scope(self) -> bool:
        try:
            creds = self.get_credentials()
        except YouTubeMCPError:
            return False
        return bool(set(creds.scopes or []) & set(ANALYTICS_ACCEPTED_SCOPES))

    def build_analytics_service(self) -> Any:
        """YouTube Analytics API v2 client. Requires the optional analytics scope (opt-in)."""
        from googleapiclient.discovery import build

        creds = self.get_credentials()
        if not set(creds.scopes or []) & set(ANALYTICS_ACCEPTED_SCOPES):
            raise analytics_not_enabled()
        return build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)
