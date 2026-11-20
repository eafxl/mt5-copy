"""GUI entry point for MetaCT.

Initializes debug/logging systems before starting the GUI.
"""


def main():
    """Entry point for GUI."""
    # Worker processes re-execute this binary when frozen. freeze_support()
    # short-circuits that re-entry into the worker bootstrap; without it every
    # ProcessPoolExecutor worker would open a second main window instead.
    import multiprocessing
    multiprocessing.freeze_support()

    # Initialize debug/logging BEFORE anything else
    from .paths import describe, dumps_dir, ensure_dirs, logs_dir
    from .utils import setup_debug, setup_logging

    ensure_dirs()

    # Setup debug dumper (crash handlers, system info)
    # Disable signal handlers in GUI mode to avoid conflicts with Qt
    setup_debug(dumps_dir(), install_signal_handlers=False)

    # Setup async logging
    logger = setup_logging("INFO", logs_dir())

    # Frozen builds can land their data in three different places depending on
    # where the EXE sits — say which one won, or a support question about a
    # missing log turns into a scavenger hunt.
    for key, value in describe().items():
        logger.info("path %-12s %s", key, value)

    # Now start the GUI
    from .gui.main_window import run_gui
    run_gui()


if __name__ == "__main__":
    main()
