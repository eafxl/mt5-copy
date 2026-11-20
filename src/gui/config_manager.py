"""Configuration manager for GUI - handles loading, saving, and MT5 folder operations."""
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml

from ..config import Config, MasterConfig, SlaveConfig, SlaveTerminalConfig, SettingsConfig


class ConfigManager:
    """Manages configuration loading, saving, and MT5 folder operations."""

    def __init__(self, config_path: Path | None = None):
        if config_path is None:
            from ..paths import config_path as default_config_path
            config_path = default_config_path()
        self.config_path = config_path
        self._data: dict[str, Any] = {}

    def load(self) -> dict[str, Any]:
        """Load raw config data from YAML."""
        if self.config_path.exists():
            with open(self.config_path, "r", encoding="utf-8") as f:
                self._data = yaml.safe_load(f) or {}
        else:
            self._data = self._default_config()
        self._migrate(self._data)
        return self._data

    @staticmethod
    def _migrate(data: dict[str, Any]) -> None:
        """Bring a file written by an older build into the current shape.

        Done on LOAD rather than on save, so every reader — the Rules dialog,
        the Settings tab, the save path that rewrites the file — sees one
        shape. A migration that only ran on save would leave the dialogs
        showing the pre-migration value until someone happened to press Save.
        """
        # The per-trade risk guard was one global cap; it is a slave field now.
        # Hand it to every slave that has none of its own, then drop the key —
        # dropping it is what stops the next save from writing it back.
        settings = data.get("settings") or {}
        legacy_guard = settings.pop("max_risk_pct_per_trade", 0.0) or 0.0
        if legacy_guard > 0:
            for slave in data.get("slaves") or []:
                slave.setdefault("max_risk_pct_per_trade", legacy_guard)

    def _default_config(self) -> dict[str, Any]:
        """Return default configuration structure."""
        return {
            "master": {
                "terminal_path": "",
                "poll_interval_ms": 200,
            },
            "slave_terminal": {
                "terminal_path": "",
            },
            "slaves": [],
            "symbol_mapping": {},
            "filling_mode": {
                "default": "IOC",
            },
            "settings": {
                "sync_existing_on_start": True,
                "max_slippage_points": 0,
                "retry_attempts": 5,
                "retry_delay_ms": 2000,
                "log_level": "INFO",
                "max_entry_deviation_ratio": 0.2,
                "debug_skip_execution": False,
                "paused": False,
            },
            "price_calc": {
                "enabled": False,
                "refresh_sec": 5.0,
                "pairs": [],
            },
            "news": {
                "enabled": False,
                "refresh_sec": 900.0,
            },
        }

    def save(self, data: dict[str, Any]) -> None:
        """Save configuration to YAML."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        self._data = data

    def validate_master_path(self, path: str) -> tuple[bool, str]:
        """Validate master terminal path. Returns (valid, message)."""
        if not path:
            return False, "Master terminal path is required"

        p = Path(path)
        if not p.exists():
            return False, f"Path does not exist: {path}"

        if not p.is_file():
            return False, "Path must be a file (terminal64.exe)"

        if p.name.lower() not in ("terminal64.exe", "terminal.exe"):
            return False, "Path must point to terminal64.exe or terminal.exe"

        return True, "Valid MT5 terminal"

    def get_next_slave_number(self) -> int:
        """Get the next available slave number."""
        slaves = self._data.get("slaves", [])
        max_num = 0
        for slave in slaves:
            name = slave.get("name", "")
            if name.lower().startswith("slave"):
                try:
                    num = int(name[5:].strip())
                    max_num = max(max_num, num)
                except ValueError:
                    pass
        return max_num + 1

    def get_slaves_directory(self) -> Path:
        """Slaves directory is locked (see template_manager.slaves_dir).

        Not user-configurable: stable path keeps per-slave AppData hashes
        stable and avoids template migration headaches.
        """
        from ..template_manager import slaves_dir
        return slaves_dir()

    def get_template_folder(self) -> Path | None:
        """Get the seeded slave template folder, or None if missing/unset."""
        template_path = self._data.get("slave_terminal", {}).get("template_path", "")
        if not template_path:
            return None
        p = Path(template_path)
        if p.is_dir():
            return p
        return None

    def create_slave_terminal(self, slave_name: str) -> tuple[bool, str, str]:
        """
        Copy the slave template folder to create a new slave terminal install.
        The template is a virgin MT5 snapshot (see template_manager.py).
        Returns (success, message, terminal_path).
        """
        template_folder = self.get_template_folder()
        if not template_folder:
            return False, (
                "Slave template not configured. Open Settings → Slave Template "
                "and run 'Seed Template' to capture a virgin MT5 install first."
            ), ""

        # Create slave folder in configured slaves directory
        slaves_dir = self.get_slaves_directory()
        slave_folder_name = f"MetaTrader 5 {slave_name}"
        slave_folder = slaves_dir / slave_folder_name

        if slave_folder.exists():
            # Folder exists, check for terminal
            terminal_exe = slave_folder / "terminal64.exe"
            if not terminal_exe.exists():
                terminal_exe = slave_folder / "terminal.exe"

            if terminal_exe.exists():
                return True, f"Using existing folder: {slave_folder}", str(terminal_exe)
            else:
                return False, f"Folder exists but no terminal found: {slave_folder}", ""

        try:
            # Ensure slaves directory exists
            slaves_dir.mkdir(parents=True, exist_ok=True)
            # Copy template folder
            shutil.copytree(template_folder, slave_folder)

            # Find terminal executable
            terminal_exe = slave_folder / "terminal64.exe"
            if not terminal_exe.exists():
                terminal_exe = slave_folder / "terminal.exe"

            if terminal_exe.exists():
                return True, f"Created from template: {slave_folder}", str(terminal_exe)
            else:
                return False, f"Copied template but terminal not found", ""

        except PermissionError:
            return False, (
                f"Permission denied copying to:\n{slave_folder}\n\n"
                "Change the Slaves Directory in Settings to a folder "
                "you have write access to (e.g. Documents\\MT5Copy), "
                "or relaunch the app as Administrator."
            ), ""
        except Exception as e:
            return False, f"Failed to copy: {e}", ""

    def delete_slave_terminal(self, terminal_path: str) -> tuple[bool, str]:
        """
        Delete slave MT5 folder.
        Returns (success, message).
        """
        if not terminal_path:
            return True, "No terminal path to delete"

        p = Path(terminal_path)
        if not p.exists():
            return True, "Terminal path doesn't exist"

        folder = p.parent

        # Safety: folder must be under the slaves directory (never the master
        # install path or anywhere outside the locked slaves tree).
        from ..template_manager import slaves_dir as _slaves_dir_fn
        slaves_root = _slaves_dir_fn().resolve()
        try:
            folder_resolved = folder.resolve()
            folder_resolved.relative_to(slaves_root)
        except (ValueError, OSError):
            return False, f"Refusing to delete folder outside slaves dir: {folder}"

        # Safety: also refuse to delete the template itself.
        from ..template_manager import template_dir as _template_dir_fn
        if folder_resolved == _template_dir_fn().resolve():
            return False, "Cannot delete the slave template"

        # Safety check - must be MT5 folder
        if not (folder / "terminal64.exe").exists() and not (folder / "terminal.exe").exists():
            return False, "Not a valid MT5 folder"

        try:
            shutil.rmtree(folder)
            return True, f"Deleted: {folder}"
        except PermissionError:
            return False, (
                f"Permission denied deleting:\n{folder}\n\n"
                "Close the MT5 terminal and relaunch the app as Administrator."
            )
        except Exception as e:
            return False, f"Failed to delete: {e}"

    def add_slave(self, slave_data: dict[str, Any]) -> None:
        """Add a slave to configuration."""
        if "slaves" not in self._data:
            self._data["slaves"] = []
        self._data["slaves"].append(slave_data)

    def remove_slave(self, slave_name: str) -> dict[str, Any] | None:
        """Remove a slave from configuration. Returns removed slave data."""
        slaves = self._data.get("slaves", [])
        for i, slave in enumerate(slaves):
            if slave.get("name") == slave_name:
                return slaves.pop(i)
        return None

    def update_slave(self, slave_name: str, slave_data: dict[str, Any]) -> bool:
        """Update an existing slave configuration."""
        slaves = self._data.get("slaves", [])
        for i, slave in enumerate(slaves):
            if slave.get("name") == slave_name:
                slaves[i] = slave_data
                return True
        return False

    def get_slaves(self) -> list[dict[str, Any]]:
        """Get all slave configurations."""
        return self._data.get("slaves", [])