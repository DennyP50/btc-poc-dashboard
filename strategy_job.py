"""Compute and persist P0 strategy states from exact levels and closed 4H bars."""

from __future__ import annotations

import argparse
import datetime as dt
import os
from pathlib import Path

import pandas as pd
import requests

from poc_store import load_levels, save_strategy_setups
from strategy_engine import evaluate_p0


API_BASE = os.getenv("BINANCE_API_BASE", "https://fapi.binance.com").rstrip("/")
KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "trades", "taker_buy_volume", "taker_buy_quote_volume", "ignore",
]


def load_closed_4h(symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Fetch 4H candles over an arbitrary range and discard the open candle."""
    cursor = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    rows: list[list] = []
    with requests.Session() as session:
        while cursor <= end_ms:
            response = session.get(
                f"{API_BASE}/fapi/v1/klines",
                params={"symbol": symbol, "interval": "4h", "startTime": cursor, "endTime": end_ms, "limit": 1500},
                timeout=30,
            )
            response.raise_for_status()
            batch = response.json()
            if not batch:
                break
            rows.extend(batch)
            next_cursor = int(batch[-1][0]) + 4 * 60 * 60 * 1000
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            if len(batch) < 1500:
                break
    if not rows:
        raise RuntimeError("Binance nevrátila žádné 4H svíčky.")
    frame = pd.DataFrame(rows, columns=KLINE_COLUMNS)
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["ts"] = pd.to_datetime(frame["open_time"], unit="ms", utc=True)
    frame["close_time"] = pd.to_datetime(frame["close_time"], unit="ms", utc=True)
    frame = frame.dropna(subset=["ts", "open", "high", "low", "close"]).drop_duplicates("ts").set_index("ts").sort_index()
    closed = frame[frame["close_time"] <= end]
    if len(closed) > 1:
        gaps = closed.index.to_series().diff().dropna()
        invalid = gaps[gaps != pd.Timedelta(hours=4)]
        if not invalid.empty:
            first_gap = invalid.index[0]
            raise RuntimeError(f"Mezera v 4H datech před {first_gap.isoformat()}: {invalid.iloc[0]}.")
    return closed


def main() -> None:
    parser = argparse.ArgumentParser(description="Update persisted P0 states from closed 4H candles")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--database", type=Path, default=Path(os.getenv("DATABASE_PATH", "data/poc.sqlite3")))
    parser.add_argument("--as-of", type=dt.datetime.fromisoformat, default=None, help="UTC ISO timestamp for deterministic backtests")
    args = parser.parse_args()
    symbol = args.symbol.upper()
    levels = load_levels(symbol, bin_size=10.0, method="aggTrades", path=args.database)
    if levels.empty:
        raise SystemExit("Databáze neobsahuje přesné $10 aggTrades profily.")
    end = pd.Timestamp(args.as_of, tz="UTC") if args.as_of and args.as_of.tzinfo is None else pd.Timestamp(args.as_of or pd.Timestamp.now(tz="UTC"))
    start = levels["day"].min() - pd.Timedelta(days=4)
    bars = load_closed_4h(symbol, start, end)
    setups = evaluate_p0(levels, bars)
    save_strategy_setups(symbol, setups, bin_size=10.0, variant="P0", path=args.database)
    counts = {str(state): int(count) for state, count in setups["setup_state"].value_counts().items()}
    print(f"P0 aktualizováno: {len(setups)} úrovní, uzavřené 4H do {bars.index[-1].isoformat()}, stavy={counts}")


if __name__ == "__main__":
    main()
