"""Night mode: scan every NIGHT_INTERVAL seconds between NIGHT_START and NIGHT_END."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from mcore import interval as iv

AMS = ZoneInfo("Europe/Amsterdam")


def _cfg(**kw):
    base = dict(check_interval=300, peak_interval=60, peak_start="08:30", peak_end="10:00",
                peak_start_2="13:30", peak_end_2="15:00", peak_weekdays_only=True,
                night_interval=7200, night_start="00:00", night_end="07:00")
    base.update(kw)
    return SimpleNamespace(**base)


def _at(h, m, cfg):
    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 3, h, m, tzinfo=AMS)
    with patch.object(iv, "datetime", _DT):
        return iv.get_interval(cfg)


def test_night_uses_long_interval():
    assert _at(1, 0, _cfg()) == (7200, False)


def test_night_wait_capped_at_window_end():
    assert _at(5, 30, _cfg()) == (5400, False)
    assert _at(6, 59, _cfg()) == (300, False)   # never faster than the normal interval


def test_day_unchanged():
    assert _at(12, 0, _cfg()) == (300, False)
    assert _at(9, 0, _cfg()) == (60, True)


def test_wraps_midnight_and_can_be_disabled():
    assert _at(23, 30, _cfg(night_start="23:00")) == (7200, False)
    assert _at(3, 0, _cfg(night_interval=0)) == (300, False)
