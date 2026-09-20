"""Backfill exact $10 daily profiles from Binance USD-M aggTrades archives."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import os
import tempfile
import zipfile
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from poc_store import load_levels, mark_touched, record_ingestion, upsert_exact_profile


DATA_VISION_BASE = os.getenv(
    "BINANCE_DATA_VISION_BASE",
    "https://data.binance.vision/data/futures/um/daily/aggTrades",
).rstrip("/")
AGGTRADE_COLUMNS = [
    "agg_trade_id",
    "price",
    "quantity",
    "first_trade_id",
    "last_trade_id",
    "transact_time",
    "is_buyer_maker",
]


def archive_url(symbol: str, day: dt.date) -> str:
    filename = f"{symbol}-aggTrades-{day:%Y-%m-%d}.zip"
    return f"{DATA_VISION_BASE}/{symbol}/{filename}"


def download_archive(symbol: str, day: dt.date, cache_dir: Path) -> tuple[Path, str]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"{symbol}-aggTrades-{day:%Y-%m-%d}.zip"
    digest = hashlib.sha256()
    if target.exists():
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        actual = digest.hexdigest()
        _verify_checksum(symbol, day, actual)
        return target, actual

    response = requests.get(archive_url(symbol, day), stream=True, timeout=(15, 180))
    if response.status_code == 404:
        raise FileNotFoundError(f"Archiv pro {day:%Y-%m-%d} ještě není dostupný.")
    response.raise_for_status()
    with tempfile.NamedTemporaryFile(dir=cache_dir, suffix=".part", delete=False) as temp:
        temp_path = Path(temp.name)
        for chunk in response.iter_content(1024 * 1024):
            if chunk:
                temp.write(chunk)
                digest.update(chunk)
    temp_path.replace(target)
    actual = digest.hexdigest()
    try:
        _verify_checksum(symbol, day, actual)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return target, actual


def _verify_checksum(symbol: str, day: dt.date, actual: str) -> None:
    checksum_url = f"{archive_url(symbol, day)}.CHECKSUM"
    response = requests.get(checksum_url, timeout=30)
    response.raise_for_status()
    expected = response.text.strip().split()[0].lower()
    if len(expected) != 64 or expected != actual.lower():
        raise ValueError(f"SHA256 kontrola archivu pro {day} selhala.")


def profile_from_archive(
    path: Path,
    bin_size: float = 10.0,
    watched_levels: dict[str, float] | None = None,
    expected_day: dt.date | None = None,
) -> tuple[float, float, float, dict[float, float], int, dict[str, pd.Timestamp]]:
    """Stream a zipped CSV and aggregate base volume into fixed price bins."""
    if bin_size <= 0:
        raise ValueError("Bin musí být kladný.")
    volumes: defaultdict[float, float] = defaultdict(float)
    rows_processed = 0
    remaining = dict(watched_levels or {})
    touches: dict[str, pd.Timestamp] = {}
    last_price: float | None = None
    with zipfile.ZipFile(path) as archive:
        members = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(members) != 1:
            raise ValueError(f"Archiv musí obsahovat právě jeden CSV soubor: {path}")
        with archive.open(members[0]) as raw:
            # Archives can contain a header. Numeric coercion safely drops it.
            for chunk in pd.read_csv(
                raw,
                header=None,
                names=AGGTRADE_COLUMNS,
                usecols=["price", "quantity", "transact_time"],
                dtype={"price": "string", "quantity": "string", "transact_time": "string"},
                chunksize=500_000,
            ):
                price = pd.to_numeric(chunk["price"], errors="coerce")
                quantity = pd.to_numeric(chunk["quantity"], errors="coerce")
                transact_time = pd.to_numeric(chunk["transact_time"], errors="coerce")
                valid = price.notna() & quantity.notna() & transact_time.notna() & (quantity > 0)
                if not valid.any():
                    continue
                prices = price[valid].to_numpy(dtype=float)
                quantities = quantity[valid].to_numpy(dtype=float)
                timestamps = transact_time[valid].to_numpy(dtype=np.int64)
                timestamps_ms = np.where(timestamps > 100_000_000_000_000, timestamps // 1000, timestamps)
                if expected_day is not None:
                    day_start_ms = int(pd.Timestamp(expected_day, tz="UTC").timestamp() * 1000)
                    day_end_ms = day_start_ms + 86_400_000
                    if np.any(timestamps_ms < day_start_ms) or np.any(timestamps_ms >= day_end_ms):
                        raise ValueError(f"Archiv obsahuje timestamp mimo UTC den {expected_day}.")
                price_bins = np.floor(prices / bin_size) * bin_size
                grouped = pd.Series(quantities).groupby(price_bins).sum()
                for price_bin, volume in grouped.items():
                    volumes[float(price_bin)] += float(volume)
                rows_processed += int(valid.sum())

                if remaining:
                    previous = np.empty_like(prices)
                    previous[0] = prices[0] if last_price is None else last_price
                    previous[1:] = prices[:-1]
                    for level_day, level_price in list(remaining.items()):
                        crossed = ((previous <= level_price) & (prices >= level_price)) | ((previous >= level_price) & (prices <= level_price))
                        positions = np.flatnonzero(crossed)
                        if positions.size:
                            raw_time = int(timestamps_ms[int(positions[0])])
                            touches[level_day] = pd.to_datetime(raw_time, unit="ms", utc=True)
                            del remaining[level_day]
                last_price = float(prices[-1])

    if not volumes:
        raise ValueError(f"Archiv neobsahuje validní aggTrades: {path}")
    poc, vah, val = levels_from_bins(volumes, bin_size)
    return poc, vah, val, dict(sorted(volumes.items())), rows_processed, touches


def levels_from_bins(volumes: dict[float, float], bin_size: float, value_area: float = 0.70) -> tuple[float, float, float]:
    prices = np.array(sorted(volumes), dtype=float)
    volume = np.array([volumes[price] for price in prices], dtype=float)
    full_prices = np.arange(prices.min(), prices.max() + bin_size * 0.5, bin_size)
    full_volume = np.zeros(len(full_prices), dtype=float)
    positions = np.rint((prices - full_prices[0]) / bin_size).astype(int)
    full_volume[positions] = volume

    poc_index = int(full_volume.argmax())
    lower = upper = poc_index
    accumulated = full_volume[poc_index]
    target = full_volume.sum() * value_area
    while accumulated < target and (lower > 0 or upper < len(full_volume) - 1):
        above = full_volume[upper + 1] if upper + 1 < len(full_volume) else -1.0
        below = full_volume[lower - 1] if lower > 0 else -1.0
        if above >= below:
            upper += 1
            accumulated += above
        else:
            lower -= 1
            accumulated += below
    return (
        float(full_prices[poc_index] + bin_size / 2),
        float(full_prices[upper] + bin_size),
        float(full_prices[lower]),
    )


def backfill_day(symbol: str, day: dt.date, bin_size: float, database: Path, cache_dir: Path) -> None:
    source = archive_url(symbol, day)
    record_ingestion(symbol, day.isoformat(), source, "RUNNING", path=database)
    try:
        archive, sha256 = download_archive(symbol, day, cache_dir)
        existing = load_levels(symbol, bin_size=bin_size, method="aggTrades", path=database)
        watched = {
            row.day.date().isoformat(): float(row.poc)
            for row in existing.itertuples(index=False)
            if row.day.date() < day and pd.isna(row.touched_at)
        }
        poc, vah, val, bins, count, touches = profile_from_archive(archive, bin_size, watched, expected_day=day)
        for level_day, touched_at in touches.items():
            mark_touched(symbol, level_day, bin_size, touched_at, path=database)
        upsert_exact_profile(
            symbol=symbol,
            day=day.isoformat(),
            bin_size=bin_size,
            poc=poc,
            vah=vah,
            val=val,
            bins=bins,
            trade_count=count,
            source_sha256=sha256,
            path=database,
        )
        record_ingestion(symbol, day.isoformat(), source, "COMPLETE", count, path=database)
        print(f"{day}: POC={poc:.2f} VA={val:.2f}–{vah:.2f} trades={count:,}")
    except Exception as exc:
        record_ingestion(symbol, day.isoformat(), source, "FAILED", message=str(exc), path=database)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill exact daily POC from Binance aggTrades")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--start", required=True, type=dt.date.fromisoformat)
    parser.add_argument("--end", required=True, type=dt.date.fromisoformat)
    parser.add_argument("--bin", type=float, default=10.0)
    parser.add_argument("--database", type=Path, default=Path(os.getenv("DATABASE_PATH", "data/poc.sqlite3")))
    parser.add_argument("--cache", type=Path, default=Path(os.getenv("AGGTRADES_CACHE", "data/aggtrades")))
    args = parser.parse_args()
    if args.bin != 10.0:
        raise SystemExit("P0 backtest vyžaduje pevný bin $10.")
    if args.end < args.start:
        raise SystemExit("--end musí být stejný nebo pozdější než --start.")
    day = args.start
    while day <= args.end:
        backfill_day(args.symbol.upper(), day, args.bin, args.database, args.cache)
        day += dt.timedelta(days=1)


if __name__ == "__main__":
    main()
