"""Persistent local upload history and action audit log (SQLite, stdlib only).

Tables:
    uploads  one row per upload attempt: file fingerprint, metadata, status, video id,
             requested vs. verified visibility. Used for duplicate-upload protection.
    actions  append-only audit log of every change made through the server
             (metadata edits, visibility changes, thumbnails, uploads).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    upload_id            TEXT PRIMARY KEY,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    file_path            TEXT NOT NULL,
    file_name            TEXT NOT NULL,
    file_size            INTEGER NOT NULL,
    sha256               TEXT NOT NULL,
    title                TEXT NOT NULL,
    description          TEXT NOT NULL DEFAULT '',
    tags                 TEXT NOT NULL DEFAULT '[]',
    category_id          TEXT NOT NULL,
    requested_visibility TEXT NOT NULL,
    actual_visibility    TEXT,
    status               TEXT NOT NULL,
    progress             REAL NOT NULL DEFAULT 0,
    video_id             TEXT,
    error                TEXT,
    warnings             TEXT NOT NULL DEFAULT '[]',
    pid                  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_uploads_sha256 ON uploads(sha256);
CREATE INDEX IF NOT EXISTS idx_uploads_video_id ON uploads(video_id);
CREATE TABLE IF NOT EXISTS actions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    video_id  TEXT,
    action    TEXT NOT NULL,
    details   TEXT NOT NULL DEFAULT '{}'
);
"""

# Upload statuses
QUEUED, UPLOADING, VERIFYING, COMPLETED, FAILED, INTERRUPTED = (
    "queued", "uploading", "verifying", "completed", "failed", "interrupted",
)
ACTIVE_STATUSES = (QUEUED, UPLOADING, VERIFYING)

# What is known about a failed or interrupted upload attempt (stored as error["outcome"]):
OUTCOME_NOT_SENT = "not_sent"    # failed before any media bytes were sent: YouTube cannot have the video
OUTCOME_REJECTED = "rejected"    # YouTube answered with a client error (4xx): the video was not created
OUTCOME_UNCERTAIN = "uncertain"  # bytes were sent and no definite answer arrived: YouTube MAY have it
# Error codes that are definite refusals (for records written before outcomes were stored).
_DEFINITE_CODES = {"auth_expired", "not_authorized", "insufficient_scope", "token_invalid", "quota_exceeded",
                   "upload_limit_exceeded", "upload_not_allowed", "invalid_metadata", "invalid_request",
                   "invalid_input", "permission_denied", "api_not_enabled", "file_not_found",
                   "file_changed_since_approval", "insufficient_disk_space"}


def interrupted_error(status: str, code: str = "upload_interrupted",
                      message: str = "The upload stopped before it finished.") -> dict[str, Any]:
    if status == QUEUED:  # nothing had been sent yet
        return {"code": code, "message": message, "outcome": OUTCOME_NOT_SENT,
                "action": "Start the upload again; nothing reached YouTube."}
    return {"code": code, "message": message + " YouTube may still have received it.", "outcome": OUTCOME_UNCERTAIN,
            "action": ("Start the upload again: the server first checks the channel for this upload, and asks for an "
                       "explicit decision if it cannot tell whether YouTube received it.")}


def attempt_outcome(record: dict[str, Any]) -> str:
    """Classify a failed/interrupted upload record. Unknown cases are treated as uncertain (conservative)."""
    error = record.get("error") or {}
    if error.get("outcome") in (OUTCOME_NOT_SENT, OUTCOME_REJECTED, OUTCOME_UNCERTAIN):
        return error["outcome"]
    if error.get("code") in _DEFINITE_CODES:
        return OUTCOME_REJECTED
    return OUTCOME_UNCERTAIN
_JSON_FIELDS = ("tags", "error", "warnings")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_OWNER_MARKERS = ("youtube_mcp", "youtube-mcp", "upload_and_share")


