"""Top-level launcher — the script the EXE is compiled from.

`src` is a package with relative imports, so it cannot be a compile entry point
itself. This thin wrapper gives Nuitka a real top-level module and keeps the
frozen entry identical to `python -m src.gui_main`.

Keep this file dependency-free: anything imported here is imported before
freeze_support() runs, and in a worker re-execution that cost is paid again on
every pool spawn.
"""
from src.gui_main import main

if __name__ == "__main__":
    main()
