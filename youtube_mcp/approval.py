"""Shared helpers for the two-step approval flow (preview + single-use confirmation token).

Every tool that changes something on YouTube uses this flow: called without a token it
returns a preview and a token bound to the exact parameters; only a second call with that
token (after the user approved the preview) performs the change. See confirmations.py.
"""

from __future__ import annotations

from typing import Any

from .confirmations import ConfirmationStore
from .errors import YouTubeMCPError


def next_step_for_confirmation(tool: str, token: str, public: bool) -> str:
    text = (
        f"STOP: show this preview to the user and ask for explicit approval. Only after the user clearly "
        f"approves, call {tool} again with exactly the same arguments plus confirmation_token='{token}'."
    )
    if public:
        text += (" This action makes content PUBLIC: the user must explicitly approve public publishing, "
                 "and you must also pass confirm_public=true. Never assume approval. The user will then also "
                 "have to click 'Allow Public' in a native macOS dialog on their Mac; tell them to expect it.")
    return text


def public_requires_approval() -> YouTubeMCPError:
    return YouTubeMCPError(
        "public_requires_approval",
        "Making a video public requires the user's explicit approval.",
        action="Ask the user whether they want the video PUBLIC. Only if they explicitly say yes, call again "
               "with confirm_public=true and the confirmation_token from the preview.",
    )


def confirmation_required(
    store: ConfirmationStore,
    tool: str,
    params: dict[str, Any],
    preview: dict[str, Any],
    *,
    public: bool = False,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Issue a token bound to `params` and build the standard preview response."""
    token = store.issue(tool, params)
    return {
        "ok": True,
        "status": "confirmation_required",
        "preview": preview,
        "warnings": warnings or [],
        "confirmation_token": token,
        "expires_in_seconds": store.ttl,
        "next_step": next_step_for_confirmation(tool, token, public),
    }


def consume(store: ConfirmationStore, tool: str, token: str, params: dict[str, Any], *,
            public: bool = False, confirm_public: bool = False) -> None:
    """Validate an approval. Public actions additionally need confirm_public=true."""
    if public and not confirm_public:
        raise public_requires_approval()
    store.consume(token, tool, params)
