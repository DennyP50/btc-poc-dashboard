import numpy as np
import pandas as pd

from poc_engine import calculate_daily_levels, day_profile, resample_candles, summarize_market


def _frame(days=2):
    ts = pd.date_range("2026-01-01", periods=days * 1440, freq="min", tz="UTC")
    base = np.where(ts.day == 1, 100.0, 110.0)
    return pd.DataFrame(
        {
            "ts": ts,
            "open": base,
            "high": base + 10,
            "low": base,
            "close": base + 5,
            "volume": 1.0,
        }
    )


def test_flat_distribution_profile():
    frame = _frame(1)
    poc, vah, val = day_profile(frame, bin_size=5)
    assert poc in (102.5, 107.5)
    assert val == 100.0
    assert vah == 110.0


def test_daily_levels_and_touch_detection():
    levels = calculate_daily_levels(_frame(2), bin_size=5)
    assert len(levels) == 2
    assert levels.iloc[0]["minutes"] == 1440
    assert pd.isna(levels.iloc[0]["touched_at"])


def test_resample_and_summary():
    frame = _frame(2)
    levels = calculate_daily_levels(frame, bin_size=5)
    candles = resample_candles(frame, "1h")
    assert len(candles) == 48
    summary = summarize_market(frame, levels)
    assert summary.level_day == "01.01.2026"
    assert summary.context == "Nad value area"
