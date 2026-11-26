"""Event risk for swing trades: results and ex-dividend dates inside your holding window (best effort via Yahoo's calendar,
cached 12 hours) and the NSE F&O ban list. Yahoo's calendar is often incomplete for Indian stocks, so always confirm
results dates on the company's announcement or NSE board-meeting pages. ASM/GSM lists are not covered. No Streamlit."""
import arrow_guard  # noqa: F401  (must run before pandas)
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

import pandas as pd
import yfinance as yf

import datastore

MAX_AGE = 12 * 3600
BAN_URL = "https://nsearchives.nseindia.com/content/fo/fo_secban.csv"
_ban = {"at": 0.0, "symbols": set(), "ok": False}


def _d(x):
    try:
        return pd.Timestamp(x).tz_localize(None).date() if pd.Timestamp(x).tzinfo else pd.Timestamp(x).date()
    except Exception:
        return None


def parse_calendar(cal):
    """[(kind, date)] from yfinance's calendar, whichever shape this yfinance version returns (dict or DataFrame)."""
    out = []
    if isinstance(cal, dict):
        e = cal.get("Earnings Date")
        for x in (e if isinstance(e, (list, tuple)) else [e] if e is not None else []):
            out.append(("Results", _d(x)))
        if cal.get("Ex-Dividend Date") is not None:
            out.append(("Ex-dividend", _d(cal["Ex-Dividend Date"])))
    elif isinstance(cal, pd.DataFrame) and not cal.empty:
        for label, kind in (("Earnings Date", "Results"), ("Ex-Dividend Date", "Ex-dividend")):
            if label in cal.index:
                out += [(kind, _d(v)) for v in cal.loc[label].dropna().tolist()]
    return [(k, d) for k, d in out if d]


def fetch_one(sym):
    return parse_calendar(yf.Ticker(sym + ".NS").calendar)


def _table(con):
    con.execute("CREATE TABLE IF NOT EXISTS events (symbol TEXT, kind TEXT, date TEXT, fetched REAL)")


def upcoming(symbols, today=None, days=14, fetch=fetch_one, max_age=MAX_AGE, now=None):
    """{symbol: [(kind, date, days_away)]} for events from today to today+days. Failed lookups are not cached."""
    today, now = today or date.today(), time.time() if now is None else now
    con = datastore.connect()
    try:
        _table(con)
        fresh = {r[0] for r in con.execute("SELECT DISTINCT symbol FROM events WHERE kind='checked' AND fetched>=?", (now - max_age,))}
        need = [s for s in dict.fromkeys(symbols) if s not in fresh]

        def one(s):
            try:
                return s, fetch(s)
            except Exception:
                return s, None
        with ThreadPoolExecutor(4) as ex:
            for s, evs in ex.map(one, need):
                if evs is None:
                    continue
                con.execute("DELETE FROM events WHERE symbol=?", (s,))
                con.executemany("INSERT INTO events VALUES (?,?,?,?)",
                                [(s, k, d.isoformat(), now) for k, d in evs] + [(s, "checked", "", now)])
        con.commit()
        out = {}
        for s in symbols:
            rows = con.execute("SELECT kind, date FROM events WHERE symbol=? AND kind<>'checked'", (s,)).fetchall()
            hits = sorted((k, date.fromisoformat(d), (date.fromisoformat(d) - today).days) for k, d in rows)
            hits = [h for h in hits if 0 <= h[2] <= days]
            if hits:
                out[s] = hits
        return out
    finally:
        con.close()


def flag_text(hits, within=None):
    """'Results in 3d; Ex-div in 6d', optionally only events inside `within` days."""
    return "; ".join(f"{'Ex-div' if k == 'Ex-dividend' else k} in {n}d" if n else f"{'Ex-div' if k == 'Ex-dividend' else k} TODAY"
                     for k, _, n in hits if within is None or n <= within)


def fo_ban(max_age=6 * 3600):
    """(set of symbols in the F&O ban today, ok flag). Best effort: the file location or format may change."""
    if time.time() - _ban["at"] < max_age:
        return _ban["symbols"], _ban["ok"]
    try:
        req = urllib.request.Request(BAN_URL, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.nseindia.com/"})
        with urllib.request.urlopen(req, timeout=8) as r:
            text = r.read().decode("utf-8", "ignore")
        syms = {ln.split(",")[1].strip().upper() for ln in text.splitlines() if "," in ln and ln.split(",")[0].strip().isdigit()}
        _ban.update(at=time.time(), symbols=syms, ok=True)
    except Exception:
        _ban.update(at=time.time(), symbols=set(), ok=False)
    return _ban["symbols"], _ban["ok"]
