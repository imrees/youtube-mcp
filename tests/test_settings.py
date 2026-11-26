"""Generic defaults and the optional local settings file (youtube_mcp.env)."""

from __future__ import annotations

from youtube_mcp import config
from youtube_mcp.config import Settings, read_settings_file

KEYS = ("YOUTUBE_MCP_TIMEZONE", "YOUTUBE_MCP_REGION", "YOUTUBE_MCP_DEFAULT_CATEGORY", "YOUTUBE_MCP_DATA_DIR",
        "YOUTUBE_MCP_TOKEN", "YOUTUBE_MCP_CLIENT_SECRET", "YOUTUBE_MCP_CONFIRM_TIMEOUT", "YOUTUBE_MCP_LOG_LEVEL")


def _clean(monkeypatch, home):
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("YOUTUBE_MCP_HOME", str(home))


def test_generic_defaults_without_settings_file(monkeypatch, tmp_path):
    _clean(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "system_timezone", lambda: "Europe/Berlin")
    s = Settings.from_env()
    assert (s.default_region, s.default_category_id, s.default_timezone) == ("US", "22", "Europe/Berlin")
    assert s.token_file == tmp_path.resolve() / "token_youtube_mcp.json"


def test_settings_file_applies_and_environment_wins(monkeypatch, tmp_path):
    _clean(monkeypatch, tmp_path)
    (tmp_path / "youtube_mcp.env").write_text(
        "# comment\nYOUTUBE_MCP_TIMEZONE=Asia/Tokyo\nYOUTUBE_MCP_REGION = gb\n"
        "YOUTUBE_MCP_DEFAULT_CATEGORY='27'\nnot a setting\nUNRELATED_SECRET=x\nYOUTUBE_MCP_HOME=/elsewhere\n")
    s = Settings.from_env()
    assert (s.default_timezone, s.default_region, s.default_category_id) == ("Asia/Tokyo", "GB", "27")
    monkeypatch.setenv("YOUTUBE_MCP_REGION", "DE")
    assert Settings.from_env().default_region == "DE"
    values = read_settings_file(tmp_path / "youtube_mcp.env")
    assert "UNRELATED_SECRET" not in values and "YOUTUBE_MCP_HOME" not in values


def test_confirm_timeout_from_settings_file_is_still_bounded(monkeypatch, tmp_path):
    from youtube_mcp import native_confirm as nc

    _clean(monkeypatch, tmp_path)
    (tmp_path / "youtube_mcp.env").write_text("YOUTUBE_MCP_CONFIRM_TIMEOUT=0\n")
    assert nc._timeout() == 10


def test_system_timezone_falls_back_to_utc(monkeypatch):
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(config.os.path, "realpath", lambda p: "/somewhere/without/zone")
    assert config.system_timezone() == "UTC"
    monkeypatch.setenv("TZ", ":America/Chicago")
    assert config.system_timezone() == "America/Chicago"
