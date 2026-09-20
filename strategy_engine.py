"""Lookahead-safe P0 state machine for daily POC retests."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum

import numpy as np
import pandas as pd


class SetupState(StrEnum):
    NAKED = "NAKED"
    FOUR_H_CLOSE = "4H_CLOSE"
    WAIT_RETEST = "WAIT_RETEST"
    ENTRY_READY = "ENTRY_READY"
    EXPIRED = "EXPIRED"


@dataclass
class SetupSnapshot:
    level_day: pd.Timestamp
    poc: float
    level_status: str
    touched_at: pd.Timestamp | pd.NaT
    setup_state: str
    direction: str | None = None
    confirmation_time: pd.Timestamp | pd.NaT = pd.NaT
    retest_bars_used: int = 0
    retest_bars_left: int = 6
    entry_time: pd.Timestamp | pd.NaT = pd.NaT
    entry: float | None = None
    stop: float | None = None
    target_poc: float | None = None
    target_day: pd.Timestamp | pd.NaT = pd.NaT
    target_distance_pct: float | None = None
    target_r: float | None = None
    expiry_reason: str | None = None


def closed_four_hour_bars(klines: pd.DataFrame, as_of: pd.Timestamp | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split resampled 4H candles into closed and current incomplete bars."""
    as_of = as_of or pd.Timestamp.now(tz="UTC")
    if as_of.tzinfo is None:
        as_of = as_of.tz_localize("UTC")
    bars = (
        klines.set_index("ts")
        .resample("4h", label="left", closed="left")
        .agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"), volume=("volume", "sum"))
        .dropna()
    )
    bars["close_time"] = bars.index + pd.Timedelta(hours=4)
    closed = bars[bars["close_time"] <= as_of].copy()
    incomplete = bars[bars["close_time"] > as_of].copy()
    return closed, incomplete


def atr_wilder(bars: pd.DataFrame, length: int = 14) -> pd.Series:
    previous_close = bars["close"].shift(1)
    true_range = pd.concat(
        [
            bars["high"] - bars["low"],
            (bars["high"] - previous_close).abs(),
            (bars["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()


def first_touch(level: float, bars: pd.DataFrame, available_from: pd.Timestamp) -> pd.Timestamp | pd.NaT:
    eligible = bars[bars.index >= available_from]
    hit = eligible[(eligible["low"] <= level) & (eligible["high"] >= level)]
    return hit.index[0] if not hit.empty else pd.NaT


def _target_for_entry(
    levels: pd.DataFrame,
    source_day: pd.Timestamp,
    entry_time: pd.Timestamp,
    entry: float,
    direction: str,
) -> pd.Series | None:
    # A target must have existed before entry and still be untouched at entry.
    known = levels[(levels["day"] < entry_time.floor("D")) & (levels["day"] != source_day)].copy()
    known = known[known["touched_at"].isna() | (known["touched_at"] > entry_time)]
    if direction == "LONG":
        candidates = known[known["poc"] > entry].sort_values("poc")
    else:
        candidates = known[known["poc"] < entry].sort_values("poc", ascending=False)
    return None if candidates.empty else candidates.iloc[0]


def evaluate_p0(
    levels: pd.DataFrame,
    closed_bars: pd.DataFrame,
    max_retest_bars: int = 6,
    stop_atr_multiple: float = 0.25,
) -> pd.DataFrame:
    """Evaluate every daily POC using only closed 4H candles.

    Confirmation requires a bar to trade through/touch the POC and close on one
    side. A later bar (never the confirmation bar) must retest the POC with its
    wick and close back in the confirmed direction within six closed bars.
    """
    if levels.empty:
        return pd.DataFrame()
    bars = closed_bars.copy().sort_index()
    bars["atr14"] = atr_wilder(bars)
    prepared = levels.copy().sort_values("day")
    if "touched_at" not in prepared:
        prepared["touched_at"] = pd.NaT
    prepared["touched_at"] = pd.to_datetime(prepared["touched_at"], utc=True, errors="coerce")

    # Derive first touches from the complete closed-bar history supplied to the
    # engine. Persistent exact levels may already carry a finer timestamp.
    for idx, level in prepared.iterrows():
        if pd.isna(level["touched_at"]):
            prepared.at[idx, "touched_at"] = first_touch(level["poc"], bars, level["day"] + pd.Timedelta(days=1))

    snapshots: list[dict] = []
    for level in prepared.itertuples(index=False):
        available_from = level.day + pd.Timedelta(days=1)
        future = bars[bars.index >= available_from]
        touched_at = level.touched_at
        base = SetupSnapshot(
            level_day=level.day,
            poc=float(level.poc),
            level_status="NAKED" if pd.isna(touched_at) else "TOUCHED",
            touched_at=touched_at,
            setup_state=SetupState.NAKED,
            retest_bars_left=max_retest_bars,
        )
        if future.empty:
            snapshots.append(asdict(base))
            continue

        # A confirmation bar must interact with the level; a close far away
        # cannot arm the setup merely because it happens to be on one side.
        confirms = future[(future["low"] <= level.poc) & (future["high"] >= level.poc)]
        if confirms.empty:
            snapshots.append(asdict(base))
            continue
        confirmation_time = confirms.index[0]
        confirmation = confirms.iloc[0]
        if confirmation["close"] < level.poc:
            direction = "SHORT"
        elif confirmation["close"] > level.poc:
            direction = "LONG"
        else:
            snapshots.append(asdict(base))
            continue

        base.setup_state = SetupState.FOUR_H_CLOSE
        base.direction = direction
        base.confirmation_time = confirmation_time
        candidates = future[future.index > confirmation_time].head(max_retest_bars)
        if candidates.empty:
            snapshots.append(asdict(base))
            continue

        base.setup_state = SetupState.WAIT_RETEST
        for bar_number, (bar_time, bar) in enumerate(candidates.iterrows(), start=1):
            base.retest_bars_used = bar_number
            base.retest_bars_left = max_retest_bars - bar_number
            if direction == "SHORT":
                retest = bar["high"] >= level.poc and bar["close"] < level.poc
                invalidated = bar["close"] >= level.poc
            else:
                retest = bar["low"] <= level.poc and bar["close"] > level.poc
                invalidated = bar["close"] <= level.poc

            if retest:
                atr = float(bar["atr14"]) if pd.notna(bar["atr14"]) else np.nan
                if not np.isfinite(atr):
                    base.setup_state = SetupState.EXPIRED
                    base.expiry_reason = "ATR14_UNAVAILABLE"
                    break
                entry = float(bar["close"])
                stop = float(bar["high"] + stop_atr_multiple * atr) if direction == "SHORT" else float(bar["low"] - stop_atr_multiple * atr)
                target = _target_for_entry(prepared, level.day, bar_time, entry, direction)
                base.setup_state = SetupState.ENTRY_READY
                base.entry_time = bar_time
                base.entry = entry
                base.stop = stop
                if target is not None:
                    target_poc = float(target["poc"])
                    reward = abs(target_poc - entry)
                    risk = abs(entry - stop)
                    base.target_poc = target_poc
                    base.target_day = target["day"]
                    base.target_distance_pct = reward / entry * 100
                    base.target_r = reward / risk if risk else None
                break
            if invalidated:
                base.setup_state = SetupState.EXPIRED
                base.expiry_reason = "CLOSE_BACK_THROUGH_POC"
                break
        else:
            if len(candidates) == max_retest_bars:
                base.setup_state = SetupState.EXPIRED
                base.expiry_reason = "RETEST_WINDOW_EXPIRED"

        snapshots.append(asdict(base))
    return pd.DataFrame(snapshots)
