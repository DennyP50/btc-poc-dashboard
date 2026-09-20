"""SQLite persistence for exact daily volume profiles and strategy snapshots."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pandas as pd


DEFAULT_DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "data/poc.sqlite3"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_levels (
    symbol TEXT NOT NULL,
    day TEXT NOT NULL,
    poc REAL NOT NULL,
    vah REAL NOT NULL,
    val REAL NOT NULL,
    bin_size REAL NOT NULL,
    method TEXT NOT NULL CHECK(method IN ('aggTrades', 'preview_1m')),
    trade_count INTEGER,
    source_sha256 TEXT,
    touched_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, day, bin_size, method)
);

CREATE INDEX IF NOT EXISTS idx_daily_levels_symbol_day
ON daily_levels(symbol, day);

CREATE TABLE IF NOT EXISTS profile_bins (
    symbol TEXT NOT NULL,
    day TEXT NOT NULL,
    bin_size REAL NOT NULL,
    price_bin REAL NOT NULL,
    volume REAL NOT NULL,
    PRIMARY KEY (symbol, day, bin_size, price_bin)
);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    symbol TEXT NOT NULL,
    day TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    rows_processed INTEGER NOT NULL DEFAULT 0,
    message TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, day, source)
);

CREATE TABLE IF NOT EXISTS strategy_setups (
    symbol TEXT NOT NULL,
    level_day TEXT NOT NULL,
    variant TEXT NOT NULL,
    bin_size REAL NOT NULL,
    poc REAL NOT NULL,
    level_status TEXT NOT NULL,
    touched_at TEXT,
    setup_state TEXT NOT NULL,
    direction TEXT,
    confirmation_time TEXT,
    retest_bars_used INTEGER NOT NULL DEFAULT 0,
    retest_bars_left INTEGER NOT NULL DEFAULT 6,
    entry_time TEXT,
    entry REAL,
    stop REAL,
    target_poc REAL,
    target_day TEXT,
    target_distance_pct REAL,
    target_r REAL,
    expiry_reason TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, level_day, variant, bin_size)
);

CREATE INDEX IF NOT EXISTS idx_strategy_setups_symbol_day
ON strategy_setups(symbol, level_day);

CREATE TABLE IF NOT EXISTS funding_rates (
    symbol TEXT NOT NULL,
    funding_time TEXT NOT NULL,
    funding_rate REAL NOT NULL,
    mark_price REAL,
    PRIMARY KEY (symbol, funding_time)
);

CREATE TABLE IF NOT EXISTS open_interest_4h (
    symbol TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    open_interest REAL NOT NULL,
    open_interest_value REAL NOT NULL,
    PRIMARY KEY (symbol, timestamp)
);
"""


def _path(path: str | Path | None = None) -> Path:
    result = Path(path) if path is not None else DEFAULT_DATABASE_PATH
    result.parent.mkdir(parents=True, exist_ok=True)
    return result


