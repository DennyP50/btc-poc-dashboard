"""Market data and daily volume-profile calculations for the dashboard.

The profile is an approximation: a one-minute candle's volume is distributed
uniformly over the price range traded by that candle. Tick/aggTrade data would
be required for an exact volume-at-price profile.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


API_BASE = os.getenv("BINANCE_API_BASE", "https://fapi.binance.com").rstrip("/")
REQUEST_TIMEOUT = float(os.getenv("BINANCE_REQUEST_TIMEOUT", "15"))
CACHE_TTL_SECONDS = max(30, int(os.getenv("CACHE_TTL_SECONDS", "180")))

KLINE_COLUMNS = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trades",
    "taker_buy_volume",
    "taker_buy_quote_volume",
    "ignore",
]

_fetch_lock = threading.Lock()


class MarketDataError(RuntimeError):
    """A readable error raised when market data cannot be loaded."""


@dataclass(frozen=True)
class MarketSummary:
    price: float
    change_24h_pct: float
    poc: float
    vah: float
    val: float
    distance_to_poc_pct: float
    context: str
    context_class: str
    level_day: str
    updated_at: pd.Timestamp


def _session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.4,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": "btc-poc-dashboard/1.0"})
    return session


def _utc_now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC").floor("min")


@lru_cache(maxsize=12)
def _fetch_recent_cached(symbol: str, days: int, cache_bucket: int) -> pd.DataFrame:
    del cache_bucket
    return _fetch_recent(symbol, days)


def load_recent_klines(symbol: str, days: int) -> pd.DataFrame:
    """Load recent 1m perpetual-futures klines with a short process cache."""
    symbol = symbol.strip().upper()
    if not symbol or not symbol.replace("_", "").isalnum():
        raise ValueError("Neplatný symbol trhu.")
    days = min(max(int(days), 2), 40)
    bucket = int(time.time() // CACHE_TTL_SECONDS)
    # lru_cache itself is thread-safe, but concurrent misses can still duplicate
    # the same expensive request. Serialize only cache misses/calls.
    with _fetch_lock:
        return _fetch_recent_cached(symbol, days, bucket).copy()


def _fetch_recent(symbol: str, days: int) -> pd.DataFrame:
    end = _utc_now()
    start = end - pd.Timedelta(days=days)
    cursor = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    rows: list[list] = []
    session = _session()

    try:
        while cursor <= end_ms:
            response = session.get(
                f"{API_BASE}/fapi/v1/klines",
                params={
                    "symbol": symbol,
                    "interval": "1m",
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": 1500,
                },
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code == 451:
                raise MarketDataError(
                    "Binance API není z této lokace dostupné (HTTP 451). "
                    "Nastavte BINANCE_API_BASE na povolený kompatibilní endpoint."
                )
            response.raise_for_status()
            batch = response.json()
            if not isinstance(batch, list):
                detail = batch.get("msg", "neočekávaná odpověď") if isinstance(batch, dict) else "neočekávaná odpověď"
                raise MarketDataError(f"Binance vrátila chybu: {detail}")
            if not batch:
                break
            rows.extend(batch)
            next_cursor = int(batch[-1][0]) + 60_000
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            if len(batch) < 1500:
                break
    except requests.RequestException as exc:
        raise MarketDataError(f"Tržní data se nepodařilo načíst: {exc}") from exc
    finally:
        session.close()

    if not rows:
        raise MarketDataError(f"Binance nevrátila žádná data pro {symbol}.")

    frame = pd.DataFrame(rows, columns=KLINE_COLUMNS)
    numeric = ["open", "high", "low", "close", "volume", "quote_volume"]
    frame[numeric] = frame[numeric].apply(pd.to_numeric, errors="coerce")
    frame["ts"] = pd.to_datetime(frame["open_time"], unit="ms", utc=True)
    frame = (
        frame.dropna(subset=["ts", *numeric])
        .drop_duplicates("ts", keep="last")
        .sort_values("ts")
        .reset_index(drop=True)
    )
    return frame


def day_profile(day: pd.DataFrame, bin_size: float, value_area: float = 0.70) -> tuple[float, float, float]:
    """Return POC, VAH and VAL for one day of one-minute candles."""
    if day.empty:
        raise ValueError("Denní profil nelze spočítat bez dat.")
    if bin_size <= 0:
        raise ValueError("Velikost cenového koše musí být kladná.")
    if not 0 < value_area <= 1:
        raise ValueError("Value area musí být v rozsahu (0, 1>.")

    low = np.floor(day["low"].min() / bin_size) * bin_size
    high = np.ceil(day["high"].max() / bin_size) * bin_size
    if high <= low:
        high = low + bin_size
    edges = np.arange(low, high + bin_size * 1.01, bin_size, dtype=float)
    volume_by_price = np.zeros(len(edges) - 1, dtype=float)

    for candle_low, candle_high, candle_volume in day[["low", "high", "volume"]].itertuples(index=False, name=None):
        if candle_volume <= 0:
            continue
        if candle_high <= candle_low:
            idx = int(np.clip(np.searchsorted(edges, candle_low, side="right") - 1, 0, len(volume_by_price) - 1))
            volume_by_price[idx] += candle_volume
            continue

        first = int(np.clip(np.searchsorted(edges, candle_low, side="right") - 1, 0, len(volume_by_price) - 1))
        last = int(np.clip(np.searchsorted(edges, candle_high, side="left"), first, len(volume_by_price) - 1))
        left = edges[first : last + 1]
        right = edges[first + 1 : last + 2]
        overlap = np.maximum(0.0, np.minimum(right, candle_high) - np.maximum(left, candle_low))
        overlap_total = overlap.sum()
        if overlap_total > 0:
            volume_by_price[first : last + 1] += candle_volume * overlap / overlap_total
        else:
            volume_by_price[first] += candle_volume

    if volume_by_price.sum() <= 0:
        raise ValueError("Denní profil neobsahuje objem.")

    poc_idx = int(volume_by_price.argmax())
    lower_idx = upper_idx = poc_idx
    accumulated = volume_by_price[poc_idx]
    target = value_area * volume_by_price.sum()
    while accumulated < target and (lower_idx > 0 or upper_idx < len(volume_by_price) - 1):
        above = volume_by_price[upper_idx + 1] if upper_idx + 1 < len(volume_by_price) else -1.0
        below = volume_by_price[lower_idx - 1] if lower_idx > 0 else -1.0
        if above >= below:
            upper_idx += 1
            accumulated += above
        else:
            lower_idx -= 1
            accumulated += below

    poc = edges[poc_idx] + bin_size / 2
    vah = edges[upper_idx + 1]
    val = edges[lower_idx]
    return float(poc), float(vah), float(val)


def calculate_daily_levels(klines: pd.DataFrame, bin_size: float) -> pd.DataFrame:
    """Calculate levels and first post-session touch for every UTC day."""
    data = klines.copy()
    data["day"] = data["ts"].dt.floor("D")
    records: list[dict] = []
    for day_start, group in data.groupby("day", sort=True):
        poc, vah, val = day_profile(group, bin_size)
        records.append(
            {
                "day": day_start,
                "poc": poc,
                "vah": vah,
                "val": val,
                "minutes": int(group["ts"].nunique()),
            }
        )

    levels = pd.DataFrame(records)
    if levels.empty:
        return levels

    touched_at: list[pd.Timestamp | pd.NaT] = []
    for row in levels.itertuples(index=False):
        following = data[data["ts"] >= row.day + pd.Timedelta(days=1)]
        hit = following[(following["low"] <= row.poc) & (following["high"] >= row.poc)]
        touched_at.append(hit.iloc[0]["ts"] if not hit.empty else pd.NaT)
    levels["touched_at"] = touched_at
    return levels


def resample_candles(klines: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    rules = {"15m": "15min", "1h": "1h", "4h": "4h"}
    if timeframe not in rules:
        raise ValueError("Nepodporovaný timeframe.")
    result = (
        klines.set_index("ts")
        .resample(rules[timeframe], label="left", closed="left")
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        )
        .dropna()
    )
    return result


def summarize_market(klines: pd.DataFrame, levels: pd.DataFrame) -> MarketSummary:
    now_day = klines["ts"].max().floor("D")
    completed = levels[levels["day"] < now_day]
    if completed.empty:
        raise ValueError("Chybí dokončený den pro předchozí denní úrovně.")
    level = completed.iloc[-1]
    price = float(klines.iloc[-1]["close"])
    cutoff = klines.iloc[-1]["ts"] - pd.Timedelta(hours=24)
    older = klines[klines["ts"] <= cutoff]
    reference = float(older.iloc[-1]["close"]) if not older.empty else float(klines.iloc[0]["open"])
    change = (price / reference - 1) * 100 if reference else 0.0

    if price > level["vah"]:
        context, css_class = "Nad value area", "above"
    elif price < level["val"]:
        context, css_class = "Pod value area", "below"
    else:
        context, css_class = "Uvnitř value area", "inside"

    return MarketSummary(
        price=price,
        change_24h_pct=change,
        poc=float(level["poc"]),
        vah=float(level["vah"]),
        val=float(level["val"]),
        distance_to_poc_pct=(price / float(level["poc"]) - 1) * 100,
        context=context,
        context_class=css_class,
        level_day=level["day"].strftime("%d.%m.%Y"),
        updated_at=klines.iloc[-1]["ts"],
    )
