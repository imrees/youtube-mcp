"""Installer tests (install.py). Offline and fast: the isolated environment is a real `venv`, but instead of
downloading packages it borrows this test environment's already-installed dependencies through a .pth file.
The real, networked installation is exercised separately (docs/INSTALL_TEST_REPORT.md)."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import venv as venv_mod
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("yt_installer", ROOT / "install.py")
installer = importlib.util.module_from_spec(spec)
sys.modules["yt_installer"] = installer  # dataclasses look the module up while it loads
spec.loader.exec_module(installer)

DEV_SITE = sysconfig.get_paths()["purelib"]
macos_only = pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only behaviour")


class Builder:
    """Stands in for the networked pip step; counts how often an environment is built."""

    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def __call__(self, venv: Path, lock: Path, log: Path) -> None:
        self.calls += 1
        venv_mod.create(venv, with_pip=False, symlinks=True)
        if self.fail:
            raise installer.InstallError("simulated download failure")
        site = installer.purelib(venv / "bin" / "python")
        site.mkdir(parents=True, exist_ok=True)
        (site / "test_borrowed_deps.pth").write_text(DEV_SITE + "\n")


def source_copy(tmp: Path, version: str | None = None, lock_extra: str = "") -> Path:
    src = tmp / f"src-{version or 'cur'}-{len(lock_extra)}"
    if src.exists():
        return src
    src.mkdir()
    for name in ("install.py", "requirements.lock.txt", "youtube_mcp.env.example"):
        shutil.copy2(ROOT / name, src / name)
    shutil.copytree(ROOT / "youtube_mcp", src / "youtube_mcp", ignore=shutil.ignore_patterns("__pycache__"))
    if version:
        (src / "youtube_mcp" / "__init__.py").write_text(f'__version__ = "{version}"\n')
    if lock_extra:
        with (src / "requirements.lock.txt").open("a") as fh:
            fh.write(lock_extra)
    return src


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    for key in [k for k in os.environ if k.startswith("YOUTUBE_MCP_")]:
        monkeypatch.delenv(key)
    return h


def layout(home: Path):
    return installer.default_layout("Darwin", {"HOME": str(home)})


def do_install(home, src=None, builder=None, **opts):
    builder = builder or Builder()
    lay = layout(home)
    code = installer.install(installer.Options(**opts), layout=lay, src=src or ROOT, build_env=builder)
    return code, lay, builder


def run_installed(lay, code: str, home: Path) -> subprocess.CompletedProcess:
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    # Run from the source folder on purpose: the installed code must still be the one imported.
    return subprocess.run([str(lay.python), "-I", "-c", code], capture_output=True, text=True, env=env, timeout=60,
                          cwd=ROOT)


def seed_user_data(lay) -> dict[str, bytes]:
    lay.data.mkdir(parents=True, exist_ok=True)
    files = {"client_secret.json": b'{"installed": {"client_id": "x"}}', "token_youtube_mcp.json": b'{"t": 1}',
             "youtube_mcp.env": b"YOUTUBE_MCP_REGION=GB\n", "data/history.sqlite3": b"history-bytes"}
    for rel, content in files.items():
        (lay.data / rel).parent.mkdir(parents=True, exist_ok=True)
        (lay.data / rel).write_bytes(content)
    return files


def assert_data_unchanged(lay, files):
    for rel, content in files.items():
        assert (lay.data / rel).read_bytes() == content, rel


# --- fresh, repeated, upgrade --------------------------------------------------------------------------

def test_fresh_install_layout_and_self_test(home, capsys):
    code, lay, builder = do_install(home)
    out = capsys.readouterr().out
    assert code == 0 and builder.calls == 1
    assert "listed all 26 tools" in out
    assert lay.app == home / "Library" / "Application Support" / "youtube-mcp-app"
    assert lay.data == home / "Library" / "Application Support" / "youtube-mcp"
    assert (lay.data.stat().st_mode & 0o777) == 0o700
    assert sorted(p.name for p in lay.data.iterdir()) == ["youtube_mcp.env.example"]  # nothing else written
    manifest = json.loads(lay.manifest.read_text())
    assert manifest["version"] == installer.source_version(ROOT) and manifest["quick_action"] is False
    for name in ("youtube-mcp-server", "youtube-mcp-authorize", "youtube-mcp-uninstall"):
        assert os.access(lay.bin / name, os.X_OK)
    assert not any(p.name in ("client_secret.json", "token_youtube_mcp.json") for p in lay.app.rglob("*"))
    assert str(lay.data / "client_secret.json") in out and "mcpServers" in out  # next steps


def test_installed_app_uses_user_data_dir_never_the_install(home):
    _, lay, _ = do_install(home)
    out = run_installed(lay, "from youtube_mcp import config as c; s=c.Settings.from_env(); "
                             "print(c.is_source_checkout(), s.token_file, s.data_dir, c.PROJECT_ROOT, sep='|')", home)
    is_src, token, data, root = out.stdout.strip().split("|")
    assert is_src == "False" and root == str(lay.lib)
    assert token == str(lay.data / "token_youtube_mcp.json") and data == str(lay.data / "data")
    assert not token.startswith(str(lay.app))


def test_missing_credentials_give_clear_instructions(home):
    _, lay, _ = do_install(home)
    out = run_installed(lay, (
        "from youtube_mcp.server import create_service\n"
        "from youtube_mcp.errors import YouTubeMCPError\n"
        "try:\n    create_service().get_channel_info()\n"
        "except YouTubeMCPError as e:\n    print(e.code); print(e.action)\n"), home)
    code, action = out.stdout.strip().splitlines()[:2]
    assert code == "not_authorized" and "-I -m youtube_mcp.authorize" in action and str(lay.python) in action
    status = subprocess.run([str(lay.bin / "youtube-mcp-authorize"), "--status"], capture_output=True, text=True,
                            env={"HOME": str(home), "PATH": "/usr/bin:/bin"}, timeout=60, cwd=ROOT)
    assert "client_secret" in (status.stdout + status.stderr) and "Traceback" not in status.stderr


def test_repeated_install_reuses_environment_and_keeps_data(home):
    _, lay, b1 = do_install(home)
    files = seed_user_data(lay)
    code, _, b2 = do_install(home)
    assert code == 0 and b2.calls == 0  # nothing rebuilt
    assert_data_unchanged(lay, files)


def test_upgrade_replaces_app_keeps_data_and_rebuilds_only_when_lock_changes(home, tmp_path, capsys):
    _, lay, _ = do_install(home)
    files = seed_user_data(lay)
    code, _, b = do_install(home, src=source_copy(tmp_path, "1.0.1"))
    assert code == 0 and b.calls == 0 and "upgrading to 1.0.1" in capsys.readouterr().out
    assert json.loads(lay.manifest.read_text())["version"] == "1.0.1"
    assert '"1.0.1"' in (lay.lib / "youtube_mcp" / "__init__.py").read_text()
    code, _, b = do_install(home, src=source_copy(tmp_path, "1.0.2", lock_extra="# changed\n"))
    assert code == 0 and b.calls == 1  # new dependency set -> new environment
    assert not (lay.app / "venv.old").exists() and not (lay.app / "lib.old").exists()
    assert_data_unchanged(lay, files)


def test_downgrade_is_refused_unless_explicit(home, tmp_path):
    do_install(home, src=source_copy(tmp_path, "9.0.0"))
    with pytest.raises(installer.InstallError, match="older"):
        do_install(home, src=source_copy(tmp_path, "1.0.0"))
    assert do_install(home, src=source_copy(tmp_path, "1.0.0"), allow_downgrade=True)[0] == 0


# --- failure recovery -----------------------------------------------------------------------------------

def test_failed_build_leaves_previous_install_working(home, tmp_path):
    _, lay, _ = do_install(home)
    files = seed_user_data(lay)
    before = json.loads(lay.manifest.read_text())
    with pytest.raises(installer.InstallError, match="simulated"):
        do_install(home, src=source_copy(tmp_path, "1.0.1", lock_extra="# new\n"), builder=Builder(fail=True))
    assert json.loads(lay.manifest.read_text()) == before
    assert not (lay.app / "venv.new").exists() and not (lay.app / "lib.new").exists()
    assert run_installed(lay, "import youtube_mcp; print(youtube_mcp.__version__)", home).stdout.strip() == "1.0.0"
    assert_data_unchanged(lay, files)


def test_broken_new_version_fails_self_test_and_is_not_activated(home, tmp_path):
    _, lay, _ = do_install(home)
    bad = source_copy(tmp_path, "1.0.1")
    (bad / "youtube_mcp" / "server.py").write_text("raise SystemExit('broken')\n")
    with pytest.raises(installer.InstallError, match="(?i)self-test"):
        do_install(home, src=bad)
    assert json.loads(lay.manifest.read_text())["version"] == "1.0.0"
    assert "raise SystemExit" not in (lay.lib / "youtube_mcp" / "server.py").read_text()


def test_interrupted_switch_over_is_recovered(home):
    _, lay, _ = do_install(home)
    lay.venv.rename(lay.app / "venv.old")  # crash after moving the old environment aside
    (lay.app / "lib.new").mkdir()
    assert sorted(installer.recover_interrupted(lay)) == ["removed unfinished lib", "restored previous venv"]
    assert lay.python.exists() and not (lay.app / "lib.new").exists()
    assert do_install(home)[0] == 0


class SimulatedCrash(BaseException):
    """A hard stop (power loss, kill -9): nothing after it runs in the interrupted process."""


class MarkedBuilder(Builder):
    """Records which dependency lock each environment was built from, so tests can tell environments apart."""

    def __call__(self, venv: Path, lock: Path, log: Path) -> None:
        super().__call__(venv, lock, log)
        (venv / "built-from-lock").write_text(installer.sha256_file(lock))


def assert_consistent(lay, home):
    """The live environment, the live code and the manifest must all belong to the same installation."""
    manifest = json.loads(lay.manifest.read_text())
    assert (lay.venv / "built-from-lock").read_text() == manifest["lock_sha256"], "environment != manifest"
    assert f'"{manifest["version"]}"' in (lay.lib / "youtube_mcp" / "__init__.py").read_text(), "code != manifest"
    out = run_installed(lay, "import youtube_mcp; print(youtube_mcp.__version__)", home)
    assert out.stdout.strip() == manifest["version"], out.stderr
    leftovers = [p.name for p in lay.app.iterdir() if p.name.endswith((".new", ".old")) or p.name == "switch.json"]
    assert leftovers == []
    return manifest["version"]


# (dependency lock changes?, crash point, version that must be live after recovery). A crash point is "before the
# switch-over is committed", "the n-th rename of the switch-over", "clean-up of the previous version", or "after
# the new manifest was written".
CRASH_CASES = [
    (True, "commit", "1.0.0"), (True, "rename-1", "1.1.0"), (True, "rename-2", "1.1.0"), (True, "rename-3", "1.1.0"),
    (True, "rename-4", "1.1.0"), (True, "cleanup", "1.1.0"), (True, "journal-removal", "1.1.0"),
    # Same dependency lock: the environment is reused and only the code is switched.
    (False, "commit", "1.0.0"), (False, "rename-1", "1.1.0"), (False, "rename-2", "1.1.0"), (False, "cleanup", "1.1.0"),
]


@pytest.mark.parametrize("lock_changes, crash_at, expected", CRASH_CASES)
def test_interruption_at_any_point_of_an_upgrade_never_mixes_code_and_dependencies(
        home, tmp_path, monkeypatch, lock_changes, crash_at, expected):
    """Regression (RC3 review): a crash after the new environment went live but before the new code did used to
    be 'recovered' into new dependencies + old code + old manifest. Every interruption point must now recover to
    one complete version: entirely the old one (before the commit point) or entirely the new one (after it)."""
    _, lay, _ = do_install(home, src=source_copy(tmp_path, "1.0.0"), builder=MarkedBuilder())
    files = seed_user_data(lay)
    new_src = source_copy(tmp_path, "1.1.0", lock_extra="# new dependency set\n" if lock_changes else "")

    kind, _, n = crash_at.partition("-")
    real_rename, real_rmtree, real_unlink = Path.rename, installer.shutil.rmtree, Path.unlink
    real_write_json = installer._write_json_atomic
    renames = []

    def crashing_rename(self, target):
        if Path(self).parent == lay.app:
            renames.append(self)
            if kind == "rename" and len(renames) == int(n):
                raise SimulatedCrash(crash_at)
        return real_rename(self, target)

    def crashing_rmtree(path, *args, **kwargs):
        if kind == "cleanup" and Path(path).name.endswith(".old"):
            raise SimulatedCrash(crash_at)
        return real_rmtree(path, *args, **kwargs)

    def crashing_unlink(self, *args, **kwargs):
        if kind == "journal" and Path(self).name == "switch.json":
            raise SimulatedCrash(crash_at)
        return real_unlink(self, *args, **kwargs)

    def crashing_write_json(path, data, mode):
        if kind == "commit" and Path(path).name == "switch.json":
            raise SimulatedCrash(crash_at)
        return real_write_json(path, data, mode)

    monkeypatch.setattr(Path, "rename", crashing_rename)
    monkeypatch.setattr(Path, "unlink", crashing_unlink)
    monkeypatch.setattr(installer.shutil, "rmtree", crashing_rmtree)
    monkeypatch.setattr(installer, "_write_json_atomic", crashing_write_json)
    with pytest.raises(SimulatedCrash):
        do_install(home, src=new_src, builder=MarkedBuilder())
    monkeypatch.undo()
    if kind == "rename":
        assert len(renames) == int(n)  # the crash really happened at that rename

    installer.recover_interrupted(lay)
    assert assert_consistent(lay, home) == expected
    assert_data_unchanged(lay, files)
    # Running the installer again finishes the upgrade from either state.
    assert do_install(home, src=new_src, builder=MarkedBuilder())[0] == 0
    assert assert_consistent(lay, home) == "1.1.0"
    assert_data_unchanged(lay, files)


def test_interrupted_upgrade_is_not_reused_by_the_previous_installer(home, tmp_path, monkeypatch):
    """Regression: the previous version's installer must not 'reuse' an environment that was built for the new
    version, just because the manifest had not been updated yet."""
    old_src = source_copy(tmp_path, "1.0.0")
    _, lay, _ = do_install(home, src=old_src, builder=MarkedBuilder())
    real_rmtree = installer.shutil.rmtree

    def crash_on_cleanup(path, *args, **kwargs):
        if Path(path).name.endswith(".old"):
            raise SimulatedCrash("cleanup")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(installer.shutil, "rmtree", crash_on_cleanup)
    with pytest.raises(SimulatedCrash):
        do_install(home, src=source_copy(tmp_path, "1.1.0", lock_extra="# new\n"), builder=MarkedBuilder())
    monkeypatch.undo()
    # The upgrade had completed its switch-over, so recovery finishes it, and the old installer sees 1.1.0.
    with pytest.raises(installer.InstallError, match="older"):
        do_install(home, src=old_src, builder=MarkedBuilder())
    assert assert_consistent(lay, home) == "1.1.0"
    assert do_install(home, src=old_src, builder=MarkedBuilder(), allow_downgrade=True)[0] == 0
    assert assert_consistent(lay, home) == "1.0.0"


def test_concurrent_installs_are_refused(home):
    lay = layout(home)
    with installer.install_lock(lay):
        with pytest.raises(installer.InstallError, match="Another"):
            with installer.install_lock(lay):
                pass


# --- inputs and platform -------------------------------------------------------------------------------

def test_sha256sums_verification(tmp_path):
    src = source_copy(tmp_path)
    rels = ["install.py", "requirements.lock.txt"] + [p.relative_to(src).as_posix()
                                                     for p in (src / "youtube_mcp").rglob("*.py")]
    sums = "".join(f"{installer.sha256_file(src / r)}  {r}\n" for r in rels)
    (src / "SHA256SUMS").write_text(sums)
    assert installer.verify_sources(src).startswith("verified")
    (src / "youtube_mcp" / "extra.py").write_text("x = 1\n")
    with pytest.raises(installer.InstallError, match="not covered"):
        installer.verify_sources(src)
    (src / "youtube_mcp" / "extra.py").unlink()
    (src / "youtube_mcp" / "server.py").write_text("tampered\n")
    with pytest.raises(installer.InstallError, match="failed verification"):
        installer.verify_sources(src)
    (src / "SHA256SUMS").write_text(sums + "0" * 64 + "  ../outside\n")
    with pytest.raises(installer.InstallError, match="unsafe"):
        installer.verify_sources(src)


def test_platform_checks():
    P = installer.Platform
    assert installer.check_platform(P("Darwin", "arm64", (3, 12), "py"), False) == []
    assert "only tested with 3.12" in installer.check_platform(P("Darwin", "x86_64", (3, 11), "py"), False)[0]
    for bad in (P("Darwin", "arm64", (3, 9), "py"), P("Darwin", "arm64", (3, 14), "py"), P("Windows", "AMD64", (3, 12), "py")):
        with pytest.raises(installer.InstallError):
            installer.check_platform(bad, False)
    with pytest.raises(installer.InstallError, match="allow-untested-platform"):
        installer.check_platform(P("Linux", "x86_64", (3, 12), "py"), False)
    assert "UNTESTED" in installer.check_platform(P("Linux", "x86_64", (3, 12), "py"), True)[0]


@pytest.mark.skipif(not Path("/usr/bin/python3").exists(), reason="needs an old system Python")
def test_too_old_python_fails_safely_with_instructions(home):
    out = subprocess.run(["/usr/bin/python3", str(ROOT / "install.py"), "--non-interactive"], capture_output=True,
                         text=True, env={"HOME": str(home), "PATH": "/usr/bin:/bin"}, timeout=60)
    version = subprocess.run(["/usr/bin/python3", "-c", "import sys;print(sys.version_info[:2] >= (3, 10))"],
                             capture_output=True, text=True).stdout.strip()
    if version == "True":
        pytest.skip("system Python is new enough")
    assert out.returncode == 1 and "not supported" in out.stderr and "python.org" in out.stderr
    lay = layout(home)
    assert not lay.app.exists() and not lay.data.exists()  # nothing of ours was created


def test_native_public_confirmation_fails_closed_off_macos(monkeypatch):
    from youtube_mcp import native_confirm as nc
    from youtube_mcp.errors import YouTubeMCPError

    monkeypatch.setattr(nc.sys, "platform", "linux")
    with pytest.raises(YouTubeMCPError) as exc:
        nc.macos_confirm_public("Make a video PUBLIC", [("Video title", "x")])
    assert exc.value.code == "native_confirmation_unavailable"


# --- MCP client configuration ---------------------------------------------------------------------------

def test_client_config_created_or_merged_with_backup_and_no_leaks(home, capsys):
    lay = layout(home)
    cfg = installer.claude_desktop_config({"HOME": str(home)})
    assert installer.configure_client(cfg, lay).endswith("created with 'YouTube'")
    assert json.loads(cfg.read_text())["mcpServers"]["YouTube"]["args"] == ["-I", "-m", "youtube_mcp"]
    cfg.write_text(json.dumps({"mcpServers": {"Other": {"command": "x", "env": {"API_KEY": "super-secret-value"}}},
                               "preferences": {"a": 1}}))
    msg = installer.configure_client(cfg, lay)
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["Other"]["env"]["API_KEY"] == "super-secret-value" and data["preferences"] == {"a": 1}
    assert data["mcpServers"]["YouTube"] == installer.client_entry(lay)
    backups = list(cfg.parent.glob("claude_desktop_config.json.backup-*"))
    assert len(backups) == 1 and "YouTube" not in backups[0].read_text() and (backups[0].stat().st_mode & 0o777) == 0o600
    assert "super-secret-value" not in msg + capsys.readouterr().out
    assert installer.configure_client(cfg, lay).endswith("unchanged")  # repeat: no new backup
    assert len(list(cfg.parent.glob("*.backup-*"))) == 1
    assert installer.unconfigure_client(cfg, lay).startswith(str(cfg))
    assert "YouTube" not in json.loads(cfg.read_text())["mcpServers"] and "Other" in json.loads(cfg.read_text())["mcpServers"]


def test_client_config_never_overwrites_foreign_entry_or_invalid_json(home):
    lay = layout(home)
    cfg = installer.claude_desktop_config({"HOME": str(home)})
    cfg.parent.mkdir(parents=True)
    foreign = {"mcpServers": {"YouTube": {"command": "/Users/me/YouTubeMCP/.venv/bin/python"}}}
    cfg.write_text(json.dumps(foreign))
    with pytest.raises(installer.InstallError, match="points somewhere else"):
        installer.configure_client(cfg, lay)
    assert json.loads(cfg.read_text()) == foreign
    assert installer.unconfigure_client(cfg, lay) is None and json.loads(cfg.read_text()) == foreign
    cfg.write_text("{not json")
    with pytest.raises(installer.InstallError, match="not valid JSON"):
        installer.configure_client(cfg, lay)
    assert cfg.read_text() == "{not json" and not list(cfg.parent.glob("*.backup-*"))


def test_install_with_client_configuration(home):
    code, lay, _ = do_install(home, configure_claude_desktop=True)
    cfg = installer.claude_desktop_config({"HOME": str(home)})
    assert code == 0 and json.loads(cfg.read_text())["mcpServers"]["YouTube"]["command"] == str(lay.python)


# --- Quick Action and uninstall --------------------------------------------------------------------------

@macos_only
def test_quick_action_only_when_selected_and_removed_on_uninstall(home, tmp_path):
    services = tmp_path / "Services"
    _, lay, _ = do_install(home, services_dir=services)
    assert not services.exists()  # not selected: nothing installed
    _, lay, _ = do_install(home, services_dir=services, with_quick_action=True)
    wf = services / "Upload & Share.workflow"
    command = installer.quick_action_is_ours(lay, services)
    assert wf.exists() and command
    doc = (wf / "Contents" / "document.wflow").read_bytes()
    assert b"-I -m youtube_mcp.quick_action upload" in doc and b"upload_and_share.py" not in doc
    info = (wf / "Contents" / "Info.plist").read_bytes()
    assert b"NSKeyEquivalent" not in info  # no keyboard shortcut is assigned
    assert json.loads(lay.manifest.read_text())["quick_action"] is True
    installer.uninstall(installer.Options(services_dir=services), layout=lay)
    assert not wf.exists() and not lay.app.exists()


@macos_only
def test_uninstall_leaves_a_quick_action_from_another_installation(home, tmp_path):
    services = tmp_path / "Services"
    _, lay, _ = do_install(home)
    other = subprocess.run([sys.executable, "-c", (
        "import sys; sys.path.insert(0, %r); from pathlib import Path; from youtube_mcp import quick_action as qa; "
        "sys.exit(qa.install(Path(%r)))") % (str(ROOT), str(services))], capture_output=True, text=True)
    assert other.returncode == 0
    installer.uninstall(installer.Options(services_dir=services), layout=lay)
    assert (services / "Upload & Share.workflow").exists()


def test_uninstall_keeps_data_by_default_and_deletes_only_on_explicit_request(home):
    _, lay, _ = do_install(home, configure_claude_desktop=True)
    files = seed_user_data(lay)
    installer.uninstall(installer.Options(), layout=lay)
    assert not lay.app.exists()
    assert_data_unchanged(lay, files)
    cfg = installer.claude_desktop_config({"HOME": str(home)})
    assert "YouTube" not in json.loads(cfg.read_text())["mcpServers"]
    assert installer.uninstall(installer.Options(), layout=lay) == 0  # repeated uninstall is harmless
    _, lay, _ = do_install(home)
    assert_data_unchanged(lay, files)  # reinstall finds the same data
    installer.uninstall(installer.Options(delete_data=True), layout=lay)  # no typed confirmation -> kept
    assert_data_unchanged(lay, files)
    _, lay, _ = do_install(home)
    installer.uninstall(installer.Options(delete_data=True, assume_yes=True), layout=lay)
    assert not lay.data.exists()


def test_uninstall_refuses_folders_that_are_not_ours(home):
    lay = layout(home)
    lay.app.mkdir(parents=True)
    (lay.app / "precious.txt").write_text("keep")
    with pytest.raises(installer.InstallError, match="does not look like"):
        installer.uninstall(installer.Options(), layout=lay)
    assert (lay.app / "precious.txt").exists()


def test_installed_code_is_never_shadowed_by_the_working_folder(home):
    """Regression: `python -m youtube_mcp` used to import a youtube_mcp folder from the current directory."""
    _, lay, _ = do_install(home)
    entry = installer.client_entry(lay)
    out = subprocess.run([entry["command"], *entry["args"][:-2], "-c", "import youtube_mcp; print(youtube_mcp.__file__)"],
                         capture_output=True, text=True, cwd=ROOT, env={"HOME": str(home), "PATH": "/usr/bin:/bin"})
    assert out.stdout.strip().startswith(str(lay.lib))
    for name in ("youtube-mcp-server", "youtube-mcp-authorize", "youtube-mcp-upload-and-share"):
        if (lay.bin / name).exists():
            assert ' -I -m ' in (lay.bin / name).read_text()


def test_shell_bootstraps_are_valid_and_executable():
    for name in ("install.sh", "uninstall.sh"):
        assert os.access(ROOT / name, os.X_OK)
        assert subprocess.run(["sh", "-n", str(ROOT / name)]).returncode == 0
    for name in ("install.sh", "uninstall.sh", "install.py"):
        code_lines = [ln for ln in (ROOT / name).read_text().splitlines()
                      if ln.strip() and not ln.lstrip().startswith("#") and "needs sudo" not in ln]
        assert not any("sudo" in ln for ln in code_lines), name  # nothing ever runs sudo


def test_dependency_lock_is_fully_hash_pinned():
    lines = (ROOT / "requirements.lock.txt").read_text().splitlines()
    pins = [line for line in lines if "==" in line]
    assert pins and all(line.endswith("\\") for line in pins)  # every pin is followed by hashes
    assert sum("--hash=sha256:" in line for line in lines) >= len(pins)
