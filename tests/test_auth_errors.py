from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone

import pytest
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials

from tests.fakes import http_error
from youtube_mcp.auth import CredentialManager
from youtube_mcp.config import SCOPES, Settings
from youtube_mcp.errors import YouTubeMCPError, redact, translate_exception

FAKE_ACCESS = "ya29.FAKEaccessTOKENvalue_1234567890"
FAKE_REFRESH = "1//0gFAKErefreshTOKENvalue1234567890"
FAKE_SECRET = "GOCSPX-FAKEclientSECRET123"


def write_token(path, scopes=SCOPES, expired=True):
    expiry = datetime.now(timezone.utc) + (timedelta(hours=-1) if expired else timedelta(hours=1))
    path.write_text(json.dumps({
        "token": FAKE_ACCESS, "refresh_token": FAKE_REFRESH, "client_id": "id.apps.googleusercontent.com",
        "client_secret": FAKE_SECRET, "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": list(scopes), "expiry": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }))


@pytest.fixture
def settings(tmp_path):
    return Settings(client_secret_file=tmp_path / "cs.json", token_file=tmp_path / "token_youtube_mcp.json",
                    data_dir=tmp_path / "data")


def assert_no_secrets(text: str):
    for secret in (FAKE_ACCESS, FAKE_REFRESH, FAKE_SECRET):
        assert secret not in text


def test_missing_token_gives_authorize_command(settings):
    with pytest.raises(YouTubeMCPError) as exc:
        CredentialManager(settings).get_credentials()
    assert exc.value.code == "not_authorized"
    assert "authorize.py" in exc.value.action


def test_upload_only_token_is_insufficient(settings):
    write_token(settings.token_file, scopes=[SCOPES[0]])
    with pytest.raises(YouTubeMCPError) as exc:
        CredentialManager(settings).get_credentials()
    assert exc.value.code == "insufficient_scope"
    assert exc.value.details["missing_scopes"] == [SCOPES[1]]


def test_expired_refresh_rejected(settings, monkeypatch):
    write_token(settings.token_file)

    def boom(self, request):
        raise RefreshError(f"invalid_grant: Token has been expired or revoked. token={FAKE_REFRESH}")
    monkeypatch.setattr(Credentials, "refresh", boom)
    with pytest.raises(YouTubeMCPError) as exc:
        CredentialManager(settings).get_credentials()
    assert exc.value.code == "auth_expired" and "7 days" in exc.value.message
    assert_no_secrets(json.dumps(exc.value.to_dict()))


def test_expired_token_refreshes_and_saves_securely(settings, monkeypatch):
    write_token(settings.token_file)

    def ok(self, request):
        self.token = "ya29.NEWtoken"
        self.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
    monkeypatch.setattr(Credentials, "refresh", ok)
    creds = CredentialManager(settings).get_credentials()
    assert creds.valid
    assert stat.S_IMODE(os.stat(settings.token_file).st_mode) == 0o600
    assert json.loads(settings.token_file.read_text())["token"] == "ya29.NEWtoken"


def test_valid_token_not_refreshed(settings, monkeypatch):
    write_token(settings.token_file, expired=False)
    monkeypatch.setattr(Credentials, "refresh", lambda *a: pytest.fail("should not refresh"))
    assert CredentialManager(settings).get_credentials().valid


def test_corrupt_token(settings):
    settings.token_file.write_text("{not json")
    with pytest.raises(YouTubeMCPError) as exc:
        CredentialManager(settings).get_credentials()
    assert exc.value.code == "token_invalid"


def test_redaction():
    text = f"Bearer header Authorization: Bearer {FAKE_ACCESS} refresh={FAKE_REFRESH} secret {FAKE_SECRET} " \
           f'{{"access_token": "abc123"}}'
    out = redact(text)
    assert_no_secrets(out) and "abc123" not in out


@pytest.mark.parametrize("status,reason,code,retryable", [
    (403, "quotaExceeded", "quota_exceeded", False),
    (403, "insufficientPermissions", "insufficient_scope", False),
    (401, "", "auth_expired", False),
    (404, "videoNotFound", "video_not_found", False),
    (400, "invalidTitle", "invalid_metadata", False),
    (400, "uploadLimitExceeded", "upload_limit_exceeded", False),
    (429, "", "rate_limited", True),
    (503, "backendError", "youtube_unavailable", True),
    (403, "forbidden", "permission_denied", False),
])
def test_http_error_mapping(status, reason, code, retryable):
    err = translate_exception(http_error(status, reason, "msg"))
    assert err.code == code and err.retryable is retryable
    d = err.to_dict()
    assert d["ok"] is False and d["error"]["message"]


def test_network_errors_retryable():
    assert translate_exception(ConnectionResetError()).code == "network_error"
    assert translate_exception(TimeoutError()).retryable


def test_settings_paths_follow_env_not_directory_name(monkeypatch, tmp_path):
    monkeypatch.setenv("YOUTUBE_MCP_HOME", str(tmp_path))
    s = Settings.from_env()
    assert s.token_file == tmp_path / "token_youtube_mcp.json"
    assert s.client_secret_file == tmp_path / "client_secret.json"
    assert s.history_db == tmp_path / "data" / "history.sqlite3"
    monkeypatch.delenv("YOUTUBE_MCP_HOME")
    monkeypatch.setenv("YOUTUBE_MCP_TOKEN", str(tmp_path / "elsewhere.json"))
    assert Settings.from_env().token_file == tmp_path / "elsewhere.json"


def test_no_hardcoded_personal_paths():
    """Distributed code and docs contain no absolute home-directory paths except generic examples."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    files = [*(root / "youtube_mcp").glob("*.py"), *(root / "tests").glob("*.py"), *(root / "scripts").glob("*.py"),
             *(root / "docs").glob("*.md"), *root.glob("*.py"), *root.glob("*.md")]
    home = re.compile(r"/(?:Users|home)/([A-Za-z0-9._-]+)")
    for f in files:
        users = set(home.findall(f.read_text())) - {"me", "you", "example"}
        assert not users, (f, users)
