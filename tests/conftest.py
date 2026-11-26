from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.fakes import FakeYouTube  # noqa: E402
from youtube_mcp.config import Settings  # noqa: E402
from youtube_mcp.confirmations import ConfirmationStore  # noqa: E402
from youtube_mcp.history import History  # noqa: E402
from youtube_mcp.service import YouTubeMCPService  # noqa: E402
from youtube_mcp.uploads import UploadManager  # noqa: E402
from youtube_mcp.youtube import YouTubeClient  # noqa: E402


@dataclass
class Env:
    service: YouTubeMCPService
    api: FakeYouTube
    history: History
    tmp: Path
    notifications: list[tuple[str, str]] = field(default_factory=list)
    native_prompts: list[tuple[str, list]] = field(default_factory=list)
    native_answer: str = "allow"  # "allow" | "cancel" | "timeout" | "unavailable"
    clipboard: list[str] = field(default_factory=list)
    sleeps: list[float] = field(default_factory=list)

    def video(self, name: str = "demo.mp4", data: bytes | None = None) -> str:
        path = self.tmp / name
        path.write_bytes(data if data is not None else os.urandom(4096))
        return str(path)

    def image(self, name: str = "thumb.jpg", size: int = 2048) -> str:
        path = self.tmp / name
        path.write_bytes(b"\xff\xd8" + os.urandom(size))
        return str(path)

    def upload(self, path: str, **kwargs: Any) -> dict[str, Any]:
        """Preview + confirm in one go (simulating an approved upload)."""
        preview = self.service.upload_video(path, **kwargs)
        assert preview["status"] == "confirmation_required", preview
        return self.service.upload_video(path, confirmation_token=preview["confirmation_token"],
                                         background=False, **kwargs)


@pytest.fixture(autouse=True)
def _no_real_native_dialogs(monkeypatch):
    """Tests must never show a real macOS dialog. Any code path that would reach osascript for a
    native confirmation fails the test instead (and fails closed)."""
    import youtube_mcp.native_confirm as nc
    import youtube_mcp.service as svc_mod

    def forbidden(*args, **kwargs):
        raise AssertionError("A test tried to show a real native confirmation dialog")
    import subprocess
    import types
    monkeypatch.setattr(nc, "subprocess", types.SimpleNamespace(run=forbidden, SubprocessError=subprocess.SubprocessError))
    monkeypatch.setattr(svc_mod, "macos_confirm_public", lambda *a, **k: forbidden())


@pytest.fixture
def env(tmp_path: Path) -> Env:
    settings = Settings(client_secret_file=tmp_path / "client_secret.json",
                        token_file=tmp_path / "token_youtube_mcp.json", data_dir=tmp_path / "data",
                        # A fixed zone with a 45-minute offset, so offset handling is really exercised.
                        default_timezone="Asia/Kathmandu")
    api = FakeYouTube()
    client = YouTubeClient(lambda: api)
    history = History(settings.history_db)
    e = Env(service=None, api=api, history=history, tmp=tmp_path)  # type: ignore[arg-type]
    uploads = UploadManager(
        client, history,
        notifier=lambda t, m: e.notifications.append((t, m)) or True,
        clipboard=lambda text: e.clipboard.append(text) or True,
        sleep=e.sleeps.append,
    )
    def fake_confirmer(action, details):
        from youtube_mcp.native_confirm import denied
        e.native_prompts.append((action, details))
        if e.native_answer == "cancel":
            raise denied("cancelled")
        if e.native_answer == "timeout":
            raise denied("timed out", "native_confirmation_timeout")
        if e.native_answer == "unavailable":
            raise denied("unavailable", "native_confirmation_unavailable")

    e.service = YouTubeMCPService(settings, client, history, uploads, ConfirmationStore(600),
                                  clipboard=lambda text: e.clipboard.append(text) or True,
                                  public_confirmer=fake_confirmer)
    from youtube_mcp.verify import Verifier
    e.service.verifier = Verifier(sleep=e.sleeps.append)  # record backoff delays instead of sleeping
    return e
