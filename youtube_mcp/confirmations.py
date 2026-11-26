"""One-time confirmation tokens for actions that need the user's explicit approval.

Flow: a tool called without a token returns a preview plus a token bound to the exact
parameters. The assistant shows the preview to the user and only after the user approves
calls the tool again with the token. Tokens are single-use, expire, and are rejected if
any parameter changed since the preview.

This is a guard rail against accidental actions by the model; the MCP client's own
per-tool permission prompt is the other layer. See README "Security model".
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from typing import Any, Callable

from .errors import YouTubeMCPError


def _digest(params: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()


class ConfirmationStore:
    def __init__(self, ttl_seconds: int = 600, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl = ttl_seconds
        self._clock = clock
        self._tokens: dict[str, tuple[str, str, float]] = {}
        self._lock = threading.Lock()

    def issue(self, action: str, params: dict[str, Any]) -> str:
        token = f"confirm-{secrets.token_urlsafe(9)}"
        with self._lock:
            now = self._clock()
            self._tokens = {k: v for k, v in self._tokens.items() if v[2] > now}
            self._tokens[token] = (action, _digest(params), now + self.ttl)
        return token

    def consume(self, token: str, action: str, params: dict[str, Any]) -> None:
        """Validate and burn a token. Raises YouTubeMCPError if it does not match."""
        with self._lock:
            entry = self._tokens.pop(token, None)
        retry = f"Call {action} again without confirmation_token to get a fresh preview, show it to the user, and wait for approval."
        if entry is None:
            raise YouTubeMCPError(
                "confirmation_invalid",
                "The confirmation token is unknown or was already used (tokens are single-use and reset when the server restarts).",
                action=retry,
            )
        token_action, digest, expires = entry
        if self._clock() > expires:
            raise YouTubeMCPError("confirmation_expired", "The confirmation token has expired.", action=retry)
        if token_action != action or digest != _digest(params):
            raise YouTubeMCPError(
                "confirmation_mismatch",
                "The parameters differ from the ones the user approved (or the file changed since the preview).",
                action=retry,
            )
