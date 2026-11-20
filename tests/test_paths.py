"""Path resolution: where the app reads code and where it writes data.

Frozen into an EXE those two stop being the same directory, and getting it
wrong is silent — logs and config land in a temp dir that is wiped on exit.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import paths  # noqa: E402


@pytest.fixture(autouse=True)
def clear_resolution():
    """app_home() memoizes; every test must resolve from scratch."""
    paths.reset_cache()
    yield
    paths.reset_cache()


@pytest.fixture
def frozen(monkeypatch):
    """Pretend we're a compiled binary."""
    monkeypatch.setattr(paths, "is_frozen", lambda: True)


# ------------------------------------------------------------ running from source

def test_source_run_uses_the_repo_root(monkeypatch):
    monkeypatch.delenv(paths.HOME_ENV_VAR, raising=False)
    assert paths.app_home() == Path(paths.__file__).resolve().parent.parent
    assert paths.config_path().name == "config.yaml"
    assert paths.config_path().parent.name == "config"


def test_every_data_dir_hangs_off_one_root(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.HOME_ENV_VAR, str(tmp_path))
    for d in (paths.logs_dir(), paths.traces_dir(), paths.dumps_dir(),
              paths.cache_dir(), paths.config_dir()):
        assert d.parent == tmp_path


# ------------------------------------------------------------------- overrides

def test_env_override_wins_over_everything(monkeypatch, tmp_path, frozen):
    monkeypatch.setenv(paths.HOME_ENV_VAR, str(tmp_path / "custom"))
    monkeypatch.setattr(paths, "executable_dir", lambda: tmp_path / "exe")
    assert paths.app_home() == tmp_path / "custom"


def test_override_expands_a_tilde(monkeypatch):
    monkeypatch.setenv(paths.HOME_ENV_VAR, "~/mt5data")
    assert paths.app_home() == Path.home() / "mt5data"


# --------------------------------------------------------------- frozen layout

def test_frozen_writes_next_to_the_exe(monkeypatch, tmp_path, frozen):
    """The portable case: C:\\mt5copy\\MetaCT.exe keeps its own config."""
    monkeypatch.delenv(paths.HOME_ENV_VAR, raising=False)
    exe_dir = tmp_path / "portable"
    exe_dir.mkdir()
    monkeypatch.setattr(paths, "executable_dir", lambda: exe_dir)
    assert paths.app_home() == exe_dir


def test_frozen_falls_back_when_the_exe_folder_is_read_only(
    monkeypatch, tmp_path, frozen
):
    """Program Files: unwritable, so data goes to the per-user dir instead."""
    monkeypatch.delenv(paths.HOME_ENV_VAR, raising=False)
    fallback = tmp_path / "localappdata" / "MetaCT"
    monkeypatch.setattr(paths, "executable_dir", lambda: tmp_path / "program files")
    monkeypatch.setattr(paths, "_is_writable", lambda d: False)
    monkeypatch.setattr(paths, "_fallback_home", lambda: fallback)
    assert paths.app_home() == fallback


def test_writability_is_probed_by_writing_not_by_os_access(tmp_path):
    """UAC virtualization makes os.access lie; only a real write is truthful."""
    assert paths._is_writable(tmp_path) is True
    assert paths._is_writable(Path("/proc/nonexistent-mt5copy/deep")) is False


def test_write_probe_leaves_nothing_behind(tmp_path):
    before = set(tmp_path.iterdir())
    paths._is_writable(tmp_path)
    assert set(tmp_path.iterdir()) == before


def test_resolution_is_memoized(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.HOME_ENV_VAR, str(tmp_path / "first"))
    first = paths.app_home()
    monkeypatch.setenv(paths.HOME_ENV_VAR, str(tmp_path / "second"))
    assert paths.app_home() == first, "must not change under a running process"


# ------------------------------------------------------------- executable_dir

def test_executable_dir_prefers_the_original_onefile_argv(monkeypatch, tmp_path):
    """Onefile: sys.executable is the temp unpack dir — data must not go there."""
    real = tmp_path / "real" / "MetaCT.exe"
    real.parent.mkdir(parents=True)
    real.write_text("", encoding="utf-8")

    class _Compiled:
        original_argv0 = str(real)
        containing_dir = str(tmp_path / "temp_unpack")

    monkeypatch.setitem(paths.__dict__, "__compiled__", _Compiled())
    monkeypatch.setattr(sys, "executable", str(tmp_path / "temp_unpack" / "x.exe"))
    assert paths.executable_dir() == real.parent


