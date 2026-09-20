import numpy as np
import pandas as pd

from strategy_engine import SetupState, closed_four_hour_bars, evaluate_p0


def _bars():
    index = pd.date_range("2026-01-01", periods=30, freq="4h", tz="UTC")
    bars = pd.DataFrame(
        {
            "open": 105.0,
            "high": 106.0,
            "low": 104.0,
            "close": 105.0,
            "volume": 1.0,
        },
        index=index,
    )
    # Source level becomes available on Jan 4. Confirmation and retest must be
    # separate closed bars.
    bars.loc[pd.Timestamp("2026-01-04 00:00", tz="UTC"), ["open", "high", "low", "close"]] = [101, 102, 97, 98]
    bars.loc[pd.Timestamp("2026-01-04 04:00", tz="UTC"), ["open", "high", "low", "close"]] = [98, 101, 97, 98]
    bars["close_time"] = bars.index + pd.Timedelta(hours=4)
    return bars


def _levels():
    return pd.DataFrame(
        [
            {"day": pd.Timestamp("2026-01-01", tz="UTC"), "poc": 90.0, "vah": 92.0, "val": 88.0, "touched_at": pd.NaT},
            {"day": pd.Timestamp("2026-01-03", tz="UTC"), "poc": 100.0, "vah": 102.0, "val": 98.0, "touched_at": pd.NaT},
        ]
    )


def test_entry_requires_later_retest_and_uses_historical_target():
    result = evaluate_p0(_levels(), _bars())
    setup = result[result["poc"] == 100.0].iloc[0]
    assert setup["setup_state"] == SetupState.ENTRY_READY
    assert setup["direction"] == "SHORT"
    assert setup["confirmation_time"] == pd.Timestamp("2026-01-04 00:00", tz="UTC")
    assert setup["entry_time"] == pd.Timestamp("2026-01-04 04:00", tz="UTC")
    assert setup["retest_bars_used"] == 1
    assert setup["target_poc"] == 90.0
    assert setup["stop"] > 101.0
    assert setup["target_r"] > 0


def test_setup_expires_after_six_closed_bars_without_retest():
    bars = _bars()
    window = pd.date_range("2026-01-04 04:00", periods=6, freq="4h", tz="UTC")
    bars.loc[window, ["open", "high", "low", "close"]] = [98.0, 99.0, 97.0, 98.0]
    result = evaluate_p0(_levels(), bars)
    setup = result[result["poc"] == 100.0].iloc[0]
    assert setup["setup_state"] == SetupState.EXPIRED
    assert setup["expiry_reason"] == "RETEST_WINDOW_EXPIRED"
    assert setup["retest_bars_used"] == 6


def test_incomplete_four_hour_bar_is_separated():
    minute_index = pd.date_range("2026-01-04 00:00", periods=360, freq="min", tz="UTC")
    minute = pd.DataFrame(
        {"ts": minute_index, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1.0}
    )
    closed, incomplete = closed_four_hour_bars(minute, pd.Timestamp("2026-01-04 06:00", tz="UTC"))
    assert list(closed.index) == [pd.Timestamp("2026-01-04 00:00", tz="UTC")]
    assert list(incomplete.index) == [pd.Timestamp("2026-01-04 04:00", tz="UTC")]
