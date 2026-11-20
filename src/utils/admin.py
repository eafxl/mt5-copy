"""Windows admin (UAC) detection + self-elevation.

App needs admin to install MT5, manage Program Files, kill processes, and
write to slave terminal folders. Without it, MT5 init can fail silently and
slave terminal copy/delete can hit ACL errors.
"""
import logging
import sys


_logger = logging.getLogger("metact").getChild("admin")

# ShellExecuteW return values: any value > 32 means success. Common errors:
#   0  = out of memory / not registered
#   2  = file not found
#  31  = no association
# 1223 = ERROR_CANCELLED — user clicked No on UAC dialog
SE_ERR_OK_MIN = 32
SE_ERR_CANCELLED = 1223


def is_admin() -> bool:
    """Return True if the current process has admin rights.

    Non-Windows platforms always return True so dev work on macOS/Linux
    isn't blocked.
    """
    if sys.platform != "win32":
        _logger.info("is_admin: non-Windows platform (%s), returning True", sys.platform)
        return True
    try:
        import ctypes
        result = bool(ctypes.windll.shell32.IsUserAnAdmin())
        _logger.info("is_admin: IsUserAnAdmin() returned %s", result)
        return result
    except Exception as e:
        _logger.warning("is_admin: ctypes IsUserAnAdmin failed: %s", e)
        return False


def _build_relaunch_params() -> str:
    """Build the command line to pass to ShellExecuteW.

    Must reproduce the ORIGINAL launch mode:
      - `python -m src.gui_main`  → must elevate with `-m src.gui_main`
        (so relative imports like `from .utils import ...` still resolve).
      - `python script.py`         → just pass the script path.

    Either way, append the original sys.argv[1:] so user-supplied args
    survive the re-launch.
    """
    import os

    extra = " ".join(f'"{a}"' for a in sys.argv[1:])

    # If we were started via `-m pkg.mod`, __main__'s __spec__ has the
    # module name. Reproduce that exactly.
    main_mod = sys.modules.get("__main__")
    spec = getattr(main_mod, "__spec__", None)
    if spec is not None and getattr(spec, "name", None):
        module_name = spec.name
        cwd = os.getcwd()
        # Run from the project root so `src.gui_main` resolves. Elevated
        # processes default to system32 — use sys.path[0] or original cwd.
        params = f"-m {module_name}"
        if extra:
            params += " " + extra
        return params, cwd

    # Fallback: launched as a plain script path.
    script = sys.argv[0] if sys.argv else ""
    parts = [f'"{script}"']
    if sys.argv[1:]:
        parts += [f'"{a}"' for a in sys.argv[1:]]
    return " ".join(parts), os.getcwd()


def try_elevate() -> tuple[bool, str]:
    """Attempt to relaunch the current process elevated (UAC prompt).

    Returns:
        (True, "")          → elevated child spawned; caller should exit.
        (False, "cancelled") → user clicked No on UAC dialog.
        (False, <reason>)    → ShellExecute failed for another reason.

    On non-Windows returns (False, "non-windows") without side effects.
    """
    if sys.platform != "win32":
        _logger.info("try_elevate: non-Windows, skipping")
        return False, "non-windows"

    try:
        import ctypes
        params, cwd = _build_relaunch_params()
        _logger.info(
            "try_elevate: ShellExecuteW runas exe=%s params=%s cwd=%s",
            sys.executable, params, cwd,
        )
        rc = ctypes.windll.shell32.ShellExecuteW(
            None,        # parent hwnd
            "runas",     # verb → triggers UAC
            sys.executable,
            params,
            cwd,         # working dir — must be project root for `-m` to find package
            1,           # SW_SHOWNORMAL
        )
        _logger.info("try_elevate: ShellExecuteW returned %d", rc)
        if rc > SE_ERR_OK_MIN:
            return True, ""
        if rc == SE_ERR_CANCELLED:
            return False, "cancelled"
        return False, f"ShellExecute error code {rc}"
    except Exception as e:
        _logger.exception("try_elevate: exception")
        return False, str(e)