def _pid_alive(pid: int | None) -> bool:
    """True if `pid` is alive AND still looks like one of our processes (guards against PID reuse)."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        import subprocess

        cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True,
                             timeout=5).stdout
    except Exception:
        return True  # cannot tell: assume alive (safe default: never mark a live upload as interrupted)
    if not cmd.strip():
        return True
    return any(marker in cmd for marker in _OWNER_MARKERS)


class History:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(db_path.parent, 0o700)
        except OSError:
            pass
        with self._connect() as conn:
            conn.executescript(SCHEMA)
        try:
            os.chmod(db_path, 0o600)
        except OSError:
            pass

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(str(self.db_path), timeout=10)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        for field in _JSON_FIELDS:
            if data.get(field) is not None:
                data[field] = json.loads(data[field])
        return data

    # --- uploads ------------------------------------------------------------------

    def create_upload(self, **fields: Any) -> str:
        upload_id = fields.pop("upload_id", None) or uuid.uuid4().hex[:12]
        now = _now()
        record = {
            "upload_id": upload_id,
            "created_at": now,
            "updated_at": now,
            "status": QUEUED,
            "pid": os.getpid(),
            **fields,
        }
        for field in _JSON_FIELDS:
            if field in record and not isinstance(record[field], str) and record[field] is not None:
                record[field] = json.dumps(record[field])
        cols = ", ".join(record)
        marks = ", ".join("?" for _ in record)
        with self._connect() as conn:
            conn.execute(f"INSERT INTO uploads ({cols}) VALUES ({marks})", list(record.values()))
        return upload_id

    def create_upload_checked(self, check: Callable[[list[dict[str, Any]]], None], **fields: Any) -> str:
        """Atomically run `check(existing_records_for_sha256)` and insert the new upload.

        Uses a SQLite IMMEDIATE transaction, so the duplicate check and the insert are one
        step across processes (MCP server and Finder Quick Action). `check` raises to abort.
        """
        upload_id = fields.pop("upload_id", None) or uuid.uuid4().hex[:12]
        now = _now()
        record = {"upload_id": upload_id, "created_at": now, "updated_at": now, "status": QUEUED,
                  "pid": os.getpid(), **fields}
        for field in _JSON_FIELDS:
            if field in record and not isinstance(record[field], str) and record[field] is not None:
                record[field] = json.dumps(record[field])
        with self._lock:
            conn = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute("SELECT * FROM uploads WHERE sha256 = ? ORDER BY created_at DESC",
                                    (record["sha256"],)).fetchall()
                try:
                    check([self._row(r) for r in rows])  # type: ignore[misc]
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                cols = ", ".join(record)
                conn.execute(f"INSERT INTO uploads ({cols}) VALUES ({', '.join('?' for _ in record)})",
                             list(record.values()))
                conn.execute("COMMIT")
            finally:
                conn.close()
        return upload_id

    def update_upload(self, upload_id: str, **fields: Any) -> None:
        fields["updated_at"] = _now()
        for field in _JSON_FIELDS:
            if field in fields and fields[field] is not None and not isinstance(fields[field], str):
                fields[field] = json.dumps(fields[field])
        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self._connect() as conn:
            conn.execute(f"UPDATE uploads SET {assignments} WHERE upload_id = ?", [*fields.values(), upload_id])

    def get_upload(self, upload_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            return self._row(conn.execute("SELECT * FROM uploads WHERE upload_id = ?", (upload_id,)).fetchone())

    def find_by_sha256(self, sha256: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM uploads WHERE sha256 = ? ORDER BY created_at DESC", (sha256,)
            ).fetchall()
        return [self._row(r) for r in rows]  # type: ignore[misc]

    def known_video_ids(self) -> set[str]:
        with self._connect() as conn:
            return {r[0] for r in conn.execute("SELECT DISTINCT video_id FROM uploads WHERE video_id IS NOT NULL")}

    def list_uploads(self, limit: int = 25, offset: int = 0) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM uploads ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
        return [self._row(r) for r in rows]  # type: ignore[misc]

    def count_uploads(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM uploads").fetchone()[0])

    def recover_interrupted(self) -> int:
        """Mark uploads left 'active' by a process that no longer exists as interrupted."""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT upload_id, pid, status FROM uploads WHERE status IN ({','.join('?' * len(ACTIVE_STATUSES))})",
                ACTIVE_STATUSES,
            ).fetchall()
            stale = [r for r in rows if r["pid"] != os.getpid() and not _pid_alive(r["pid"])]
            for row in stale:
                conn.execute(
                    "UPDATE uploads SET status = ?, updated_at = ?, error = ? WHERE upload_id = ?",
                    (INTERRUPTED, _now(), json.dumps(interrupted_error(row["status"])), row["upload_id"]),
                )
        return len(stale)

    # --- audit log ------------------------------------------------------------------

    @contextmanager
    def exclusive(self, name: str, timeout: float = 60.0) -> Iterator[None]:
        """Cross-process mutual exclusion (flock on a lock file next to the database).

        Serialises check-then-write sequences between every local process that shares this history
        (several MCP server instances and the Finder Quick Action).
        """
        key = hashlib.sha256(name.encode()).hexdigest()[:16]
        path = self.db_path.parent / f"lock-{key}.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        from .errors import YouTubeMCPError
                        raise YouTubeMCPError("busy", "Another local process is making the same change.",
                                              action="Wait a moment, check the current state, then try again.",
                                              retryable=True) from None
                    time.sleep(0.05)
            yield
        finally:
            os.close(fd)  # closing releases the lock

    def last_playlist_change(self, playlist_id: str, video_id: str) -> dict[str, Any] | None:
        """The most recent locally recorded add/remove of this video in this playlist, if any."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ts, action, details FROM actions WHERE video_id = ? AND action IN "
                "('add_to_playlist', 'remove_from_playlist') ORDER BY id DESC LIMIT 200", (video_id,)).fetchall()
        for row in rows:
            details = json.loads(row["details"] or "{}")
            if details.get("playlist_id") == playlist_id:
                return {"ts": row["ts"], "action": row["action"], "details": details}
        return None

    def log_action(self, action: str, video_id: str | None, details: dict[str, Any] | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO actions (ts, video_id, action, details) VALUES (?, ?, ?, ?)",
                (_now(), video_id, action, json.dumps(details or {}, default=str)),
            )

    def list_actions(self, video_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if video_id:
                rows = conn.execute(
                    "SELECT * FROM actions WHERE video_id = ? ORDER BY id DESC LIMIT ?", (video_id, limit)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM actions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(r), "details": json.loads(r["details"])} for r in rows]
