"""Runtime configuration.

Every path is derived either from an environment variable or from the location of
this package on disk, so the project directory can be renamed or moved freely.

Environment variables (all optional):

    YOUTUBE_MCP_HOME             Base directory for credentials, settings and data. Default:
                                 - a source checkout (the folder holding pyproject.toml or
                                   youtube_mcp_server.py next to this package, and not inside
                                   site-packages): that folder, as before;
                                 - an installed package: a user-owned directory, never site-packages
                                   (macOS: ~/Library/Application Support/youtube-mcp,
                                   Linux: $XDG_DATA_HOME/youtube-mcp or ~/.local/share/youtube-mcp).
    YOUTUBE_MCP_CLIENT_SECRET    OAuth client file (default: $HOME/client_secret.json).
    YOUTUBE_MCP_TOKEN            OAuth token written by the authorize command
                                 (default: $HOME/token_youtube_mcp.json).
    YOUTUBE_MCP_DATA_DIR         Upload history and other local data (default: $HOME/data).
    YOUTUBE_MCP_DEFAULT_CATEGORY Default YouTube category id for uploads (default: 22, People & Blogs).
    YOUTUBE_MCP_LOG_LEVEL        Logging level for stderr logs (default: INFO).
    YOUTUBE_MCP_TIMEZONE         IANA timezone for scheduled publishing (default: this computer's
                                 timezone, or UTC if it cannot be determined).
    YOUTUBE_MCP_REGION           ISO 3166-1 region for video categories (default: US).

Every variable except YOUTUBE_MCP_HOME can also be set in an optional `youtube_mcp.env` file in
YOUTUBE_MCP_HOME (one KEY=VALUE per line, # comments allowed). It is read by both the MCP server and
the Finder Quick Action, so local preferences live in one place. Real environment variables win.
The file holds preferences only and is excluded from version control; never put secrets in it.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent
APP_DIR_NAME = "youtube-mcp"


def is_source_checkout(root: Path = PROJECT_ROOT) -> bool:
    """True when running from a project folder (not from an installed package in site-packages)."""
    if any(part in ("site-packages", "dist-packages") for part in root.parts):
        return False
    return (root / "pyproject.toml").is_file() or (root / "youtube_mcp_server.py").is_file()


def user_data_dir(platform: str = sys.platform, environ: os._Environ | dict = os.environ) -> Path:
    """Per-user persistent directory for an installed package (never inside site-packages)."""
    home = Path(environ.get("HOME") or Path.home())
    if platform == "darwin":
        return home / "Library" / "Application Support" / APP_DIR_NAME
    if platform.startswith("win"):
        return Path(environ.get("APPDATA") or home / "AppData" / "Roaming") / APP_DIR_NAME
    xdg = environ.get("XDG_DATA_HOME", "").strip()
    return (Path(xdg) if xdg else home / ".local" / "share") / APP_DIR_NAME


def default_home() -> Path:
    return PROJECT_ROOT if is_source_checkout() else user_data_dir()


def cli_command(module: str, script: str) -> list[str]:
    """How to run one of this project's command-line tools: the launcher script in a source checkout,
    otherwise `python -m <module>` (works for an installed package)."""
    launcher = PROJECT_ROOT / script
    if is_source_checkout() and launcher.is_file():
        return [sys.executable, str(launcher)]
    return [sys.executable, "-I", "-m", module]  # -I: the working folder can't shadow the installed code

# youtube.upload: videos.insert. youtube.force-ssl: read/update videos, thumbnails, channel info.
SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
)

# Optional, opt-in scopes (requested only by `authorize.py --analytics`). They are never required,
# so a token without them keeps working for every other tool. No monetary/revenue scope is used.
ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"
YOUTUBE_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
# YouTube Analytics API v2 accepts either of these read-only scopes (see the reports.query reference).
ANALYTICS_ACCEPTED_SCOPES: tuple[str, ...] = (ANALYTICS_SCOPE, YOUTUBE_READONLY_SCOPE)

# Safety: the default visibility is fixed in code and deliberately not configurable.
DEFAULT_VISIBILITY = "unlisted"
VISIBILITIES: tuple[str, ...] = ("private", "unlisted", "public")


SETTINGS_FILE_NAME = "youtube_mcp.env"
_SETTINGS_KEYS = ("YOUTUBE_MCP_CLIENT_SECRET", "YOUTUBE_MCP_TOKEN", "YOUTUBE_MCP_DATA_DIR",
                  "YOUTUBE_MCP_DEFAULT_CATEGORY", "YOUTUBE_MCP_LOG_LEVEL", "YOUTUBE_MCP_TIMEZONE",
                  "YOUTUBE_MCP_REGION", "YOUTUBE_MCP_CONFIRM_TIMEOUT")


def read_settings_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines; unknown keys and malformed lines are ignored."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if key in _SETTINGS_KEYS:
            out[key] = value.strip("'\"")
    return out


def system_timezone() -> str:
    """The computer's IANA timezone name (from $TZ or /etc/localtime), else UTC."""
    tz = os.environ.get("TZ", "").strip().lstrip(":")
    if tz and "/" in tz:
        return tz
    try:
        target = os.path.realpath("/etc/localtime")
    except OSError:
        return "UTC"
    marker = "zoneinfo/"
    return target.split(marker, 1)[1] if marker in target else "UTC"


def _value(var: str, file_values: dict[str, str]) -> str:
    return (os.environ.get(var, "").strip() or file_values.get(var, "")).strip()


def _env_path(var: str, default: Path, file_values: dict[str, str] | None = None) -> Path:
    value = _value(var, file_values or {})
    return Path(value).expanduser().resolve() if value else default


@dataclass(frozen=True)
class Settings:
    client_secret_file: Path
    token_file: Path
    data_dir: Path
    default_category_id: str = "22"  # People & Blogs (YouTube's own default)
    confirmation_ttl_seconds: int = 600
    default_timezone: str = "UTC"
    default_region: str = "US"

    @property
    def history_db(self) -> Path:
        return self.data_dir / "history.sqlite3"

    @classmethod
    def from_env(cls) -> "Settings":
        home = _env_path("YOUTUBE_MCP_HOME", default_home())
        fv = read_settings_file(home / SETTINGS_FILE_NAME)
        return cls(
            client_secret_file=_env_path("YOUTUBE_MCP_CLIENT_SECRET", home / "client_secret.json", fv),
            token_file=_env_path("YOUTUBE_MCP_TOKEN", home / "token_youtube_mcp.json", fv),
            data_dir=_env_path("YOUTUBE_MCP_DATA_DIR", home / "data", fv),
            default_category_id=_value("YOUTUBE_MCP_DEFAULT_CATEGORY", fv) or "22",
            default_timezone=_value("YOUTUBE_MCP_TIMEZONE", fv) or system_timezone(),
            default_region=(_value("YOUTUBE_MCP_REGION", fv) or "US").upper(),
        )


def setting(var: str) -> str:
    """One YOUTUBE_MCP_* value from the environment or the settings file (for settings read elsewhere)."""
    home = _env_path("YOUTUBE_MCP_HOME", default_home())
    return _value(var, read_settings_file(home / SETTINGS_FILE_NAME))
