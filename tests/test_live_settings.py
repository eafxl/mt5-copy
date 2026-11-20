"""Tests for live settings - thread safety and real-time updates."""
import tempfile
import threading
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest
import yaml


class TestLiveSettings:
    """Test LiveSettings thread safety and functionality."""

    def setup_method(self):
        """Reset singleton before each test."""
        from src.live_settings import LiveSettings
        LiveSettings.reset()

    def test_singleton(self):
        """Verify LiveSettings is a singleton."""
        from src.live_settings import get_live_settings

        s1 = get_live_settings()
        s2 = get_live_settings()
        assert s1 is s2

    def test_thread_safe_reads(self):
        """Verify concurrent reads don't crash."""
        from src.live_settings import get_live_settings

        settings = get_live_settings()
        settings.max_slippage = 25
        settings.update_slave("Slave1", multiplier=2.0)

        results = []
        errors = []

        def reader(n):
            for _ in range(100):
                try:
                    _ = settings.max_slippage
                    _ = settings.get_slave("Slave1")
                    _ = settings.is_slave_enabled("Slave1")
                    results.append(1)
                except Exception as e:
                    errors.append(str(e))

        threads = [threading.Thread(target=reader, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Errors during concurrent reads: {errors}"
        assert len(results) == 1000

    def test_thread_safe_writes(self):
        """Verify concurrent writes don't crash or corrupt data."""
        from src.live_settings import get_live_settings

        settings = get_live_settings()
        errors = []

        def writer(n):
            for i in range(50):
                try:
                    settings.max_slippage = 10 + n + i
                    settings.update_slave(f"Slave{n}", multiplier=1.0 + i * 0.1)
                except Exception as e:
                    errors.append(str(e))

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Errors during concurrent writes: {errors}"

    def test_concurrent_read_write(self):
        """Verify concurrent reads and writes work correctly."""
        from src.live_settings import get_live_settings

        settings = get_live_settings()
        settings.max_slippage = 20
        settings.update_slave("Slave1", multiplier=1.0, enabled=True)

        read_errors = []
        write_errors = []
        read_count = [0]
        write_count = [0]

        stop_event = threading.Event()

        def reader():
            while not stop_event.is_set():
                try:
                    _ = settings.max_slippage
                    _ = settings.get_slave_multiplier("Slave1")
                    read_count[0] += 1
                except Exception as e:
                    read_errors.append(str(e))

        def writer():
            for i in range(100):
                try:
                    settings.max_slippage = 20 + (i % 10)
                    settings.update_slave("Slave1", multiplier=1.0 + (i % 10) * 0.1)
                    write_count[0] += 1
                    time.sleep(0.001)
                except Exception as e:
                    write_errors.append(str(e))

        # Start readers
        readers = [threading.Thread(target=reader) for _ in range(5)]
        for t in readers:
            t.start()

        # Run writer
        writer_thread = threading.Thread(target=writer)
        writer_thread.start()
        writer_thread.join()

        # Stop readers
        stop_event.set()
        for t in readers:
            t.join()

        assert len(read_errors) == 0, f"Read errors: {read_errors}"
        assert len(write_errors) == 0, f"Write errors: {write_errors}"
        assert read_count[0] > 0
        assert write_count[0] == 100

    def test_version_increments(self):
        """Verify version increments on changes."""
        from src.live_settings import get_live_settings

        settings = get_live_settings()
        v1 = settings.version

        settings.max_slippage = 30
        v2 = settings.version
        assert v2 > v1

        settings.update_slave("Slave1", multiplier=2.0)
        v3 = settings.version
        assert v3 > v2

    def test_slave_settings_copy(self):
        """Verify get_slave returns a copy, not the original."""
        from src.live_settings import get_live_settings

        settings = get_live_settings()
        settings.update_slave("Slave1", multiplier=1.5, enabled=True)

        slave1 = settings.get_slave("Slave1")
        slave1.multiplier = 999.0  # Modify the copy

        slave1_again = settings.get_slave("Slave1")
        assert slave1_again.multiplier == 1.5  # Original unchanged

    def test_callback_on_change(self):
        """Verify callbacks are called on settings change."""
        from src.live_settings import get_live_settings

        settings = get_live_settings()
        callback_versions = []

        def on_change(version):
            callback_versions.append(version)

        settings.register_callback(on_change)

        settings.max_slippage = 25
        settings.update_slave("Slave1", multiplier=2.0)

        # Callbacks should have been called
        assert len(callback_versions) >= 2

    def test_snapshot(self):
        """Verify snapshot returns all settings."""
        from src.live_settings import get_live_settings

        settings = get_live_settings()
        settings.max_slippage = 30
        settings.max_entry_deviation_ratio = 0.3
        settings.update_slave("Slave1", multiplier=2.0, enabled=True)

        snapshot = settings.snapshot()

        assert snapshot["max_slippage"] == 30
        assert snapshot["max_entry_deviation_ratio"] == 0.3
        assert "Slave1" in snapshot["slaves"]
        assert snapshot["slaves"]["Slave1"]["multiplier"] == 2.0


class TestIssuerTagging:
    """Test issuer-tagged logging in LiveSettings."""

    def setup_method(self):
        from src.live_settings import LiveSettings
        LiveSettings.reset()

    def test_update_from_config_passes_issuer_to_callbacks(self):
        """update_from_config passes issuer to callbacks."""
        from src.live_settings import get_live_settings
        from src.config import (
            Config, SlaveConfig, MasterConfig, SlaveTerminalConfig, SettingsConfig,
        )

        settings = get_live_settings()
        callback_args = []

        def on_change(version, issuer):
            callback_args.append((version, issuer))

        settings.register_callback(on_change)

        config = Config(
            master=MasterConfig(terminal_path="", poll_interval_ms=500),
            slave_terminal=SlaveTerminalConfig(terminal_path=""),
            slaves=[SlaveConfig(
                name="S1", login=1, password="x", server="s",
                multiplier=1.0, enabled=True,
            )],
            symbol_mapping={},
            filling_mode={"default": "IOC"},
            settings=SettingsConfig(max_slippage_points=10),
        )

        settings.update_from_config(config, issuer="system")

        assert len(callback_args) == 1
        version, issuer = callback_args[0]
        assert issuer == "system"
        assert version > 0

    def test_update_from_dict_passes_issuer_to_callbacks(self):
        """update_from_dict passes issuer to callbacks."""
        from src.live_settings import get_live_settings

        settings = get_live_settings()
        callback_args = []

        def on_change(version, issuer):
            callback_args.append((version, issuer))

        settings.register_callback(on_change)

        data = {
            "settings": {"max_slippage_points": 25},
            "slaves": [{"name": "S1", "multiplier": 1.0, "enabled": True}],
        }
        settings.update_from_dict(data, issuer="user")

        assert len(callback_args) == 1
        version, issuer = callback_args[0]
        assert issuer == "user"

    def test_update_slave_with_issuer_logs_tag(self):
        """update_slave(issuer='user') includes tag in log."""
        from src.live_settings import get_live_settings
        import logging

        settings = get_live_settings()
        log_messages = []

        handler = logging.Handler()
        handler.emit = lambda record: log_messages.append(record.getMessage())
        handler.setLevel(logging.DEBUG)
        settings._logger.addHandler(handler)
        old_level = settings._logger.level
        settings._logger.setLevel(logging.DEBUG)

        try:
            settings.update_slave("S1", issuer="user", multiplier=2.0)

            tagged = [m for m in log_messages if "[user]" in m and "S1" in m]
            assert len(tagged) == 1
        finally:
            settings._logger.removeHandler(handler)
            settings._logger.setLevel(old_level)

    def test_update_from_config_default_issuer_is_system(self):
        """update_from_config defaults to issuer='system'."""
        from src.live_settings import get_live_settings
        from src.config import (
            Config, SlaveConfig, MasterConfig, SlaveTerminalConfig, SettingsConfig,
        )

        settings = get_live_settings()
        callback_args = []

        def on_change(version, issuer):
            callback_args.append(issuer)

        settings.register_callback(on_change)

        config = Config(
            master=MasterConfig(terminal_path="", poll_interval_ms=500),
            slave_terminal=SlaveTerminalConfig(terminal_path=""),
            slaves=[],
            symbol_mapping={},
            filling_mode={"default": "IOC"},
            settings=SettingsConfig(),
        )

        settings.update_from_config(config)  # no issuer arg

        assert callback_args == ["system"]

    def test_update_from_dict_default_issuer_is_user(self):
        """update_from_dict defaults to issuer='user'."""
        from src.live_settings import get_live_settings

        settings = get_live_settings()
        callback_args = []

        def on_change(version, issuer):
            callback_args.append(issuer)

        settings.register_callback(on_change)

        settings.update_from_dict({"settings": {}, "slaves": []})  # no issuer arg

        assert callback_args == ["user"]


class TestConfigWatcher:
    """Test config file watcher."""

    def setup_method(self):
        """Reset singletons before each test."""
        from src.live_settings import LiveSettings
        LiveSettings.reset()

    def test_detects_but_does_not_apply_file_change(self):
        """Watcher detects config changes but must NOT apply them to live settings.

        Contract change: the UI is the single source of truth. config.yaml is
        read only at startup; runtime file edits are detected (reload_count
        increments) but never pushed onto the running engine.
        """
        from src.config_watcher import ConfigWatcher
        from src.live_settings import get_live_settings

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"

            # Create initial config
            initial_config = {
                "master": {"terminal_path": "", "poll_interval_ms": 500},
                "slave_terminal": {"terminal_path": ""},
                "slaves": [
                    {"name": "Slave1", "login": 123, "password": "x", "server": "s",
                     "multiplier": 1.0, "enabled": True}
                ],
                "symbol_mapping": {},
                "settings": {
                    "max_slippage_points": 0,
                    "max_entry_deviation_ratio": 0.2,
                    "retry_attempts": 3,
                    "retry_delay_ms": 1000,
                    "log_level": "INFO",
                },
            }
            with open(config_path, "w") as f:
                yaml.dump(initial_config, f)

            settings = get_live_settings()
            watcher = ConfigWatcher(config_path, check_interval=0.2)

            # Watcher does not apply anything, even on force_reload.
            watcher.force_reload()
            assert settings.max_slippage == 0

            watcher.start()

            try:
                # Modify config file at runtime
                time.sleep(0.1)
                initial_config["settings"]["max_slippage_points"] = 30
                initial_config["slaves"][0]["multiplier"] = 3.0
                with open(config_path, "w") as f:
                    yaml.dump(initial_config, f)

                # Wait for watcher to detect the change
                time.sleep(0.5)

                # Change was DETECTED (count incremented) ...
                assert watcher.reload_count >= 1
                # ... but NOT applied — live settings are untouched.
                assert settings.max_slippage == 0

            finally:
                watcher.stop()

    def test_watcher_reload_count(self):
        """Verify reload count increments."""
        from src.config_watcher import ConfigWatcher

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"

            config = {
                "master": {"terminal_path": "", "poll_interval_ms": 500},
                "slave_terminal": {"terminal_path": ""},
                "slaves": [
                    {"name": "Slave1", "login": 123, "password": "x", "server": "s",
                     "multiplier": 1.0, "enabled": True}
                ],
                "symbol_mapping": {},
                "settings": {
                    "max_slippage_points": 0,
                    "max_entry_deviation_ratio": 0.2,
                    "retry_attempts": 3,
                    "retry_delay_ms": 1000,
                    "log_level": "INFO",
                },
            }
            with open(config_path, "w") as f:
                yaml.dump(config, f)

            watcher = ConfigWatcher(config_path, check_interval=0.1)
            watcher.start()

            try:
                initial_count = watcher.reload_count

                # Touch file multiple times
                for i in range(3):
                    time.sleep(0.15)
                    config["settings"]["max_slippage_points"] = i + 1
                    with open(config_path, "w") as f:
                        yaml.dump(config, f)
                    time.sleep(0.15)

                # Should have reloaded at least once
                assert watcher.reload_count > initial_count

            finally:
                watcher.stop()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
