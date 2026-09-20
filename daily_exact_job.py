"""Idempotent hourly job that imports the latest completed UTC aggTrades day."""

from __future__ import annotations

import argparse
import datetime as dt
import os
from pathlib import Path

from exact_poc import backfill_day
from poc_store import load_levels


def main() -> None:
    parser = argparse.ArgumentParser(description="Import latest completed UTC aggTrades archive when available")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--database", type=Path, default=Path(os.getenv("DATABASE_PATH", "data/poc.sqlite3")))
    parser.add_argument("--cache", type=Path, default=Path(os.getenv("AGGTRADES_CACHE", "data/aggtrades")))
    args = parser.parse_args()
    symbol = args.symbol.upper()
    day = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    existing = load_levels(symbol, bin_size=10.0, method="aggTrades", path=args.database)
    if not existing.empty and day in set(existing["day"].dt.date):
        print(f"{day}: přesný profil už je uložený.")
        return
    try:
        backfill_day(symbol, day, 10.0, args.database, args.cache)
    except FileNotFoundError:
        # Publishing is asynchronous; the next hourly run retries normally.
        print(f"{day}: archiv zatím není publikovaný, další běh to zkusí znovu.")


if __name__ == "__main__":
    main()
