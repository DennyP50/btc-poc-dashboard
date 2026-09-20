#!/usr/bin/env python3
"""Daily POC / VAH / VAL pro BTCUSDT perp z Binance Data Vision (1m klines).

Aproximace: objem každé 1m svíčky se rozdělí rovnoměrně mezi high a low.
Přesný profil by vyžadoval aggTrades.

Použití:
  pip install pandas numpy requests plotly
  python btc_daily_poc.py --start 2026-06-01 --end 2026-09-18 --bin 25 --tf 4h
Výstup: <SYMBOL>_daily_poc.csv a <SYMBOL>_daily_poc.html
Den = UTC 00:00-23:59.
"""
import argparse
import datetime as dt
import io
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests

BASE = "https://data.binance.vision/data/futures/um"
COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time",
        "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume", "ignore"]
CACHE = Path("dv_cache")


def fetch_zip(url):
    CACHE.mkdir(exist_ok=True)
    f = CACHE / url.rsplit("/", 1)[1]
    if not f.exists():
        r = requests.get(url, timeout=60)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        f.write_bytes(r.content)
    with zipfile.ZipFile(f) as z:
        raw = z.read(z.namelist()[0])
    df = pd.read_csv(io.BytesIO(raw), header=None, names=COLS)
    return df[pd.to_numeric(df["open_time"], errors="coerce").notna()]  # zahodí hlavičku


def load_klines(sym, start, end):
    parts, m = [], dt.date(start.year, start.month, 1)
    while m <= end:
        nm = (m.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        df = fetch_zip(f"{BASE}/monthly/klines/{sym}/1m/{sym}-1m-{m:%Y-%m}.zip")
        if df is None:  # měsíc ještě není v monthly -> denní soubory
            d = max(m, start)
            while d < nm and d <= end:
                x = fetch_zip(f"{BASE}/daily/klines/{sym}/1m/{sym}-1m-{d:%Y-%m-%d}.zip")
                if x is not None:
                    parts.append(x)
                d += dt.timedelta(days=1)
        else:
            parts.append(df)
        m = nm
    df = pd.concat(parts)
    df[["open", "high", "low", "close", "volume"]] = df[["open", "high", "low", "close", "volume"]].astype(float)
    t = df["open_time"].astype("int64").to_numpy()
    t = np.where(t > 1e14, t // 1000, t)  # případné µs -> ms
    df["ts"] = pd.to_datetime(t, unit="ms", utc=True)
    df = df.drop_duplicates("ts").sort_values("ts")
    return df[(df.ts.dt.date >= start) & (df.ts.dt.date <= end)].reset_index(drop=True)


def day_profile(g, bin_size, va=0.70):
    lo = np.floor(g.low.min() / bin_size) * bin_size
    hi = np.ceil(g.high.max() / bin_size) * bin_size
    edges = np.arange(lo, hi + bin_size, bin_size)
    vol = np.zeros(max(len(edges) - 1, 1))
    for l, h, v in zip(g.low.to_numpy(), g.high.to_numpy(), g.volume.to_numpy()):
        i0 = min(int((l - lo) // bin_size), len(vol) - 1)
        i1 = min(int((h - lo) // bin_size), len(vol) - 1)
        vol[i0:i1 + 1] += v / (i1 - i0 + 1)
    poc = int(vol.argmax())
    a = b = poc
    acc, tot = vol[poc], vol.sum()
    while acc < va * tot:  # standardní rozšiřování VA od POC
        up = vol[b + 1] if b + 1 < len(vol) else -1
        dn = vol[a - 1] if a > 0 else -1
        if up >= dn:
            b += 1; acc += up
        else:
            a -= 1; acc += dn
    return edges[poc] + bin_size / 2, edges[b + 1], edges[a]


def build(k, bin_size):
    rows = []
    for day, g in k.groupby(k.ts.dt.date):
        poc, vah, val = day_profile(g, bin_size)
        rows.append(dict(day=day, poc=poc, vah=vah, val=val, minutes=len(g)))
    p = pd.DataFrame(rows)
    daily = k.groupby(k.ts.dt.date).agg(high=("high", "max"), low=("low", "min"))
    touched = []
    for i, r in p.iterrows():  # první pozdější den, kdy cena POC protnula
        later = daily.iloc[i + 1:]
        hit = later[(later.low <= r.poc) & (later.high >= r.poc)]
        touched.append(hit.index[0] if len(hit) else None)
    p["touched"] = touched
    return p


def plot(k, p, sym, tf, out):
    c = (k.set_index("ts").resample(tf)
         .agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna())
    end = c.index[-1] + pd.Timedelta(tf)
    fig = go.Figure(go.Candlestick(x=c.index, open=c.open, high=c.high, low=c.low,
                                   close=c.close, name=sym))

    def lines(col, naked_only=False):
        xs, ys = [], []
        for r in p.itertuples():
            x0 = pd.Timestamp(r.day, tz="UTC") + pd.Timedelta(days=1)
            if x0 >= end:
                continue
            if naked_only:
                if r.touched is not None and not pd.isna(r.touched):
                    continue
                x1 = end
            else:
                x1 = min(x0 + pd.Timedelta(days=1), end)
            y = getattr(r, col)
            xs += [x0, x1, None]; ys += [y, y, None]
        return xs, ys

    for col, name, style in [("poc", "POC předchozího dne", dict(color="orange", width=2)),
                             ("vah", "VAH předchozího dne", dict(color="gray", dash="dot")),
                             ("val", "VAL předchozího dne", dict(color="gray", dash="dot"))]:
        x, y = lines(col)
        fig.add_trace(go.Scatter(x=x, y=y, mode="lines", line=style, name=name))
    x, y = lines("poc", naked_only=True)
    fig.add_trace(go.Scatter(x=x, y=y, mode="lines", name="Naked POC",
                             line=dict(color="magenta", width=1, dash="dash")))
    fig.update_layout(title=f"{sym} perp – daily POC/VA (UTC dny)", xaxis_rangeslider_visible=False,
                      template="plotly_dark", height=800)
    fig.write_html(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--bin", type=float, default=25.0, help="velikost cenového koše v USD")
    ap.add_argument("--tf", default="4h", help="timeframe svíček v grafu (1h, 4h, 1D...)")
    a = ap.parse_args()
    k = load_klines(a.symbol, dt.date.fromisoformat(a.start), dt.date.fromisoformat(a.end))
    p = build(k, a.bin)
    bad = p[p.minutes != 1440]
    if len(bad):
        print("Dny s neúplnými minutami:\n", bad[["day", "minutes"]].to_string(index=False))
    p.to_csv(f"{a.symbol}_daily_poc.csv", index=False)
    plot(k, p, a.symbol, a.tf, f"{a.symbol}_daily_poc.html")
    print(f"Hotovo: {len(p)} dní -> {a.symbol}_daily_poc.csv / .html")


if __name__ == "__main__":
    main()
