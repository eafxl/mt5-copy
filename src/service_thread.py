"""Service thread - runs CopyTradingService in a background QThread.

Architecture:
- Qt GUI runs in main thread (required by Qt)
- Copy trading service runs here in a background thread
- Communication via:
  - LiveSettings (thread-safe singleton) for settings/pause
  - UIEventQueue (thread-safe queue) for trade events to UI
  - Qt signals for log output and service status
"""
import asyncio
import logging

from PyQt6.QtCore import QThread, pyqtSignal

from .main import CopyTradingService


class _QtLogHandler(logging.Handler):
    """Logging handler that forwards formatted records via a callback.

    Installed while the service thread runs, removed on exit.
    Allows the GUI to display service log output in real time.
    """

    def __init__(self, callback):
        super().__init__()
        self._callback = callback
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
        ))

    def emit(self, record):
        try:
            msg = self.format(record)
            self._callback(msg)
        except Exception:
            pass


class WarmupThread(QThread):
    """Runs CopyTradingService setup + warmup_slaves in a background thread.

    After warmup completes the ready service is available via `self.service`.
    The main thread reads it from the `finished` signal handler — no cross-thread
    object signal needed.

    Signals:
        log_message(str): Formatted log line from the service.
        status_changed(str): "warming_up", "setup_failed", or "error".
    """

    log_message = pyqtSignal(str)
    status_changed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.service: CopyTradingService | None = None
        self.success = False
        self.warmup_ok = 0
        self.warmup_failed = 0

    def run(self):
        """Thread entry point - setup + warmup only."""
        self.service = CopyTradingService(embedded=True)

        log_handler = _QtLogHandler(self.log_message.emit)
        root_logger = logging.getLogger()
        root_logger.addHandler(log_handler)

        try:
            if not self.service.setup():
                self.log_message.emit("Service setup failed - check configuration")
                self.status_changed.emit("setup_failed")
                return

            self.status_changed.emit("warming_up")

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                ok, failed = loop.run_until_complete(self.service.warmup_only())
            finally:
                loop.close()

            self.warmup_ok = ok
            self.warmup_failed = failed
            self.success = True

        except Exception as e:
            self.log_message.emit(f"Warmup error: {e}")
            self.status_changed.emit("error")
        finally:
            root_logger.removeHandler(log_handler)


class ServiceThread(QThread):
    """Runs CopyTradingService in a background thread with its own asyncio loop.

    Accepts an already-warmed CopyTradingService instance (from WarmupThread).

    Signals (all thread-safe via Qt queued connections):
        log_message(str): Formatted log line from the service.
        status_changed(str): One of "starting", "running", "setup_failed", "error".
    """

    log_message = pyqtSignal(str)
    status_changed = pyqtSignal(str)

    def __init__(self, service: CopyTradingService, parent=None):
        super().__init__(parent)
        self._service = service
        self._loop: asyncio.AbstractEventLoop | None = None

    def run(self):
        """Thread entry point - runs the already-warmed service."""
        self.status_changed.emit("starting")

        log_handler = _QtLogHandler(self.log_message.emit)
        root_logger = logging.getLogger()
        root_logger.addHandler(log_handler)

        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

            try:
                self._loop.run_until_complete(self._async_main())
            finally:
                self._loop.close()
                self._loop = None

        except Exception as e:
            self.log_message.emit(f"Service error: {e}")
            self.status_changed.emit("error")
        finally:
            root_logger.removeHandler(log_handler)

    async def _async_main(self):
        """Async entry: run service loop (skip warmup).

        Cleanup (shutdown + terminal killing) is handled by DashboardTab._on_finished
        when the thread exits, NOT here. This avoids killing terminals on transient
        errors and keeps shutdown control in the GUI layer.
        """
        await self._service.run_async(
            on_ready=lambda: self.status_changed.emit("running"),
            skip_warmup=True,
        )

    def request_stop(self):
        """Request graceful service shutdown (called from GUI/main thread).

        Thread-safe: uses loop.call_soon_threadsafe to signal the asyncio event.
        """
        service = self._service
        loop = self._loop
        if service:
            service.running = False
            if service._shutdown_event and loop:
                try:
                    loop.call_soon_threadsafe(service._shutdown_event.set)
                except RuntimeError:
                    pass  # Loop already closed
