"""The per-trade risk guard belongs to a slave, not to the app.

WHY IT MOVED
    It was one global percentage applied to every slave. A cap only means
    something next to the account it guards: 1% of a 100k prop account is
    $1000 and a sane ceiling, while 1% of a $101 account is $1.01 — less than
    that broker's MINIMUM lot can lose on any stop it will ever be given. The
    small account was therefore blocked on every single trade, permanently,
    and the only trace of it was a "projected loss > max" line at trade time
    that read as if the trade had been oversized.

WHAT IS PINNED HERE
    That each slave's own cap reaches the worker, that a config written before
    the move keeps its guard (migrated onto the slaves, then dropped so it
    cannot come back), and that the number of caps in play equals the number
    of accounts.

The guard's arithmetic is pinned in test_risk_guard.py; this is the plumbing.
"""
import textwrap

import pytest
import yaml

from src.config import Config
from src.gui.config_manager import ConfigManager
from src.live_settings import get_live_settings


def _cap_in_task(copier, make_position, slave_index=0):
    """The max_risk_pct_per_trade the copier would send for one slave."""
    task = copier._build_open_task(
        copier.config.slaves[slave_index],
        make_position(ticket=555, symbol="NAS100", sl=1.09),
        100,
        "T1",
        "TCP",
    )
    return task.params["max_risk_pct_per_trade"]


# ==================== each account carries its own cap ====================

def test_each_slave_sends_its_own_cap(copier_factory, make_config, make_position):
    """The whole point: two accounts, two different ceilings, one master trade."""
    copier = copier_factory(make_config(
        num_slaves=2,
        slave_overrides=[
            {"max_risk_pct_per_trade": 1.0},    # the 100k prop account
            {"max_risk_pct_per_trade": 25.0},   # the $101 account, min lot 0.1
        ],
    ))

    assert _cap_in_task(copier, make_position, 0) == 1.0
    assert _cap_in_task(copier, make_position, 1) == 25.0


def test_the_guard_is_off_by_default(copier_factory, make_config, make_position):
    """A slave nobody configured must not acquire a ceiling from anywhere."""
    copier = copier_factory(make_config(num_slaves=1))

    assert _cap_in_task(copier, make_position) == 0.0


def test_a_live_edit_reaches_the_next_trade(
    copier_factory, make_config, make_position
):
    """Changed in the Rules dialog, not on restart — same as every other
    per-slave risk field."""
    copier = copier_factory(make_config(
        num_slaves=1, slave_overrides=[{"max_risk_pct_per_trade": 1.0}],
    ))
    sid = copier.config.slaves[0].slave_id

    get_live_settings().update_slave(sid, max_risk_pct_per_trade=3.5)

    assert _cap_in_task(copier, make_position) == 3.5


def test_a_negative_cap_is_rejected(make_config):
    with pytest.raises(ValueError, match="max_risk_pct_per_trade must be >= 0"):
        make_config(num_slaves=1, slave_overrides=[{"max_risk_pct_per_trade": -1.0}])


# ==================== the legacy global value ====================

_LEGACY_YAML = textwrap.dedent("""\
    master:
      terminal_path: C:\\MT5\\terminal64.exe
    slave_terminal:
      terminal_path: C:\\MT5 Slave\\terminal64.exe
    slaves:
      - name: prop
        login: 26401008
        password: pass
        server: FivePercentOnline-Real
      - name: small
        login: 2089148
        password: pass
        server: Bybit-Live
        max_risk_pct_per_trade: 25.0
    settings:
      max_risk_pct_per_trade: 1.0
""")


@pytest.fixture
def legacy_config_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(_LEGACY_YAML, encoding="utf-8")
    return path


def test_legacy_global_cap_is_handed_to_slaves_that_have_none(legacy_config_file):
    """A file written before the move must keep guarding exactly as it did —
    silently dropping the cap would be the dangerous direction."""
    config = Config.load(legacy_config_file)

    assert config.slaves[0].max_risk_pct_per_trade == 1.0


def test_a_slaves_own_cap_survives_the_migration(legacy_config_file):
    """The explicit per-slave value wins; the legacy number does not overwrite
    the very setting that exists to fix it."""
    config = Config.load(legacy_config_file)

    assert config.slaves[1].max_risk_pct_per_trade == 25.0


def test_the_global_cap_is_dropped_after_migrating(legacy_config_file):
    """Read once, then gone: while it still held a value, two sources of truth
    could disagree about what guards a slave."""
    config = Config.load(legacy_config_file)

    assert config.settings.max_risk_pct_per_trade == 0.0


def test_gui_load_migrates_the_raw_dict_too(legacy_config_file):
    """The GUI reads the YAML itself, so it needs the same migration — without
    it the Rules dialog would show 0 for a slave the engine is guarding."""
    data = ConfigManager(legacy_config_file).load()

    assert data["slaves"][0]["max_risk_pct_per_trade"] == 1.0
    assert data["slaves"][1]["max_risk_pct_per_trade"] == 25.0
    assert "max_risk_pct_per_trade" not in data["settings"]


def test_a_gui_save_does_not_resurrect_the_global_key(legacy_config_file):
    """Dropping it on load is what makes the next save final — a rewritten file
    must not carry the key back for the following load to migrate again."""
    manager = ConfigManager(legacy_config_file)
    manager.save(manager.load())

    written = yaml.safe_load(legacy_config_file.read_text(encoding="utf-8"))
    assert "max_risk_pct_per_trade" not in written["settings"]
    assert written["slaves"][0]["max_risk_pct_per_trade"] == 1.0


def test_live_settings_carries_the_cap_per_slave(make_config):
    """The copy path reads LiveSettings, not Config — the value has to land in
    the per-slave record, not in a global field that no longer exists."""
    config = make_config(
        num_slaves=2,
        slave_overrides=[
            {"max_risk_pct_per_trade": 1.0},
            {"max_risk_pct_per_trade": 25.0},
        ],
    )
    settings = get_live_settings()
    settings.update_from_config(config)

    caps = [
        settings.get_slave(s.slave_id).max_risk_pct_per_trade
        for s in config.slaves
    ]
    assert caps == [1.0, 25.0]
    assert not hasattr(settings, "max_risk_pct_per_trade")
