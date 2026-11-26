#!/usr/bin/env python3
"""YouTubeMCP installer, upgrader and uninstaller. Standard library only.

Start it with ./install.sh (which finds a suitable Python), or directly:

    python3 install.py                       # install or upgrade (asks about optional parts)
    python3 install.py --with-quick-action   # also install the macOS Finder Quick Action
    python3 install.py --uninstall           # remove the app; your credentials and data are kept
    python3 install.py --help

What it does, in order (nothing needs sudo; everything stays in your home folder):
 1. checks the operating system, architecture and Python version;
 2. verifies the source files against SHA256SUMS (when present);
 3. builds an isolated Python environment from hash-locked, pre-built packages
    (pip --require-hashes --only-binary=:all:), so no package build scripts run;
 4. copies the app into the app folder and smoke-tests it (server start + all tool definitions);
 5. switches to the new version only after every step succeeded (the previous one is kept until then);
 6. optionally adds the server to an MCP client configuration (after backing it up) and installs the
    Quick Action.

Two separate folders are used:
    app folder:  code and Python environment; replaced on upgrade, removed on uninstall
    data folder: your OAuth client, token, settings, upload history and logs; never overwritten,
                 kept on uninstall unless you explicitly ask to delete it
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import fcntl
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

HERE = Path(__file__).resolve().parent
# The package lives next to this file in a source folder, or in lib/ inside an installed app folder.
for _candidate in (HERE, HERE / "lib"):
    if (_candidate / "youtube_mcp" / "config.py").is_file():
        sys.path.insert(0, str(_candidate))
        break

from youtube_mcp.config import SETTINGS_FILE_NAME, user_data_dir  # noqa: E402

APP_DIR_NAME = "youtube-mcp-app"
MANIFEST = "install-manifest.json"
SWITCH_JOURNAL = "switch.json"  # exists only while a validated new version is being switched in
MIN_PYTHON, MAX_PYTHON = (3, 10), (3, 13)  # accepted range
TESTED_PYTHONS = {(3, 12)}                  # versions this installer was actually tested with
SUPPORTED_SYSTEMS = {"Darwin"}              # tested
UNTESTED_SYSTEMS = {"Linux"}                # allowed only with --allow-untested-platform
EXPECTED_TOOLS = 26
CLIENT_ENTRY = "YouTube"
LOCK_FILE = "requirements.lock.txt"
PACKAGE = "youtube_mcp"


class InstallError(Exception):
    """A requirement is missing or a step failed. Nothing half-done is left active."""


# --- small helpers ---------------------------------------------------------------------------

def say(msg: str = "") -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"WARNING: {msg}", flush=True)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def source_version(src: Path) -> str:
    text = (src / PACKAGE / "__init__.py").read_text()
    for line in text.splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise InstallError(f"Could not read the version from {src / PACKAGE / '__init__.py'}.")


def version_tuple(v: str) -> tuple[int, ...]:
    out = []
    for part in v.split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def private_dir(path: Path) -> None:
    """Create a directory readable only by the user (never changes an existing one's contents)."""
    if not path.exists():
        path.mkdir(parents=True, mode=0o700)
    os.chmod(path, 0o700)


def remove_tree(path: Path, must_contain: str | None = None) -> None:
    """Delete a directory this installer created. Refuses anything that doesn't look like ours."""
    if not path.exists():
        return
    if must_contain and not (path / must_contain).exists():
        raise InstallError(f"Refusing to delete {path}: it does not look like a YouTubeMCP folder.")
    shutil.rmtree(path)


# --- environment checks ----------------------------------------------------------------------

@dataclass
class Platform:
    system: str = field(default_factory=platform.system)
    machine: str = field(default_factory=platform.machine)
    python: tuple[int, int] = field(default_factory=lambda: sys.version_info[:2])
    python_path: str = sys.executable


def check_platform(p: Platform, allow_untested: bool) -> list[str]:
    """Return warnings; raise InstallError when the platform can't be used."""
    notes = []
    if p.system not in SUPPORTED_SYSTEMS | UNTESTED_SYSTEMS:
        raise InstallError(f"{p.system} is not supported by this installer. Supported: macOS. "
                           "Linux is possible but untested (see docs/INSTALL.md).")
    if p.system in UNTESTED_SYSTEMS:
        if not allow_untested:
            raise InstallError(f"{p.system} has not been tested with this installer. To try anyway, re-run with "
                               "--allow-untested-platform. Public actions will always be refused there, because "
                               "the native confirmation dialog needs macOS.")
        notes.append(f"{p.system} is UNTESTED. Public actions will be refused (no native confirmation dialog), and "
                     "the Finder Quick Action, notifications and clipboard are unavailable.")
    if p.machine not in ("arm64", "x86_64", "aarch64", "AMD64"):
        notes.append(f"Architecture {p.machine} is untested; installation fails safely if no pre-built packages exist.")
    if not (MIN_PYTHON <= p.python <= MAX_PYTHON):
        found = ".".join(map(str, p.python))
        raise InstallError(
            f"Python {found} is not supported (need 3.10 to 3.13). On macOS, install Python 3.12 from "
            "https://www.python.org/downloads/ or with Homebrew (brew install python@3.12), then run ./install.sh "
            "again. Apple's built-in /usr/bin/python3 (3.9) is too old.")
    if p.python not in TESTED_PYTHONS:
        notes.append(f"Python {p.python[0]}.{p.python[1]} is accepted but this installer was only tested with 3.12.")
    return notes


def verify_sources(src: Path) -> str:
    """Check every file listed in SHA256SUMS, and that every package file is listed."""
    sums = src / "SHA256SUMS"
    if not sums.is_file():
        return "not verified (no SHA256SUMS: running from a development folder)"
    listed: dict[str, str] = {}
    for line in sums.read_text().splitlines():
        if not line.strip():
            continue
        digest, name = line.split(None, 1)
        name = name.strip().lstrip("*")
        if name.startswith("/") or ".." in Path(name).parts:
            raise InstallError(f"SHA256SUMS contains an unsafe path: {name}")
        listed[name] = digest
    for name, digest in listed.items():
        f = src / name
        if not f.is_file() or sha256_file(f) != digest:
            raise InstallError(f"Source file failed verification: {name}. Download the release again.")
    for f in (src / PACKAGE).rglob("*.py"):
        rel = f.relative_to(src).as_posix()
        if "__pycache__" not in rel and rel not in listed:
            raise InstallError(f"Unexpected file not covered by SHA256SUMS: {rel}")
    for required in (LOCK_FILE, "install.py"):
        if required not in listed:
            raise InstallError(f"SHA256SUMS does not cover {required}.")
    return f"verified ({len(listed)} files)"


# --- locations -------------------------------------------------------------------------------

@dataclass
class Layout:
    app: Path
    data: Path

    @property
    def venv(self) -> Path:
        return self.app / "venv"

    @property
    def python(self) -> Path:
        return self.venv / "bin" / "python"

    @property
    def lib(self) -> Path:
        return self.app / "lib"

    @property
    def bin(self) -> Path:
        return self.app / "bin"

    @property
    def manifest(self) -> Path:
        return self.app / MANIFEST

    @property
    def journal(self) -> Path:
        return self.app / SWITCH_JOURNAL


def default_layout(system: str | None = None, environ: dict | None = None) -> Layout:
    env = os.environ if environ is None else environ
    plat = {"Darwin": "darwin", "Linux": "linux"}.get(system or platform.system(), sys.platform)
    data = user_data_dir(plat, env)
    return Layout(app=data.parent / APP_DIR_NAME, data=data)


def read_manifest(layout: Layout) -> dict[str, Any] | None:
    try:
        return json.loads(layout.manifest.read_text())
    except (OSError, ValueError):
        return None


@contextlib.contextmanager
def install_lock(layout: Layout) -> Iterator[None]:
    layout.app.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(layout.app.parent / f".{APP_DIR_NAME}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstallError("Another YouTubeMCP install or uninstall is running. Wait for it to finish.") from None
        yield
    finally:
        os.close(fd)


def switch_over(layout: Layout, replace: list[str], manifest: dict[str, Any]) -> None:
    """Make a validated new version live: "<name>.new" replaces "<name>" for every name in `replace`.

    Writing the journal is the commit point. Before it, nothing live has been touched and recovery discards the
    ".new" folders. After it, recovery rolls forward to the new version. So an interruption at any moment leaves
    either the complete previous version or the complete new one: never new dependencies with old code (or the
    reverse), and never a manifest that describes something other than what is live.
    """
    _write_json_atomic(layout.journal, {"replace": replace, "manifest": manifest}, 0o600)
    _complete_switch(layout)


def _complete_switch(layout: Layout) -> None:
    """Finish a committed switch-over. Idempotent: safe to repeat after an interruption at any step."""
    try:
        journal = json.loads(layout.journal.read_text())
        replace, manifest = list(journal["replace"]), dict(journal["manifest"])
    except (OSError, ValueError, KeyError, TypeError):
        raise InstallError(f"{layout.journal} is unreadable, so the app folder's state is unknown. Uninstall "
                           "(your data is kept) and install again.") from None
    for name in replace:
        live, old, new = layout.app / name, layout.app / f"{name}.old", layout.app / f"{name}.new"
        if new.exists():
            if live.exists():
                if old.exists():
                    shutil.rmtree(old)
                live.rename(old)
            new.rename(live)
        elif not live.exists():
            raise InstallError(f"An interrupted upgrade left no usable '{name}' folder in {layout.app}. Uninstall "
                               "(your data is kept) and install again.")
    _write_json_atomic(layout.manifest, manifest, 0o644)
    for name in replace:
        shutil.rmtree(layout.app / f"{name}.old", ignore_errors=True)
    layout.journal.unlink()


def recover_interrupted(layout: Layout) -> list[str]:
    """Bring the app folder back to one complete version after an earlier run was interrupted."""
    if layout.journal.exists():
        _complete_switch(layout)
        return ["finished switching to the new version"]
    done = []
    for name in ("venv", "lib"):
        live, old, new = layout.app / name, layout.app / f"{name}.old", layout.app / f"{name}.new"
        if new.exists():  # never committed: the live version was not touched
            shutil.rmtree(new)
            done.append(f"removed unfinished {name}")
        # ".old" folders outside a switch-over only come from release-candidate installers.
        if old.exists():
            if live.exists():
                shutil.rmtree(old)
            else:
                old.rename(live)
                done.append(f"restored previous {name}")
    return done


# --- building ---------------------------------------------------------------------------------

def run(cmd: list[str], log: Path, env: dict[str, str] | None = None, timeout: int = 900,
        cwd: Path | None = None) -> None:
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"\n$ {' '.join(cmd)}\n")
        fh.flush()
        try:
            proc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, env=env, timeout=timeout,
                                  cwd=str(cwd or log.parent))
        except subprocess.TimeoutExpired:
            raise InstallError(f"Step timed out after {timeout // 60} minutes: {' '.join(cmd[:4])}... "
                               f"(details in {log}). Check your internet connection and run the installer "
                               "again.") from None
    if proc.returncode != 0:
        raise InstallError(f"Step failed: {' '.join(cmd[:4])}... (details in {log})")


