"""Persist funding and recent 4H open-interest context without changing P0."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pandas as pd
import requests

from poc_store import save_market_context


API_BASE = os.getenv("BINANCE_API_BASE", "https://fapi.binance.com").rstrip("/")


def fetch_context(symbol: str, days: int = 30) -> tuple[pd.DataFrame, pd.DataFrame]:
    end = pd.Timestamp.now(tz="UTC")
    start = end - pd.Timedelta(days=min(max(days, 1), 30))
    params = {"symbol": symbol, "startTime": int(start.timestamp() * 1000), "endTime": int(end.timestamp() * 1000)}
    with requests.Session() as session:
        funding_response = session.get(f"{API_BASE}/fapi/v1/fundingRate", params={**params, "limit": 1000}, timeout=30)
        funding_response.raise_for_status()
        oi_response = session.get(
            f"{API_BASE}/futures/data/openInterestHist",
            params={**params, "period": "4h", "limit": 500},
            timeout=30,
        )
        oi_response.raise_for_status()

    funding_raw = funding_response.json()
    funding = pd.DataFrame(funding_raw)
    if funding.empty:
        funding = pd.DataFrame(columns=["funding_time", "funding_rate", "mark_price"])
    else:
        funding = funding.rename(columns={"fundingTime": "funding_time", "fundingRate": "funding_rate", "markPrice": "mark_price"})
        if "mark_price" not in funding:
            funding["mark_price"] = pd.NA
        funding["funding_time"] = pd.to_datetime(funding["funding_time"], unit="ms", utc=True)
        funding["funding_rate"] = pd.to_numeric(funding["funding_rate"], errors="raise")
        funding["mark_price"] = pd.to_numeric(funding["mark_price"], errors="coerce")
        funding = funding[["funding_time", "funding_rate", "mark_price"]].drop_duplicates("funding_time")

    oi_raw = oi_response.json()
    open_interest = pd.DataFrame(oi_raw)
    if open_interest.empty:
        open_interest = pd.DataFrame(columns=["timestamp", "open_interest", "open_interest_value"])
    else:
        open_interest = open_interest.rename(
            columns={"sumOpenInterest": "open_interest", "sumOpenInterestValue": "open_interest_value"}
        )
        open_interest["timestamp"] = pd.to_datetime(open_interest["timestamp"], unit="ms", utc=True)
        open_interest["open_interest"] = pd.to_numeric(open_interest["open_interest"], errors="raise")
        open_interest["open_interest_value"] = pd.to_numeric(open_interest["open_interest_value"], errors="raise")
        open_interest = open_interest[["timestamp", "open_interest", "open_interest_value"]].drop_duplicates("timestamp").sort_values("timestamp")
        gaps = open_interest["timestamp"].diff().dropna()
        invalid = gaps[gaps != pd.Timedelta(hours=4)]
        if not invalid.empty:
            raise RuntimeError(f"Mezera v OI 4H datech: {invalid.iloc[0]}.")
    return funding, open_interest


def main() -> None:
    parser = argparse.ArgumentParser(description="Update funding and 4H OI context data")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--database", type=Path, default=Path(os.getenv("DATABASE_PATH", "data/poc.sqlite3")))
    args = parser.parse_args()
    funding, open_interest = fetch_context(args.symbol.upper(), args.days)
    save_market_context(args.symbol, funding, open_interest, args.database)
    print(f"Kontext uložen: funding={len(funding)}, OI 4H={len(open_interest)}. P0 pravidla zůstala beze změny.")


if __name__ == "__main__":
    main()