def test_executable_dir_falls_back_to_sys_executable(monkeypatch, tmp_path):
    exe = tmp_path / "dist" / "MetaCT.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")

    paths.__dict__.pop("__compiled__", None)
    monkeypatch.setattr(sys, "argv", [""])
    monkeypatch.setattr(sys, "executable", str(exe))
    assert paths.executable_dir() == exe.parent


# ------------------------------------------------------------------ resources

def test_icons_are_found_in_the_source_tree():
    icons = paths.icons_dir()
    assert icons.is_dir()
    assert (icons / "app_icon.ico").exists(), "the EXE icon the builder embeds"


def test_resources_are_separate_from_data(monkeypatch, tmp_path, frozen):
    """The whole point: a read-only bundle can't be where logs are written."""
    monkeypatch.setenv(paths.HOME_ENV_VAR, str(tmp_path / "data"))
    assert paths.resource_dir() != paths.app_home()


# -------------------------------------------------------------------- ensure

def test_ensure_dirs_creates_the_whole_tree(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.HOME_ENV_VAR, str(tmp_path / "home"))
    paths.ensure_dirs()
    for d in (paths.config_dir(), paths.logs_dir(), paths.traces_dir(),
              paths.dumps_dir(), paths.cache_dir()):
        assert d.is_dir()


def test_ensure_dirs_never_raises_on_a_bad_root(monkeypatch):
    """Called before logging exists — it must not be the thing that crashes."""
    monkeypatch.setenv(paths.HOME_ENV_VAR, "/proc/nonexistent-mt5copy/deep")
    paths.ensure_dirs()


def test_describe_reports_every_resolved_location(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.HOME_ENV_VAR, str(tmp_path))
    info = paths.describe()
    assert set(info) == {"frozen", "app_home", "resource_dir", "config",
                         "logs", "traces", "dumps", "cache"}
    assert info["app_home"] == str(tmp_path)


# ------------------------------------------------------- surviving the rename
#
# The app was called MetaCopier. On an upgraded machine its data directory
# still is, and adopting it is not a nicety: a fresh %LOCALAPPDATA%\MetaCT
# comes up with no accounts configured and no explanation, which is
# indistinguishable from a corrupted install.

def test_the_old_data_directory_is_adopted_when_only_it_exists(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    (tmp_path / "MetaCopier").mkdir()

    assert paths._fallback_home() == tmp_path / "MetaCopier"


def test_the_current_directory_wins_once_it_exists(monkeypatch, tmp_path):
    """Both present means the migration already happened; never split in two."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    (tmp_path / "MetaCopier").mkdir()
    (tmp_path / paths.APP_NAME).mkdir()

    assert paths._fallback_home() == tmp_path / paths.APP_NAME


def test_a_clean_machine_gets_the_current_name(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert paths._fallback_home() == tmp_path / paths.APP_NAME


def test_the_old_environment_override_is_still_honoured(monkeypatch, tmp_path):
    """Someone's launcher script sets MT5COPY_HOME; it must keep working."""
    monkeypatch.delenv(paths.HOME_ENV_VAR, raising=False)
    monkeypatch.setenv("MT5COPY_HOME", str(tmp_path))

    assert paths.app_home() == tmp_path


def test_the_current_override_wins_over_the_old_one(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.HOME_ENV_VAR, str(tmp_path / "new"))
    monkeypatch.setenv("MT5COPY_HOME", str(tmp_path / "old"))

    assert paths.app_home() == tmp_path / "new"


# --------------------------------------------------- consumers route through it

def test_consumers_default_to_the_shared_root(monkeypatch, tmp_path):
    """Nobody may walk up from their own __file__ any more."""
    monkeypatch.setenv(paths.HOME_ENV_VAR, str(tmp_path))
    paths.reset_cache()

    from src.gui.config_manager import ConfigManager

    assert ConfigManager().config_path == tmp_path / "config" / "config.yaml"


def test_no_module_resolves_data_paths_from_its_own_file():
    """A stray Path(__file__).parent.parent would silently break the EXE."""
    src = Path(paths.__file__).resolve().parent
    offenders = []
    for py in src.rglob("*.py"):
        if py.name == "paths.py":
            continue
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if "__file__" in line and not line.lstrip().startswith("#"):
                offenders.append(f"{py.relative_to(src)}:{i}")
    assert not offenders, f"resolve via src.paths instead: {offenders}"
