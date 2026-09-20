import zipfile

import pandas as pd

from exact_poc import profile_from_archive
from poc_store import connect, load_levels, load_strategy_setups, save_market_context, save_strategy_setups, upsert_exact_profile


def test_aggtrades_profile_uses_actual_trade_prices(tmp_path):
    archive = tmp_path / "trades.zip"
    csv = "1,100.0,1.0,1,1,1,true\n2,101.0,2.0,2,2,2,false\n3,115.0,1.0,3,3,3,true\n"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("trades.csv", csv)
    poc, vah, val, bins, count, touches = profile_from_archive(archive, bin_size=10.0)
    assert count == 3
    assert bins == {100.0: 3.0, 110.0: 1.0}
    assert poc == 105.0
    assert val == 100.0
    assert vah == 110.0
    assert touches == {}


def test_archive_records_first_crossing_time_for_prior_level(tmp_path):
    archive = tmp_path / "touches.zip"
    csv = "1,99.0,1.0,1,1,1767225600000,true\n2,101.0,1.0,2,2,1767225660000,false\n"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("trades.csv", csv)
    *_, touches = profile_from_archive(archive, 10.0, {"2025-12-31": 100.0})
    assert touches["2025-12-31"] == pd.Timestamp("2026-01-01 00:01:00", tz="UTC")


def test_store_keeps_exact_level_history(tmp_path):
    database = tmp_path / "poc.sqlite3"
    upsert_exact_profile("BTCUSDT", "2026-01-01", 10, 105, 110, 100, {100: 3}, 3, "abc", database)
    upsert_exact_profile("BTCUSDT", "2026-01-02", 10, 115, 120, 110, {110: 4}, 4, "def", database)
    levels = load_levels("BTCUSDT", 10, path=database)
    assert list(levels["poc"]) == [105.0, 115.0]
    assert list(levels["method"].unique()) == ["aggTrades"]
    assert all(isinstance(day, pd.Timestamp) for day in levels["day"])


def test_store_round_trips_strategy_snapshots(tmp_path):
    database = tmp_path / "poc.sqlite3"
    setups = pd.DataFrame(
        [
            {
                "level_day": pd.Timestamp("2026-01-01", tz="UTC"),
                "poc": 100.0,
                "level_status": "TOUCHED",
                "touched_at": pd.Timestamp("2026-01-02 04:00", tz="UTC"),
                "setup_state": "ENTRY_READY",
                "direction": "SHORT",
                "confirmation_time": pd.Timestamp("2026-01-02 04:00", tz="UTC"),
                "retest_bars_used": 2,
                "retest_bars_left": 4,
                "entry_time": pd.Timestamp("2026-01-02 12:00", tz="UTC"),
                "entry": 99.0,
                "stop": 101.0,
                "target_poc": 90.0,
                "target_day": pd.Timestamp("2025-12-30", tz="UTC"),
                "target_distance_pct": 9.09,
                "target_r": 4.5,
                "expiry_reason": None,
            }
        ]
    )
    save_strategy_setups("BTCUSDT", setups, path=database)
    loaded = load_strategy_setups("BTCUSDT", path=database)
    assert loaded.iloc[0]["setup_state"] == "ENTRY_READY"
    assert loaded.iloc[0]["target_poc"] == 90.0
    assert loaded.iloc[0]["entry_time"] == pd.Timestamp("2026-01-02 12:00", tz="UTC")


def test_store_persists_context_without_touching_strategy(tmp_path):
    database = tmp_path / "poc.sqlite3"
    funding = pd.DataFrame(
        [{"funding_time": pd.Timestamp("2026-01-01", tz="UTC"), "funding_rate": 0.0001, "mark_price": 100.0}]
    )
    oi = pd.DataFrame(
        [{"timestamp": pd.Timestamp("2026-01-01", tz="UTC"), "open_interest": 123.0, "open_interest_value": 456.0}]
    )
    save_market_context("BTCUSDT", funding, oi, database)
    with connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM funding_rates").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM open_interest_4h").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM strategy_setups").fetchone()[0] == 0