@contextmanager
def connect(path: str | Path | None = None):
    connection = sqlite3.connect(_path(path), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def initialize(path: str | Path | None = None) -> None:
    with connect(path) as connection:
        connection.executescript(SCHEMA)


def upsert_exact_profile(
    symbol: str,
    day: str,
    bin_size: float,
    poc: float,
    vah: float,
    val: float,
    bins: dict[float, float],
    trade_count: int,
    source_sha256: str | None,
    path: str | Path | None = None,
) -> None:
    initialize(path)
    with connect(path) as connection:
        connection.execute(
            """
            INSERT INTO daily_levels
                (symbol, day, poc, vah, val, bin_size, method, trade_count, source_sha256)
            VALUES (?, ?, ?, ?, ?, ?, 'aggTrades', ?, ?)
            ON CONFLICT(symbol, day, bin_size, method) DO UPDATE SET
                poc=excluded.poc, vah=excluded.vah, val=excluded.val,
                trade_count=excluded.trade_count,
                source_sha256=excluded.source_sha256,
                created_at=CURRENT_TIMESTAMP
            """,
            (symbol, day, poc, vah, val, bin_size, trade_count, source_sha256),
        )
        connection.execute(
            "DELETE FROM profile_bins WHERE symbol=? AND day=? AND bin_size=?",
            (symbol, day, bin_size),
        )
        connection.executemany(
            """
            INSERT INTO profile_bins(symbol, day, bin_size, price_bin, volume)
            VALUES (?, ?, ?, ?, ?)
            """,
            [(symbol, day, bin_size, float(price), float(volume)) for price, volume in bins.items()],
        )


def record_ingestion(
    symbol: str,
    day: str,
    source: str,
    status: str,
    rows_processed: int = 0,
    message: str | None = None,
    path: str | Path | None = None,
) -> None:
    initialize(path)
    with connect(path) as connection:
        connection.execute(
            """
            INSERT INTO ingestion_runs(symbol, day, source, status, rows_processed, message)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, day, source) DO UPDATE SET
                status=excluded.status,
                rows_processed=excluded.rows_processed,
                message=excluded.message,
                updated_at=CURRENT_TIMESTAMP
            """,
            (symbol, day, source, status, rows_processed, message),
        )


def mark_touched(
    symbol: str,
    day: str,
    bin_size: float,
    touched_at: pd.Timestamp,
    method: str = "aggTrades",
    path: str | Path | None = None,
) -> None:
    """Persist only the first observed touch of a level."""
    initialize(path)
    timestamp = pd.Timestamp(touched_at)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    with connect(path) as connection:
        connection.execute(
            """
            UPDATE daily_levels
            SET touched_at=?
            WHERE symbol=? AND day=? AND bin_size=? AND method=? AND touched_at IS NULL
            """,
            (timestamp.isoformat(), symbol.upper(), day, bin_size, method),
        )


def load_levels(
    symbol: str,
    bin_size: float = 10.0,
    method: str = "aggTrades",
    path: str | Path | None = None,
) -> pd.DataFrame:
    database = _path(path)
    if not database.exists():
        return pd.DataFrame(columns=["day", "poc", "vah", "val", "method", "touched_at"])
    initialize(database)
    with connect(database) as connection:
        rows = connection.execute(
            """
            SELECT day, poc, vah, val, bin_size, method, trade_count, touched_at
            FROM daily_levels
            WHERE symbol=? AND bin_size=? AND method=?
            ORDER BY day
            """,
            (symbol.upper(), bin_size, method),
        ).fetchall()
    result = pd.DataFrame([dict(row) for row in rows])
    if result.empty:
        return result
    result["day"] = pd.to_datetime(result["day"], utc=True)
    result["touched_at"] = pd.to_datetime(result["touched_at"], utc=True, errors="coerce")
    return result


def _db_value(value):
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    return value


def save_strategy_setups(
    symbol: str,
    setups: pd.DataFrame,
    bin_size: float = 10.0,
    variant: str = "P0",
    path: str | Path | None = None,
) -> None:
    if setups.empty:
        return
    initialize(path)
    columns = [
        "level_day", "poc", "level_status", "touched_at", "setup_state",
        "direction", "confirmation_time", "retest_bars_used", "retest_bars_left",
        "entry_time", "entry", "stop", "target_poc", "target_day",
        "target_distance_pct", "target_r", "expiry_reason",
    ]
    sql = """
        INSERT INTO strategy_setups (
            symbol, level_day, variant, bin_size, poc, level_status, touched_at,
            setup_state, direction, confirmation_time, retest_bars_used,
            retest_bars_left, entry_time, entry, stop, target_poc, target_day,
            target_distance_pct, target_r, expiry_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, level_day, variant, bin_size) DO UPDATE SET
            poc=excluded.poc, level_status=excluded.level_status,
            touched_at=excluded.touched_at, setup_state=excluded.setup_state,
            direction=excluded.direction, confirmation_time=excluded.confirmation_time,
            retest_bars_used=excluded.retest_bars_used,
            retest_bars_left=excluded.retest_bars_left,
            entry_time=excluded.entry_time, entry=excluded.entry, stop=excluded.stop,
            target_poc=excluded.target_poc, target_day=excluded.target_day,
            target_distance_pct=excluded.target_distance_pct,
            target_r=excluded.target_r, expiry_reason=excluded.expiry_reason,
            updated_at=CURRENT_TIMESTAMP
    """
    values = []
    for row in setups[columns].itertuples(index=False, name=None):
        values.append((symbol.upper(), _db_value(row[0]), variant, bin_size, *[_db_value(value) for value in row[1:]]))
    with connect(path) as connection:
        connection.executemany(sql, values)


def load_strategy_setups(
    symbol: str,
    bin_size: float = 10.0,
    variant: str = "P0",
    path: str | Path | None = None,
) -> pd.DataFrame:
    database = _path(path)
    if not database.exists():
        return pd.DataFrame()
    initialize(database)
    with connect(database) as connection:
        rows = connection.execute(
            """
            SELECT level_day, poc, level_status, touched_at, setup_state,
                   direction, confirmation_time, retest_bars_used,
                   retest_bars_left, entry_time, entry, stop, target_poc,
                   target_day, target_distance_pct, target_r, expiry_reason,
                   updated_at
            FROM strategy_setups
            WHERE symbol=? AND bin_size=? AND variant=?
            ORDER BY level_day
            """,
            (symbol.upper(), bin_size, variant),
        ).fetchall()
    result = pd.DataFrame([dict(row) for row in rows])
    if result.empty:
        return result
    for column in ("level_day", "touched_at", "confirmation_time", "entry_time", "target_day", "updated_at"):
        result[column] = pd.to_datetime(result[column], utc=True, errors="coerce")
    return result


def save_market_context(
    symbol: str,
    funding: pd.DataFrame,
    open_interest: pd.DataFrame,
    path: str | Path | None = None,
) -> None:
    initialize(path)
    with connect(path) as connection:
        if not funding.empty:
            connection.executemany(
                """
                INSERT INTO funding_rates(symbol, funding_time, funding_rate, mark_price)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(symbol, funding_time) DO UPDATE SET
                    funding_rate=excluded.funding_rate,
                    mark_price=excluded.mark_price
                """,
                [
                    (
                        symbol.upper(),
                        pd.Timestamp(row.funding_time).isoformat(),
                        float(row.funding_rate),
                        None if pd.isna(row.mark_price) else float(row.mark_price),
                    )
                    for row in funding.itertuples(index=False)
                ],
            )
        if not open_interest.empty:
            connection.executemany(
                """
                INSERT INTO open_interest_4h(symbol, timestamp, open_interest, open_interest_value)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(symbol, timestamp) DO UPDATE SET
                    open_interest=excluded.open_interest,
                    open_interest_value=excluded.open_interest_value
                """,
                [
                    (
                        symbol.upper(),
                        pd.Timestamp(row.timestamp).isoformat(),
                        float(row.open_interest),
                        float(row.open_interest_value),
                    )
                    for row in open_interest.itertuples(index=False)
                ],
            )