def create_env(venv: Path, lock: Path, log: Path) -> None:
    """Create an isolated environment and install the hash-locked, pre-built dependencies."""
    run([sys.executable, "-m", "venv", str(venv)], log)
    py = str(venv / "bin" / "python")
    env = {**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1"}
    env.pop("PIP_INDEX_URL", None), env.pop("PIP_EXTRA_INDEX_URL", None)  # only the official index
    run([py, "-m", "pip", "install", "--require-hashes", "--only-binary=:all:", "--no-deps",
         "--index-url", "https://pypi.org/simple", "-r", str(lock)], log, env=env, timeout=3600)


def purelib(python: Path) -> Path:
    out = subprocess.run([str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                         capture_output=True, text=True, timeout=60, check=True, cwd=python.parent)
    return Path(out.stdout.strip())


def smoke_test(python: Path, lib: Path) -> int:
    """Start the real server over stdio with a throwaway home and count its tools."""
    probe = (
        "import json, subprocess, sys\n"
        "msgs = [{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18',"
        "'capabilities':{},'clientInfo':{'name':'installer','version':'0'}}},"
        "{'jsonrpc':'2.0','method':'notifications/initialized'},{'jsonrpc':'2.0','id':2,'method':'tools/list'}]\n"
        "server = 'import runpy, sys; sys.path.insert(0, ' + repr(LIB) + '); runpy.run_module(\"youtube_mcp\", run_name=\"__main__\")'\n"
        "p = subprocess.Popen([sys.executable, '-I', '-c', server], stdin=subprocess.PIPE, stdout=subprocess.PIPE,"
        " stderr=subprocess.DEVNULL, text=True, cwd=HOME)\n"
        "for m in msgs: p.stdin.write(json.dumps(m) + '\\n'); p.stdin.flush()\n"
        "tools = None\n"
        "for line in p.stdout:\n"
        "    r = json.loads(line)\n"
        "    if r.get('id') == 2: tools = r['result']['tools']; break\n"
        "p.stdin.close(); p.terminate(); p.wait(10)\n"
        "print(len(tools) if tools is not None else -1)\n"
    )
    with tempfile.TemporaryDirectory(prefix="youtube-mcp-smoke-") as home:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", home),
               "YOUTUBE_MCP_HOME": home, "YOUTUBE_MCP_LOG_LEVEL": "ERROR"}
        # -I (isolated): neither the current folder nor PYTHON* variables can change what gets imported.
        header = f"LIB = {str(lib)!r}\nHOME = {home!r}\n"
        try:
            out = subprocess.run([str(python), "-I", "-c", header + probe], capture_output=True, text=True,
                                 timeout=120, env=env, cwd=home)
        except subprocess.TimeoutExpired:
            raise InstallError("The server did not start within 2 minutes during the self-test.") from None
    try:
        return int(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise InstallError("The server failed its self-test (it did not list its tools).") from None


def write_launchers(layout: Layout) -> None:
    layout.bin.mkdir(exist_ok=True)
    py = layout.python
    scripts = {
        "youtube-mcp-server": f'exec "{py}" -I -m youtube_mcp "$@"',
        "youtube-mcp-authorize": f'exec "{py}" -I -m youtube_mcp.authorize "$@"',
        "youtube-mcp-uninstall": f'exec "{py}" -I "{layout.app / "install.py"}" --uninstall "$@"',
    }
    if platform.system() == "Darwin":
        scripts["youtube-mcp-upload-and-share"] = f'exec "{py}" -I -m youtube_mcp.quick_action "$@"'
    for name, body in scripts.items():
        path = layout.bin / name
        path.write_text(f"#!/bin/sh\n# Generated by the YouTubeMCP installer.\n{body}\n")
        os.chmod(path, 0o755)


# --- MCP client configuration ------------------------------------------------------------------

def client_entry(layout: Layout) -> dict[str, Any]:
    # -I: the client's working folder can never shadow the installed code.
    return {"command": str(layout.python), "args": ["-I", "-m", "youtube_mcp"]}


def claude_desktop_config(environ: dict | None = None) -> Path:
    home = Path((environ or os.environ).get("HOME") or Path.home())
    return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"


def _is_our_entry(entry: Any, layout: Layout) -> bool:
    return isinstance(entry, dict) and str(entry.get("command", "")).startswith(str(layout.app))


def _write_json_atomic(path: Path, data: dict[str, Any], mode: int) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def _backup(path: Path) -> Path:
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.backup-{stamp}")
    n = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.backup-{stamp}-{n}")
        n += 1
    shutil.copy2(path, backup)
    os.chmod(backup, 0o600)
    return backup


def _load_client_config(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except ValueError:
        raise InstallError(f"{path} is not valid JSON, so it was left unchanged. Fix it, or add the server "
                           "by hand (see docs/INSTALL.md).") from None
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
        raise InstallError(f"{path} has an unexpected structure, so it was left unchanged.")
    return data


def configure_client(path: Path, layout: Layout) -> str:
    """Add (or update our own) server entry. Other servers are never read out or changed."""
    entry = client_entry(layout)
    if path.exists():
        data = _load_client_config(path)
        servers = data.setdefault("mcpServers", {})
        current = servers.get(CLIENT_ENTRY)
        if current == entry:
            return f"{path}: already configured, unchanged"
        if current is not None and not _is_our_entry(current, layout):
            raise InstallError(
                f"{path} already has a server named '{CLIENT_ENTRY}' that points somewhere else, so it was left "
                "unchanged. Remove or rename that entry first if you want this installation to replace it.")
        backup = _backup(path)
        servers[CLIENT_ENTRY] = entry
        _write_json_atomic(path, data, path.stat().st_mode & 0o777)
        return f"{path}: added '{CLIENT_ENTRY}' (backup: {backup.name})"
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(path, {"mcpServers": {CLIENT_ENTRY: entry}}, 0o600)
    return f"{path}: created with '{CLIENT_ENTRY}'"


def unconfigure_client(path: Path, layout: Layout) -> str | None:
    if not path.exists():
        return None
    data = _load_client_config(path)
    servers = data.get("mcpServers", {})
    if not _is_our_entry(servers.get(CLIENT_ENTRY), layout):
        return None
    backup = _backup(path)
    del servers[CLIENT_ENTRY]
    _write_json_atomic(path, data, path.stat().st_mode & 0o777)
    return f"{path}: removed '{CLIENT_ENTRY}' (backup: {backup.name})"


def generic_client_snippet(layout: Layout) -> str:
    return json.dumps({"mcpServers": {CLIENT_ENTRY: client_entry(layout)}}, indent=2)


# --- Quick Action --------------------------------------------------------------------------------

def quick_action(layout: Layout, action: str, services_dir: Path | None, log: Path) -> str:
    """Install or remove the Quick Action using the installed app's own code."""
    code = (
        "import sys; from pathlib import Path; from youtube_mcp import quick_action as qa\n"
        f"d = Path({str(services_dir)!r}) if {services_dir is not None!r} else qa.SERVICES_DIR\n"
        f"sys.exit(qa.{action}(d))\n"
    )
    run([str(layout.python), "-I", "-c", code], log, cwd=layout.app)
    return action


def quick_action_is_ours(layout: Layout, services_dir: Path | None) -> bool:
    out = subprocess.run(
        [str(layout.python), "-I", "-c",
         "import sys; from pathlib import Path; from youtube_mcp import quick_action as qa\n"
         f"d = Path({str(services_dir)!r}) if {services_dir is not None!r} else qa.SERVICES_DIR\n"
         "print(qa.workflow_command(d) or '')\n"],
        capture_output=True, text=True, timeout=60, cwd=layout.app)
    return str(layout.python) in out.stdout


# --- install / uninstall ---------------------------------------------------------------------

@dataclass
class Options:
    with_quick_action: bool = False
    configure_claude_desktop: bool = False
    allow_untested_platform: bool = False
    allow_downgrade: bool = False
    rebuild_env: bool = False
    delete_data: bool = False
    assume_yes: bool = False
    interactive: bool = False
    services_dir: Path | None = None  # for tests; default ~/Library/Services


def ask(question: str, default: bool = False) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        answer = input(question + suffix).strip().lower()
    except EOFError:
        return default
    return default if not answer else answer in ("y", "yes")


def install(opts: Options, layout: Layout | None = None, src: Path = HERE,
            build_env: Callable[[Path, Path, Path], None] = create_env, plat: Platform | None = None) -> int:
    plat = plat or Platform()
    layout = layout or default_layout(plat.system)
    version = source_version(src)
    say(f"YouTubeMCP {version} installer")
    say(f"  System: {plat.system} {plat.machine}, Python {'.'.join(map(str, plat.python))} ({plat.python_path})")
    for note in check_platform(plat, opts.allow_untested_platform):
        warn(note)
    if not (src / PACKAGE / "server.py").is_file() or not (src / LOCK_FILE).is_file():
        raise InstallError(f"{src} is not a complete YouTubeMCP source folder.")
    say(f"  Source files: {verify_sources(src)}")
    say(f"  App folder:   {layout.app}")
    say(f"  Data folder:  {layout.data}  (your credentials and history; never overwritten)")

    if opts.interactive and plat.system == "Darwin":
        if not opts.with_quick_action:
            opts.with_quick_action = ask("Install the optional Finder Quick Action 'Upload & Share'?")
        if not opts.configure_claude_desktop and claude_desktop_config().parent.is_dir():
            opts.configure_claude_desktop = ask("Claude Desktop was found. Add YouTubeMCP to its configuration "
                                                "(a backup is made first)?")

    with install_lock(layout):
        private_dir(layout.data)
        layout.app.mkdir(parents=True, exist_ok=True)
        for note in recover_interrupted(layout):
            say(f"  Recovered from an interrupted run: {note}")
        log = layout.app / "install.log"
        previous = read_manifest(layout)
        if previous:
            old, new = previous.get("version", "0"), version
            if version_tuple(new) < version_tuple(old) and not opts.allow_downgrade:
                raise InstallError(f"Version {old} is installed; this is the older {new}. Use --allow-downgrade "
                                   "to install it anyway.")
            say(f"  Installed version: {old} -> {'reinstalling' if old == new else 'upgrading to'} {new}")

        lock = src / LOCK_FILE
        lock_hash = sha256_file(lock)
        python_id = f"{plat.python_path}|{'.'.join(map(str, plat.python))}"
        reuse = (previous and not opts.rebuild_env and previous.get("lock_sha256") == lock_hash
                 and previous.get("python") == python_id and layout.python.exists())
        new_venv, new_lib = layout.app / "venv.new", layout.app / "lib.new"
        try:
            if reuse:
                say("  Python environment: unchanged, reused")
                venv_dir = layout.venv
            else:
                say("  Python environment: building (downloads pinned, hash-checked packages from PyPI)...")
                build_env(new_venv, lock, log)
                venv_dir = new_venv
            shutil.copytree(src / PACKAGE, new_lib / PACKAGE,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            python = venv_dir / "bin" / "python"
            tools = smoke_test(python, new_lib)
            if tools != EXPECTED_TOOLS:
                raise InstallError(f"Self-test failed: the server listed {tools} tools, expected {EXPECTED_TOOLS}.")
            say(f"  Self-test: server started and listed all {tools} tools")
            # Point the environment at the final code location (same text whether or not it is reused), so the
            # new environment is complete before anything live is touched.
            (purelib(python) / "youtube_mcp_app.pth").write_text(str(layout.lib) + "\n")
        except BaseException:
            shutil.rmtree(new_venv, ignore_errors=True)
            shutil.rmtree(new_lib, ignore_errors=True)
            raise

        manifest = {"version": version, "python": python_id, "lock_sha256": lock_hash,
                    "installed_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
                    "data_dir": str(layout.data),
                    "quick_action": bool(opts.with_quick_action or (previous or {}).get("quick_action"))}
        switch_over(layout, ["lib"] if reuse else ["venv", "lib"], manifest)
        shutil.copy2(src / "install.py", layout.app / "install.py")
        shutil.copy2(lock, layout.app / LOCK_FILE)
        write_launchers(layout)
        example = src / f"{SETTINGS_FILE_NAME}.example"
        if example.is_file() and not (layout.data / example.name).exists():
            shutil.copy2(example, layout.data / example.name)  # an example only; never the settings file

        if opts.with_quick_action:
            if plat.system != "Darwin":
                warn("The Quick Action is macOS-only; skipped.")
                manifest["quick_action"] = False
            else:
                quick_action(layout, "install", opts.services_dir, log)
                say("  Finder Quick Action: installed (no keyboard shortcut is assigned)")
            _write_json_atomic(layout.manifest, manifest, 0o644)
        if opts.configure_claude_desktop:
            say("  " + configure_client(claude_desktop_config(), layout))

    print_next_steps(layout, opts)
    return 0


def print_next_steps(layout: Layout, opts: Options) -> None:
    secret = layout.data / "client_secret.json"
    token = layout.data / "token_youtube_mcp.json"
    say("\nInstalled.\n")
    say("Next steps:")
    if not secret.exists():
        say(f"  1. Create your own Google OAuth client (docs/SETUP.md, step 2) and save it as:\n       {secret}")
    else:
        say(f"  1. OAuth client found: {secret}")
    if not token.exists():
        say(f"  2. Sign in once:\n       \"{layout.bin / 'youtube-mcp-authorize'}\"")
    else:
        say(f"  2. Already signed in. Check with: \"{layout.bin / 'youtube-mcp-authorize'}\" --status")
    if opts.configure_claude_desktop:
        say("  3. Fully quit and restart Claude Desktop.")
    else:
        say("  3. Add the server to your MCP client. Most clients accept this JSON (use full paths as shown):")
        say("\n".join("       " + line for line in generic_client_snippet(layout).splitlines()))
    say(f"\nUninstall later with: \"{layout.bin / 'youtube-mcp-uninstall'}\" (keeps your credentials and data).")


def uninstall(opts: Options, layout: Layout | None = None) -> int:
    layout = layout or default_layout()
    manifest = read_manifest(layout)
    if manifest is None and not layout.app.exists():
        say(f"YouTubeMCP is not installed in {layout.app}. Nothing to do.")
        return 0
    with install_lock(layout):
        log = Path(tempfile.gettempdir()) / "youtube-mcp-uninstall.log"
        if layout.python.exists() and platform.system() == "Darwin":
            if quick_action_is_ours(layout, opts.services_dir):
                quick_action(layout, "uninstall", opts.services_dir, log)
                say("Removed the Finder Quick Action.")
        result = unconfigure_client(claude_desktop_config(), layout)
        if result:
            say(result)
        remove_tree(layout.app, must_contain=MANIFEST if manifest is not None else "venv")
        say(f"Removed the app folder: {layout.app}")
        if opts.delete_data:
            confirmed = opts.assume_yes or (opts.interactive and input(
                f"Type DELETE to permanently delete your credentials, token, settings and upload history in "
                f"{layout.data}: ").strip() == "DELETE")
            if confirmed and layout.data.exists():
                shutil.rmtree(layout.data)
                say(f"Deleted the data folder: {layout.data}")
            else:
                say(f"Kept your data folder: {layout.data}")
        else:
            say(f"Kept your data folder (credentials, settings, upload history): {layout.data}")
            say("To revoke Google access, remove the app at https://myaccount.google.com/permissions")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="install.py", description="Install, upgrade or uninstall YouTubeMCP.")
    ap.add_argument("--with-quick-action", action="store_true", help="also install the macOS Finder Quick Action")
    ap.add_argument("--configure-claude-desktop", action="store_true",
                    help="add the server to Claude Desktop's configuration (backed up first)")
    ap.add_argument("--print-client-config", action="store_true", help="only print the MCP client JSON and exit")
    ap.add_argument("--non-interactive", action="store_true", help="never ask questions (optional parts stay off)")
    ap.add_argument("--allow-untested-platform", action="store_true", help="try installing on Linux (untested)")
    ap.add_argument("--allow-downgrade", action="store_true", help="install an older version over a newer one")
    ap.add_argument("--rebuild-env", action="store_true", help="rebuild the Python environment from scratch")
    ap.add_argument("--uninstall", action="store_true", help="remove the app (your data is kept)")
    ap.add_argument("--delete-data", action="store_true",
                    help="with --uninstall: also delete credentials, settings and history (asks you to type DELETE)")
    ap.add_argument("--yes", action="store_true", help="with --delete-data: skip the typed confirmation")
    args = ap.parse_args(argv)
    opts = Options(with_quick_action=args.with_quick_action, configure_claude_desktop=args.configure_claude_desktop,
                   allow_untested_platform=args.allow_untested_platform, allow_downgrade=args.allow_downgrade,
                   rebuild_env=args.rebuild_env, delete_data=args.delete_data, assume_yes=args.yes,
                   interactive=sys.stdin.isatty() and not args.non_interactive)
    try:
        if args.print_client_config:
            say(generic_client_snippet(default_layout()))
            return 0
        if args.uninstall:
            return uninstall(opts)
        return install(opts)
    except InstallError as err:
        print(f"\nINSTALLATION STOPPED: {err}\nNothing was changed in your data folder.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled. Run the installer again to finish; it recovers automatically.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
