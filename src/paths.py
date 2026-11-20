"""Single source of truth for every path the app reads or writes.

Running from source, `Path(__file__).parent.parent` is the repo root and doubles
as both the code location and the data location. Frozen into an EXE those two
stop being the same thing:

  * **Resources** (icons) live inside the bundle. Under Nuitka onefile the
    bundle is a temp directory that is wiped on exit, so nothing writable may
    ever land there.
  * **Data** (config.yaml, logs, traces, dumps, cache) must live next to the
    EXE the user actually launched, so it survives a rebuild and the user can
    find it.

Every module resolves through the helpers here instead of walking up from its
own `__file__`, so there is exactly one place that knows the difference.

Data root resolution order:

1. ``$METACT_HOME`` (or the older ``$MT5COPY_HOME``) — explicit override, wins
   always.
2. The directory holding the EXE, when frozen and that directory is writable.
   This is the portable layout: ``C:\\metact\\MetaCT.exe`` keeps its
   ``config\\``, ``logs\\`` and ``traces\\`` as siblings.
3. ``%LOCALAPPDATA%\\MetaCT`` — the fallback when the EXE sits somewhere
   read-only (Program Files, a network share, a UAC-virtualized folder).
   An existing ``%LOCALAPPDATA%\\MetaCopier`` from before the rename is used
   in its place, so an upgrade keeps its config instead of starting blank.
4. The repo root, when not frozen.
"""
import os
import sys
from pathlib import Path

APP_NAME = "MetaCT"

#: What the app was called before. Only ever read: a folder under the old name
#: is adopted, never created. Dropping this would not lose the user's data, it
#: would ORPHAN it — the app would come up with no accounts configured and no
#: indication why, which looks exactly like a corrupted install.
LEGACY_APP_NAMES = ("MetaCopier",)

#: Environment override for the writable data root.
HOME_ENV_VAR = "METACT_HOME"

#: Older spelling of the same override, still honoured.
LEGACY_HOME_ENV_VARS = ("MT5COPY_HOME",)

_SRC_DIR = Path(__file__).resolve().parent

# Resolution is stable for a process lifetime and involves a filesystem probe,
# so it is computed once. Worker processes each do it once too and land on the
# same answer, since it depends only on argv/env.
_app_home: Path | None = None


def is_frozen() -> bool:
    """True when running from a compiled binary rather than .py source.

    Nuitka injects a ``__compiled__`` global into every module it compiles;
    PyInstaller sets ``sys.frozen``. Checking both keeps this module honest
    under either builder.
    """
    return "__compiled__" in globals() or bool(getattr(sys, "frozen", False))


def _compiled_attr(name: str):
    """Read an attribute off Nuitka's ``__compiled__`` object, or None."""
    compiled = globals().get("__compiled__")
    return getattr(compiled, name, None) if compiled is not None else None


def executable_dir() -> Path:
    """Directory containing the binary the user launched.

    Under onefile this must be the *original* EXE's folder, never the temp
    extraction dir — data written to the latter disappears on exit. Nuitka
    exposes ``original_argv0`` for exactly that; the rest are fallbacks for
    other builders and for odd launch paths.
    """
    candidates = [
        _compiled_attr("original_argv0"),
        _compiled_attr("containing_dir"),
        sys.argv[0] if sys.argv and sys.argv[0] else None,
        sys.executable,
    ]
    for cand in candidates:
        if not cand:
            continue
        try:
            p = Path(cand).resolve()
        except (OSError, ValueError):
            continue
        if p.is_dir():
            return p
        if p.exists():
            return p.parent
    return Path.cwd()


def _is_writable(directory: Path) -> bool:
    """Probe by actually writing — `os.access` lies under UAC virtualization."""
    probe = directory / f".{APP_NAME}.write_test"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _fallback_home() -> Path:
    """Per-user data dir, used when the EXE's folder is read-only.

    A directory left by an older name is preferred when it exists and the
    current one does not — that is an upgrade, and the config in it is the
    user's. Once the new directory exists it wins unconditionally, so the two
    can never both be live.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return _adopt_legacy(Path(base), lambda name: Path(base) / name)
    home = Path.home()
    return _adopt_legacy(home, lambda name: home / f".{name.lower()}")


def _adopt_legacy(_base: Path, build) -> Path:
    """``build(APP_NAME)``, unless only a legacy-named directory exists."""
    current = build(APP_NAME)
    if current.exists():
        return current
    for legacy in LEGACY_APP_NAMES:
        candidate = build(legacy)
        if candidate.is_dir():
            return candidate
    return current


def app_home() -> Path:
    """Writable data root: config, logs, traces, dumps, cache."""
    global _app_home
    if _app_home is not None:
        return _app_home

    override = os.environ.get(HOME_ENV_VAR)
    for legacy_var in LEGACY_HOME_ENV_VARS:
        if override:
            break
        override = os.environ.get(legacy_var)
    if override:
        home = Path(override).expanduser()
    elif is_frozen():
        exe_dir = executable_dir()
        home = exe_dir if _is_writable(exe_dir) else _fallback_home()
    else:
        home = _SRC_DIR.parent          # repo root

    _app_home = home
    return home


def resource_dir() -> Path:
    """Read-only bundle root — where ``src/`` and its assets sit."""
    return _SRC_DIR.parent


def icons_dir() -> Path:
    """Directory holding the app icon and toolbar glyphs.

    Nuitka standalone keeps compiled modules' ``__file__`` pointing inside the
    dist tree, so the source-relative answer is already right once the build
    copies ``src/gui/icons`` alongside. The second candidate covers a builder
    that flattens the layout instead.
    """
    for cand in (_SRC_DIR / "gui" / "icons", resource_dir() / "src" / "gui" / "icons"):
        if cand.is_dir():
            return cand
    return _SRC_DIR / "gui" / "icons"


# ------------------------------------------------------------ data locations

def config_dir() -> Path:
    return app_home() / "config"


def config_path() -> Path:
    return config_dir() / "config.yaml"


def logs_dir() -> Path:
    return app_home() / "logs"


def traces_dir() -> Path:
    return app_home() / "traces"


def dumps_dir() -> Path:
    return app_home() / "dumps"


def cache_dir() -> Path:
    return app_home() / "cache"


def ensure_dirs() -> None:
    """Create the writable tree. Safe to call repeatedly; never raises."""
    for d in (config_dir(), logs_dir(), traces_dir(), dumps_dir(), cache_dir()):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


def reset_cache() -> None:
    """Forget the resolved data root. Tests only."""
    global _app_home
    _app_home = None


def describe() -> dict[str, str]:
    """Resolved paths, for the log header and the debug dump."""
    return {
        "frozen": str(is_frozen()),
        "app_home": str(app_home()),
        "resource_dir": str(resource_dir()),
        "config": str(config_path()),
        "logs": str(logs_dir()),
        "traces": str(traces_dir()),
        "dumps": str(dumps_dir()),
        "cache": str(cache_dir()),
    }
