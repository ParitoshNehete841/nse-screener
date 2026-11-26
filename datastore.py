"""Local price store (SQLite). Downloads only the days that are missing, keeps working when Yahoo is down, re-downloads a
stock's whole history when a split/dividend has re-based it, and flags bad data. No Streamlit, shared by app and bot."""
import arrow_guard  # noqa: F401  (must run before pandas)
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

DB = Path(os.environ.get("NSE_SCREENER_DB") or Path(__file__).resolve().parent / "data" / "prices.db")
TTL = 120            # seconds: do not hit the network again for a stock synced this recently
OVERLAP_DAYS = 5     # re-fetch this many days before the last stored bar
REBASE_TOL = 0.005   # >0.5% difference on already-stored days => prices were re-based (split/dividend): reload history
INDEX = "^NSEI"


def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=30)
    con.execute("CREATE TABLE IF NOT EXISTS bars (symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, "
                "volume REAL, PRIMARY KEY (symbol, date))")
    con.execute("CREATE TABLE IF NOT EXISTS meta (symbol TEXT PRIMARY KEY, last_fetch REAL)")
    return con


def _clean(d):
    """Normalised OHLCV frame: naive dates, no duplicates/NaN/non-positive prices, High >= Low."""
    d = d[["Open", "High", "Low", "Close", "Volume"]].copy()
    d.index = pd.DatetimeIndex(d.index).tz_localize(None).normalize()
    d = d[~d.index.duplicated(keep="last")].dropna(subset=["Close"])
    for c in ("Open", "High", "Low"):
        d[c] = d[c].fillna(d["Close"])
    d["Volume"] = d["Volume"].fillna(0)
    return d[(d["Close"] > 0) & (d["Open"] > 0) & (d["High"] >= d["Low"])]


def _upsert(con, sym, d):
    rows = [(sym, ix.strftime("%Y-%m-%d"), float(r.Open), float(r.High), float(r.Low), float(r.Close), float(r.Volume))
            for ix, r in d.iterrows()]
    con.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?)", rows)


def _stored_close(con, sym, lo, hi):
    q = "SELECT date, close FROM bars WHERE symbol=? AND date>=? AND date<? ORDER BY date"
    s = pd.read_sql_query(q, con, params=(sym, lo, hi), parse_dates=["date"], index_col="date")
    return s["close"]


def sync(tickers, start, fetch, chunk=25, pause=1.0, now=None):
    """Bring the store up to date for tickers (Yahoo style, e.g. 'TCS.NS'). fetch(part, since) -> {ticker: DataFrame}.
    Returns a small log dict. Never raises for network problems: whatever is stored stays usable."""
    now = time.time() if now is None else now
    con = connect()
    try:
        start_ts = pd.Timestamp(start)
        rng = {r[0]: (r[1], r[2]) for r in con.execute("SELECT symbol, MIN(date), MAX(date) FROM bars GROUP BY symbol")}
        synced = dict(con.execute("SELECT symbol, last_fetch FROM meta").fetchall())
        full = [t for t in tickers if t not in rng or pd.Timestamp(rng[t][0]) > start_ts + pd.Timedelta(days=10)]
        inc = [t for t in tickers if t not in full and now - synced.get(t, 0) >= TTL]
        log = {"full": len(full), "incremental": len(inc), "rebased": [], "fetched": 0}
        touched = set()

        def batches(group, since):
            for i in range(0, len(group), chunk):
                part = group[i:i + chunk]
                part = part if INDEX in part else part + [INDEX]
                got = fetch(part, since)
                yield {t: _clean(d) for t, d in got.items() if len(d)}
                if i + chunk < len(group):
                    time.sleep(pause)

        for got in batches(full, start):
            for t, d in got.items():
                _upsert(con, t, d)
                touched.add(t)
        if inc:
            since = (min(pd.Timestamp(rng[t][1]) for t in inc) - pd.Timedelta(days=OVERLAP_DAYS)).strftime("%Y-%m-%d")
            for got in batches(inc, since):
                for t, d in got.items():
                    if t in rng and t not in full:
                        last = rng[t][1]
                        old = _stored_close(con, t, d.index.min().strftime("%Y-%m-%d"), last)  # days before the last stored bar
                        both = pd.concat([old, d["Close"]], axis=1, join="inner").dropna()
                        if len(both) and ((both.iloc[:, 1] / both.iloc[:, 0] - 1).abs().max() > REBASE_TOL):
                            if t != INDEX:
                                log["rebased"].append(t)
                                continue
                    _upsert(con, t, d)
                    touched.add(t)
        for t in log["rebased"]:  # re-based history: replace it entirely
            for got in batches([t], start):
                if t in got:
                    con.execute("DELETE FROM bars WHERE symbol=?", (t,))
                    _upsert(con, t, got[t])
                    touched.add(t)
        con.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", [(t, now) for t in touched])
        log["fetched"] = len(touched)
        con.commit()
        return log
    finally:
        con.close()


def read(tickers, start, min_rows=120):
    """{ticker: OHLCV DataFrame} from the store for dates >= start (only tickers with at least min_rows bars)."""
    con = connect()
    try:
        out, lo = {}, pd.Timestamp(start).strftime("%Y-%m-%d")
        for t in tickers:
            df = pd.read_sql_query("SELECT date, open AS Open, high AS High, low AS Low, close AS Close, volume AS Volume "
                                   "FROM bars WHERE symbol=? AND date>=? ORDER BY date", con, params=(t, lo),
                                   parse_dates=["date"], index_col="date")
            df.index.name = None
            if len(df) >= min_rows:
                out[t] = df
        return out
    finally:
        con.close()


def last_sync(ticker=INDEX):
    con = connect()
    try:
        r = con.execute("SELECT last_fetch FROM meta WHERE symbol=?", (ticker,)).fetchone()
        return datetime.fromtimestamp(r[0]) if r else None
    finally:
        con.close()


def health(tickers):
    """One row per stored ticker with data-quality flags (stale, short history, zero-volume days, extreme moves)."""
    con = connect()
    try:
        rows, last = [], {}
        for t in tickers:
            d = pd.read_sql_query("SELECT date, close, volume FROM bars WHERE symbol=? ORDER BY date DESC LIMIT 260", con,
                                  params=(t,), parse_dates=["date"], index_col="date").sort_index()
            if len(d):
                last[t] = d.index[-1]
                n_all = con.execute("SELECT COUNT(*) FROM bars WHERE symbol=?", (t,)).fetchone()[0]
                move = d["close"].pct_change().abs().max() * 100 if len(d) > 1 else 0.0
                rows.append(dict(Symbol=t.removesuffix(".NS"), Bars=n_all, Last=d.index[-1].date(), MaxMove=move,
                                 ZeroVol=int((d["volume"] <= 0).sum())))
        if not rows:
            return pd.DataFrame(columns=["Symbol", "Bars", "Last", "Stale", "MaxMove", "ZeroVol", "Flags"])
        ref = last.get(INDEX) or max(last.values())
        df = pd.DataFrame(rows)
        df["Stale"] = [(ref - pd.Timestamp(x)).days for x in df["Last"]]
        df["Flags"] = df.apply(lambda r: "; ".join(f for f, bad in [("stale", r["Stale"] >= 3), ("short history", r["Bars"] < 250),
                                                                     ("zero-volume days", r["ZeroVol"] >= 5),
                                                                     ("extreme move (split / bad tick?)", r["MaxMove"] > 25)] if bad), axis=1)
        return df
    finally:
        con.close()
