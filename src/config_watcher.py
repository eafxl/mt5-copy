"""Config file watcher - detects config.yaml changes and updates LiveSettings.

Runs as a background thread, checks file modification time periodically.
When change detected, reloads config and updates LiveSettings atomically.
"""
import threading
import time
from pathlib import Path

from .utils.logging import get_logger


class ConfigWatcher:
    """Background thread that watches config.yaml for changes.

    When the file is modified, reloads and updates LiveSettings.
    This enables live settings updates without service restart.
    """

    def __init__(
        self,
        config_path: Path | None = None,
        check_interval: float = 2.0,
    ):
        """
        Args:
            config_path: Path to config.yaml (default: config/config.yaml)
            check_interval: How often to check for changes (seconds)
        """
        if config_path is None:
            from .paths import config_path as default_config_path
            config_path = default_config_path()

        self.config_path = config_path
        self.check_interval = check_interval
        self.logger = get_logger()

        self._running = False
        self._thread: threading.Thread | None = None
        self._last_mtime: float = 0.0
        self._reload_count: int = 0
        self._last_error: str = ""

    def start(self):
        """Start the config watcher thread."""
        if self._running:
            return

        self._running = True
        self._last_mtime = self._get_mtime()

        self._thread = threading.Thread(
            target=self._watch_loop,
            name="ConfigWatcher",
            daemon=True,
        )
        self._thread.start()
        self.logger.info(f"[ConfigWatcher] Started watching: {self.config_path}")

    def stop(self):
        """Stop the config watcher."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self.check_interval + 1)
        self.logger.info("[ConfigWatcher] Stopped")

    def _get_mtime(self) -> float:
        """Get file modification time (0 if file doesn't exist)."""
        try:
            return self.config_path.stat().st_mtime
        except (FileNotFoundError, OSError):
            return 0.0

    def _watch_loop(self):
        """Main watch loop - runs in background thread."""
        while self._running:
            try:
                current_mtime = self._get_mtime()

                if current_mtime > self._last_mtime and current_mtime > 0:
                    self._last_mtime = current_mtime
                    self._reload_config()

            except Exception as e:
                self._last_error = str(e)
                self.logger.error(f"[ConfigWatcher] Error: {e}")

            # Sleep in small increments for responsive shutdown
            for _ in range(int(self.check_interval * 10)):
                if not self._running:
                    break
                time.sleep(0.1)

    def _reload_config(self):
        """React to a config.yaml change.

        The UI is the single source of truth for live settings. A config-file
        change must NOT be pushed back onto the running engine — doing so
        asynchronously overrode the user's in-app settings without consent
        (and once silently re-enabled live execution while debug mode was on).

        The file is now read into LiveSettings only ONCE at startup
        (TradeCopier.__init__). Runtime changes flow one way: UI -> LiveSettings
        (+ persisted to file on Save). Here we only note the change; we do not
        apply it.
        """
        self._reload_count += 1
        self._last_error = ""
        self.logger.info(
            "[ConfigWatcher] config.yaml changed — NOT applied "
            "(UI is source of truth; file is read only at startup)."
        )

    def force_reload(self):
        """Force an immediate config reload."""
        self._reload_config()

    @property
    def reload_count(self) -> int:
        """Number of successful reloads."""
        return self._reload_count

    @property
    def last_error(self) -> str:
        """Last error message (empty if no error)."""
        return self._last_error

    @property
    def is_running(self) -> bool:
        """Whether the watcher is running."""
        return self._running


# Global watcher instance
_watcher: ConfigWatcher | None = None


def start_config_watcher(config_path: Path | None = None) -> ConfigWatcher:
    """Start the global config watcher."""
    global _watcher
    if _watcher is None:
        _watcher = ConfigWatcher(config_path)
    _watcher.start()
    return _watcher


def stop_config_watcher():
    """Stop the global config watcher."""
    global _watcher
    if _watcher:
        _watcher.stop()
        _watcher = None


def get_config_watcher() -> ConfigWatcher | None:
    """Get the global config watcher (None if not started)."""
    return _watcher
