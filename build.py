"""Build MetaCT into a Windows EXE with Nuitka.

WHY NUITKA (and not PyInstaller)
    PyInstaller bundles .pyc, which decompiles back to readable source in
    seconds — for a tool that carries broker credentials and a trading edge
    that is the wrong default. Nuitka compiles to C and links a real binary,
    so there is no bytecode to lift out. It also starts faster, and its PyQt6
    plugin resolves the Qt plugin tree (platforms, styles, imageformats)
    correctly, which is the part hand-written PyInstaller specs always get
    wrong.

WHY ONEFILE
    One `dist\\MetaCT.exe` and nothing else — that is the whole point of
    shipping a binary. Onefile unpacks itself to a temp dir on launch, which
    costs a few seconds of startup, and the ProcessPoolExecutor workers (one
    per slave) are launched from the already-unpacked copy rather than
    re-extracting. Those pools are long-lived, so the cost lands at startup
    only, not on the copy path. Not worth a folder of loose DLLs.

USAGE
    python build.py                 # dist\\MetaCT.exe
    python build.py --console       # keep the console window (debugging)
    python build.py --clean         # delete previous artifacts first
    python build.py --check         # print the command, build nothing

Cross-compiling is not a thing for Nuitka: a Windows EXE must be built on
Windows. `--check` works anywhere, so the command can be reviewed from macOS.
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENTRY = ROOT / "metact.py"
ICON = ROOT / "src" / "gui" / "icons" / "app_icon.ico"
OUT_DIR = ROOT / "dist"
LOG_PATH = OUT_DIR / "build.log"
EXE_NAME = "MetaCT.exe"
EXE_PATH = OUT_DIR / EXE_NAME

# Nuitka's scratch trees, named after the entry script. Listed so --clean can
# remove exactly our artifacts: dist/ is also make_dist.sh's output directory,
# and wiping the whole thing would take a source distribution with it.
BUILD_TEMP = [
    ROOT / "metact.build",
    ROOT / "metact.onefile-build",
    ROOT / "metact.dist",
]

COMPANY = "MetaCT"
PRODUCT = "MetaCT"

# Imported lazily so `--check` still runs on a machine without the package.
def _version() -> str:
    text = (ROOT / "src" / "__init__.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"\'')
    return "0.0.0"


# Data that must sit next to the code in the bundle. Icons are loaded from disk
# at runtime (paths.icons_dir), so a code-only build ships blank toolbar
# buttons and a default window icon.
DATA_DIRS = [
    ("src/gui/icons", "src/gui/icons"),
    ("mt5_ea", "mt5_ea"),          # the EA the user copies into MT5 by hand
]

# Packages pulled in whole, because import following alone misses them.
#
#   MetaTrader5 — a package, not a single module. Its __init__ imports numpy
#       part-way down the file, and including it as a bare module left the
#       dependency out entirely.
#   numpy — MetaTrader5's hard dependency. Loads `numpy._core.multiarray` and
#       friends as C extensions through its own machinery, which no static
#       import scan sees, so it has to be named outright.
INCLUDE_PACKAGES = ["src", "MetaTrader5", "numpy"]
INCLUDE_MODULES: list[str] = []

# numpy ships .pyi stubs and version metadata it reads at import time.
INCLUDE_PACKAGE_DATA = ["numpy"]

# Test-only and dev-only weight. Excluding them cuts both build time and the
# shipped size, and keeps pytest from being importable inside the product.
EXCLUDE = ["pytest", "_pytest", "tests", "tkinter", "setuptools", "pip"]


def nuitka_version() -> tuple[tuple[int, ...], str]:
    """Installed Nuitka version, plus why it couldn't be determined.

    Returns ``(version, error)``. An installed-but-crashing Nuitka must not be
    reported as "not installed" — that sends you off installing a package that
    is already there instead of reading the traceback.
    """
    try:
        out = subprocess.run(
            [sys.executable, "-m", "nuitka",
             # Nuitka may offer to download a C compiler and wait on stdin for
             # an answer. With capture_output that prompt is invisible, so the
             # probe hangs with nothing on screen. Accept up front AND close
             # stdin, so any other question fails fast instead of hanging.
             "--assume-yes-for-downloads", "--version"],
            capture_output=True, text=True, timeout=30,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return (), f"could not run Nuitka: {e}"
    if out.returncode != 0:
        combined = (out.stderr or "") + (out.stdout or "")
        if "No module named nuitka" in combined:
            return (), "Nuitka not installed — pip install nuitka"
        tail = combined.strip().splitlines()
        detail = "\n      ".join(tail[-4:]) if tail else "no output"
        return (), (
            f"Nuitka is installed but crashed on --version (exit "
            f"{out.returncode}):\n      {detail}"
        )
    first = (out.stdout or "").strip().splitlines()[0] if out.stdout else ""
    parts = []
    for chunk in first.split(".")[:3]:
        digits = "".join(c for c in chunk if c.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts), ""


def build_command(console: bool, jobs: int | None,
                  nuitka_ver: tuple[int, ...]) -> list[str]:
    version = _version()
    # Windows file/product version fields want exactly four numeric parts.
    quad = ".".join((version.split(".") + ["0", "0", "0", "0"])[:4])

    cmd = [
        sys.executable, "-m", "nuitka",
        "--onefile",
        "--assume-yes-for-downloads",
        f"--output-dir={OUT_DIR}",
        f"--output-filename={EXE_NAME}",
        # PyQt6's Qt plugins (platforms/, styles/, imageformats/) are loaded by
        # Qt at runtime, not imported — only the plugin knows to ship them.
        "--enable-plugin=pyqt6",
        # ProcessPoolExecutor: teaches Nuitka how the frozen binary re-executes
        # itself as a worker. Without it, spawning a pool worker relaunches the
        # GUI instead.
        "--enable-plugin=multiprocessing",
        f"--company-name={COMPANY}",
        f"--product-name={PRODUCT}",
        f"--file-version={quad}",
        f"--product-version={quad}",
        "--file-description=MT5 trade copier",
        f"--copyright={COMPANY}",
    ]

    # Nuitka 1.7 replaced --windows-disable-console with a tri-state flag.
    if nuitka_ver >= (1, 7):
        cmd.append("--windows-console-mode=" + ("force" if console else "disable"))
    elif not console:
        cmd.append("--windows-disable-console")

    if ICON.exists():
        cmd.append(f"--windows-icon-from-ico={ICON}")
    else:
        print(f"WARNING: {ICON} missing — run tools/make_icon.py first.")

    for pkg in INCLUDE_PACKAGES:
        cmd.append(f"--include-package={pkg}")
    for mod in INCLUDE_MODULES:
        cmd.append(f"--include-module={mod}")
    for pkg in INCLUDE_PACKAGE_DATA:
        cmd.append(f"--include-package-data={pkg}")
    for name in EXCLUDE:
        cmd.append(f"--nofollow-import-to={name}")
    for src, dest in DATA_DIRS:
        if (ROOT / src).is_dir():
            cmd.append(f"--include-data-dir={ROOT / src}={dest}")

    if jobs:
        cmd.append(f"--jobs={jobs}")
    cmd.append(str(ENTRY))
    return cmd


def share_reason(path: Path) -> str:
    """Why `path` is unbuildable, or "" when it's a normal local directory.

    Nuitka canonicalizes every path it touches through `getFilenameRealPath`,
    which asserts that a resolved path still starts with its own drive's real
    path. On a Parallels share (`C:\\Mac\\Home\\...`, a junction onto
    `\\\\Mac\\Home`) and on mapped network drives that assumption is false, and
    the build dies on `assert path.startswith(drive_real_path)` — with a bare
    AssertionError that says nothing about the cause. Catch it here instead.
    """
    if sys.platform != "win32":
        return ""

    raw = str(path)
    try:
        real = os.path.realpath(raw)
    except OSError:
        real = raw

    if real.startswith("\\\\"):
        return f"{raw} resolves to the network path {real}"

    # Parallels mounts the macOS side at C:\Mac\... via a junction. realpath
    # does not always follow it, but Nuitka's canonicalization does.
    parts = Path(raw).parts
    if len(parts) > 1 and parts[1].lower() == "mac":
        return f"{raw} is a Parallels shared folder (C:\\Mac\\...)"

    try:
        import ctypes

        DRIVE_REMOTE = 4
        drive = os.path.splitdrive(os.path.abspath(raw))[0]
        if drive and ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == DRIVE_REMOTE:
            return f"{raw} is on network drive {drive}"
    except Exception:
        pass

    return ""


#: Seconds of total silence from Nuitka before printing an "alive" heartbeat.
HEARTBEAT_S = 20.0


def run_teed(cmd: list[str]) -> int:
    """Run Nuitka, streaming output to the console AND to build/build.log.

    Nuitka emits far more than a console buffer holds, and the interesting
    line — the first compiler error — scrolls off long before the build gives
    up. The log is what actually gets read afterwards; the live stream is only
    there so a 15-minute compile doesn't look hung.

    Two things conspire to make a piped build look dead, and both are handled
    here:

      * Nuitka's stdout is block-buffered once it isn't a console, so output
        arrives in 8 KB lumps, minutes apart. ``PYTHONUNBUFFERED`` in the
        child's environment forces it back to line-at-a-time.
      * The C compile stage is legitimately silent for minutes. A reader thread
        feeds a queue so the main loop can notice the silence and print elapsed
        time, instead of the user concluding it hung and killing it.

    stdin is closed: ``--assume-yes-for-downloads`` answers the only question
    Nuitka normally asks, and with output piped any other prompt would be
    invisible — better to fail fast than to wait forever on a hidden question.
    """
    import queue
    import threading
    import time

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"

    with open(LOG_PATH, "w", encoding="utf-8", errors="replace") as log:
        log.write(" ".join(cmd) + "\n\n")
        log.flush()

        proc = subprocess.Popen(
            cmd, cwd=ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        assert proc.stdout is not None

        lines: "queue.Queue[str | None]" = queue.Queue()

        def reader():
            for line in proc.stdout:            # type: ignore[union-attr]
                lines.put(line)
            lines.put(None)                     # sentinel: stream closed

        threading.Thread(target=reader, daemon=True).start()

        started = time.monotonic()
        while True:
            try:
                line = lines.get(timeout=HEARTBEAT_S)
            except queue.Empty:
                mins, secs = divmod(int(time.monotonic() - started), 60)
                print(f"    ... still compiling ({mins}m{secs:02d}s elapsed)",
                      flush=True)
                continue
            if line is None:
                break
            sys.stdout.write(line)
            sys.stdout.flush()
            log.write(line)
            log.flush()   # the log must survive a Ctrl-C or a hard kill

        return proc.wait()


def preflight(check_only: bool) -> tuple[int, ...]:
    """Verify the build can actually produce a working EXE."""
    problems = []

    if not ENTRY.exists():
        problems.append(f"entry script missing: {ENTRY}")

    # Each probe announces itself: a cold `nuitka --version` takes tens of
    # seconds, and an unlabelled pause reads as a hang.
    print("    checking Nuitka...", flush=True)
    ver, ver_error = nuitka_version()
    if not ver:
        # NOT fatal. This probe exists only to choose between
        # --windows-console-mode and the pre-1.7 --windows-disable-console. A
        # Nuitka that cannot answer still builds fine, and if it is genuinely
        # broken the real build says so with its whole output in build.log —
        # more useful than dying on a version check.
        print(f"    WARNING: {ver_error or 'Nuitka version unknown'}")
        print("    Assuming current flag syntax and continuing.", flush=True)
        ver = (99, 0)

    if not check_only:
        # Checked before anything expensive: Nuitka dies on these paths with a
        # bare AssertionError deep in its path canonicalization, which reads
        # like a Nuitka bug rather than "you are building on a network share".
        print("    checking paths...", flush=True)
        for label, p in (("repo", ROOT), ("interpreter", Path(sys.executable))):
            reason = share_reason(p)
            if reason:
                problems.append(
                    f"{label} is not on a local disk: {reason}.\n"
                    f"      Nuitka canonicalizes every path it touches and "
                    f"asserts on these.\n"
                    f"      Copy the repo to a local disk (e.g. C:\\mt5copy), "
                    f"recreate the venv\n"
                    f"      there, and build from that copy."
                )

        if sys.platform != "win32":
            problems.append(
                f"a Windows EXE must be built on Windows (running on "
                f"{sys.platform}). Use --check to review the command here."
            )
        # numpy is checked explicitly: it is not imported anywhere in src/, it
        # arrives as MetaTrader5's dependency, and its absence only shows up as
        # a crash on the built EXE's first run.
        for mod in ("PyQt6", "yaml", "MetaTrader5", "numpy"):
            print(f"    checking import {mod}...", flush=True)
            try:
                __import__(mod)
            except ImportError:
                problems.append(f"missing runtime dependency: {mod}")

    if problems:
        print("Build blocked:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    return ver


def report() -> None:
    """Say where the artifact landed and what the user still has to supply."""
    if not EXE_PATH.exists():
        print(f"Build finished but {EXE_PATH} is missing — check {LOG_PATH}.")
        sys.exit(1)

    size_mb = EXE_PATH.stat().st_size / (1024 * 1024)
    print()
    print("=" * 60)
    print(f"  {EXE_PATH}   ({size_mb:.1f} MB)")
    print("=" * 60)
    print()
    print("Data lives NEXT TO the EXE (config/, logs/, traces/, dumps/,")
    print("cache/), or in %LOCALAPPDATA%\\MetaCT when that folder is")
    print("read-only. Override with MT5COPY_HOME.")
    print()
    print("config.yaml is deliberately NOT bundled — it holds broker")
    print("passwords in plaintext. First run creates a fresh one.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Build MetaCT.exe with Nuitka")
    ap.add_argument("--console", action="store_true",
                    help="keep the console window for debugging")
    ap.add_argument("--jobs", type=int, default=None,
                    help="parallel C compile jobs")
    ap.add_argument("--check", action="store_true",
                    help="print the command and exit without building")
    ap.add_argument("--clean", action="store_true",
                    help="delete the previous EXE, log and Nuitka scratch dirs")
    args = ap.parse_args()

    # Runs before anything else: the log lives under OUT_DIR, so cleaning
    # afterwards would delete the log just written. Only our own artifacts are
    # removed — dist/ is make_dist.sh's output too, and rmtree'ing all of it
    # would silently eat a source distribution.
    if args.clean:
        for path in [EXE_PATH, LOG_PATH, *BUILD_TEMP]:
            if path.is_dir():
                print(f"Removing {path}")
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                print(f"Removing {path}")
                path.unlink(missing_ok=True)

    ver = preflight(args.check)
    cmd = build_command(args.console, args.jobs, ver)

    if args.check:
        print()
        print(" \\\n  ".join(cmd))
        return

    print("Building — first run compiles the C sources and takes 5-15 min.")
    print("The C compile stage is silent for minutes; a heartbeat line every")
    print(f"{HEARTBEAT_S:.0f}s means it is still working.")
    print(f"Full output is also written to {LOG_PATH}")
    print(flush=True)
    rc = run_teed(cmd)
    if rc != 0:
        print()
        print(f"Nuitka exited {rc}. Full output: {LOG_PATH}")
        sys.exit(rc)

    report()


if __name__ == "__main__":
    main()
