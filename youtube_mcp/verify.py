"""Post-write verification with bounded, read-only retries.

YouTube's API is eventually consistent: a write can be accepted while immediate reads still return
the previous state (or "not found" for something just created). Every write tool therefore reports
two separate things:

- the write outcome:  accepted (the API returned success), rejected (the API returned an error), or
  uncertain (the connection failed while writing; see YouTubeClient._write). Writes are never retried.
- the verification:   reads are retried with exponential backoff (~15 s in total) until the observed
  state matches what was written:
    confirmed      - a read shows the expected state
    not_confirmed  - reads succeeded but still show a different state (propagation delay, or YouTube
                     did not apply it); never "fixed" by writing again
    unverifiable   - the reads themselves failed (network, not found yet, ...)

An unconfirmed change is returned as a structured warning, not an error, so that the assistant does
not repeat a write that YouTube may already have accepted.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .errors import YouTubeMCPError

CONFIRMED, NOT_CONFIRMED, UNVERIFIABLE = "confirmed", "not_confirmed", "unverifiable"
DEFAULT_DELAYS: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0, 8.0)  # 6 reads over ~15.5 s


@dataclass
class Verification:
    status: str
    attempts: int
    elapsed_seconds: float
    observed: Any = None
    last_error: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def confirmed(self) -> bool:
        return self.status == CONFIRMED

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.status, "attempts": self.attempts,
                               "elapsed_seconds": round(self.elapsed_seconds, 1)}
        if self.last_error:
            out["last_read_error"] = self.last_error
        return out


class Verifier:
    def __init__(self, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 delays: tuple[float, ...] = DEFAULT_DELAYS) -> None:
        self.sleep = sleep
        self.clock = clock
        self.delays = delays

    def wait_for(self, read: Callable[[], Any], matches: Callable[[Any], bool]) -> Verification:
        """Call `read` (never a write) until `matches(value)` or the retry budget is spent."""
        start = self.clock()
        attempts, observed, last_error, ever_read = 0, None, None, False
        for delay in (0.0, *self.delays):
            if delay:
                self.sleep(delay)
            attempts += 1
            try:
                observed = read()
                ever_read, last_error = True, None
            except YouTubeMCPError as err:
                last_error = err.to_dict()["error"]
                if err.code in ("auth_expired", "not_authorized", "insufficient_scope", "quota_exceeded"):
                    break  # retrying cannot help
                continue
            if matches(observed):
                return Verification(CONFIRMED, attempts, self.clock() - start, observed)
        status = NOT_CONFIRMED if ever_read and last_error is None else UNVERIFIABLE
        return Verification(status, attempts, self.clock() - start, observed, last_error)


def write_report(verification: Verification, *, resource: dict[str, Any], check_with: str,
                 manual_attention: bool = False) -> dict[str, Any]:
    """Common fields for a write tool's response."""
    report: dict[str, Any] = {"write_accepted": True, "verified": verification.confirmed,
                              "verification": verification.to_dict(), **resource}
    if verification.confirmed:
        report["requires_manual_attention"] = False
        return report
    report["requires_manual_attention"] = manual_attention
    what = ("YouTube accepted the change, but reads still showed a different state"
            if verification.status == NOT_CONFIRMED else
            "YouTube accepted the change, but it could not be read back")
    report.setdefault("warnings", []).append({
        "code": "verification_pending",
        "message": (f"{what} after {verification.attempts} reads (~{verification.elapsed_seconds:.0f} s). This is "
                    "usually YouTube's propagation delay."),
        "action": (f"Do NOT repeat the change. Check again in a minute with {check_with}"
                   + (". Because this affects privacy or publishing, confirm it in YouTube Studio." if manual_attention
                      else ".")),
    })
    return report
