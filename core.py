"""Screening logic with no Streamlit dependency (shared by the web app and the alert bot)."""
import arrow_guard  # noqa: F401  (must run before pandas)
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf

import datastore
from strategies import STRATS, feats

IST = ZoneInfo("Asia/Kolkata")

# Starter lists - constituents change, so edit or use the Custom box.
NIFTY50 = ("ADANIENT ADANIPORTS APOLLOHOSP ASIANPAINT AXISBANK BAJAJ-AUTO BAJFINANCE BAJAJFINSV BEL BHARTIARTL "
           "CIPLA COALINDIA DRREDDY EICHERMOT ETERNAL GRASIM HCLTECH HDFCBANK HDFCLIFE HEROMOTOCO HINDALCO "
           "HINDUNILVR ICICIBANK INDUSINDBK INFY ITC JIOFIN JSWSTEEL KOTAKBANK LT M&M MARUTI NESTLEIND NTPC ONGC "
           "POWERGRID RELIANCE SBILIFE SBIN SHRIRAMFIN SUNPHARMA TATACONSUMER TATASTEEL TCS TECHM TITAN TRENT "
           "ULTRACEMCO WIPRO").split()
MIDCAPS = ("HAL BHEL PFC RECLTD IRFC DLF POLYCAB DIXON PERSISTENT COFORGE TVSMOTOR MUTHOOTFIN CHOLAFIN LUPIN "
           "AUROPHARMA JINDALSTEL VEDL SAIL NMDC BANKBARODA PNB CANBK FEDERALBNK IDFCFIRSTB AUBANK IRCTC INDHOTEL "
           "LODHA GODREJPROP PIDILITIND SIEMENS ABB CGPOWER BSE MAZDOCK COCHINSHIP RVNL NHPC TATAPOWER BHARATFORG "
           "ASHOKLEY HAVELLS VOLTAS CUMMINSIND TATAELXSI").split()

# Filter presets: trend, c20 (close vs prior 20D high %), h52 (max % below 52W high), e20 (close vs EMA20 %),
# rsi, bbw (max BB-width percentile, lower = tighter squeeze), rvol (min), rs (min 3M RS vs Nifty), turn (min Cr/day)
BASE = dict(trend=True, c20=(-15.0, 15.0), h52=50, e20=(-15.0, 15.0), rsi=(0, 100), bbw=100, rvol=0.0, rs=-50.0, turn=10.0)
PRESETS = {
    "Pre-breakout": {**BASE, "c20": (-3.0, 0.0), "h52": 15, "rsi": (50, 70), "bbw": 40},
    "Breakout + volume": {**BASE, "c20": (0.0, 8.0), "h52": 10, "rsi": (55, 80), "rvol": 1.5},
    "Pullback to EMA20": {**BASE, "e20": (-2.5, 1.0), "rsi": (40, 55), "h52": 25},
    "Momentum leaders": {**BASE, "h52": 5, "rsi": (60, 85), "rs": 5.0},
    "Custom (wide open)": {**BASE, "trend": False},
}


def download(symbols, chunk=25, pause=1.0, start=None):
    """Daily bars for NSE symbols + Nifty (^NSEI) from the local price store (datastore.py), which downloads only what is
    missing. Default window is the last 2 years; pass start="YYYY-MM-DD" for longer history. During market hours the
    last bar is today's partial candle (~15 min delayed). If Yahoo fails, stored data is served and the returned
    timestamp is the real age of the data. Raises RuntimeError only if there is no usable data at all."""
    start = start or (datetime.now() - timedelta(days=730)).strftime("%Y-%m-%d")
    tickers, errors = [s + ".NS" for s in symbols] + [datastore.INDEX], []

    def fetch(part, since):
        for _attempt in range(2):
            try:
                raw = yf.download(part, start=since, interval="1d", group_by="ticker", auto_adjust=True, threads=True,
                                  progress=False)
                break
            except Exception as e:  # rate limit, network, parsing...
                errors.append(f"{type(e).__name__}: {e}")
                time.sleep(pause * 3)
        else:
            return {}
        out = {}
        for t in part:
            try:
                d = raw[t].dropna(subset=["Close"])
            except KeyError:
                continue
            if len(d):
                out[t] = d
        return out

    datastore.sync(tickers, start, fetch, chunk=chunk, pause=pause)
    out = datastore.read(tickers, start, min_rows=120)
    if datastore.INDEX not in out or len(out) < 2:
        raise RuntimeError("No usable price data" + (f" ({errors[-1]})" if errors else "")
                           + ". Yahoo Finance may be rate limiting you or your network blocks it, and nothing is stored yet.")
    synced = datastore.last_sync()
    return out, (synced.astimezone(IST) if synced else datetime.now(IST))


def session_frac():
    """Fraction of NSE session elapsed (as seen through the 15-min delay); None if market closed."""
    n = datetime.now(IST)
    m = n.hour * 60 + n.minute - 555 - 15
    return max(m / 375, 0.1) if n.weekday() < 5 and 0 < m < 375 else None


def add_indicators(d):
    c, h, l = d["Close"], d["High"], d["Low"]
    pc = c.shift()
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    dl = c.diff()
    up = dl.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    dn = (-dl.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    d = d.copy()
    d["ATR"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    d["RSI"] = 100 - 100 / (1 + up / dn)
    for n in (20, 50, 200):
        d[f"E{n}"] = c.ewm(span=n, adjust=False).mean()
    d["BBW"] = 4 * c.rolling(20).std() / c.rolling(20).mean()
    return d


def analyze(sym, d, idx63, frac):
    d = add_indicators(d)
    r = d.iloc[-1]
    c, h, v = d["Close"], d["High"], d["Volume"]
    p20h, h52 = h.iloc[-21:-1].max(), h.iloc[-252:].max()
    avgv = v.iloc[-21:-1].mean()
    rvol = v.iloc[-1] / (avgv * (frac or 1)) if avgv else np.nan
    bbw_pct = (d["BBW"].iloc[-120:] <= r["BBW"]).mean() * 100
    rs = (r["Close"] / c.iloc[-64] - 1) * 100 - idx63
    from20, from52 = (r["Close"] / p20h - 1) * 100, (r["Close"] / h52 - 1) * 100
    trend = r["Close"] > r["E50"] > r["E200"]
    score = (
        (int(r["Close"] > r["E20"]) + int(r["Close"] > r["E50"]) + int(r["E50"] > r["E200"])) / 3 * 20
        + np.clip(1 - abs(from20) / 5, 0, 1) * 20
        + (1 - bbw_pct / 100) * 15
        + np.clip(rvol / 2, 0, 1) * 15 * (0 if np.isnan(rvol) else 1)
        + np.clip(1 - abs(r["RSI"] - 60) / 25, 0, 1) * 15
        + np.clip(rs / 10, 0, 1) * 15
    )
    return dict(
        Symbol=sym, Price=r["Close"], ChgPct=(r["Close"] / c.iloc[-2] - 1) * 100, RVOL=rvol, RSI=r["RSI"],
        vs20DH=from20, vs52WH=from52, vsEMA20=(r["Close"] / r["E20"] - 1) * 100, Squeeze=bbw_pct, RS3M=rs,
        ATRpct=r["ATR"] / r["Close"] * 100, TurnCr=avgv * r["Close"] / 1e7, Score=score, Trend=trend,
        Chart=f"https://in.tradingview.com/chart/?symbol=NSE:{sym.replace('&', '_').replace('-', '_')}",
    )


def build_df(data, project=True):
    """Indicator table for every downloaded stock. Returns (df, nifty_3m_return_pct)."""
    nd = data["^NSEI"]["Close"]
    idx63 = (nd.iloc[-1] / nd.iloc[-64] - 1) * 100
    frac = session_frac() if project else None
    rows = [analyze(t[:-3], d, idx63, frac) for t, d in data.items() if t != "^NSEI"]
    return pd.DataFrame(rows), idx63


def apply_filters(df, F):
    """Rows matching filter dict F (same keys as the PRESETS entries), best score first."""
    m = (df["TurnCr"] >= F["turn"]) & df["vs20DH"].between(*F["c20"]) & df["vsEMA20"].between(*F["e20"]) \
        & df["RSI"].between(*F["rsi"]) & (df["vs52WH"] >= -F["h52"]) & (df["Squeeze"] <= F["bbw"]) \
        & (df["RS3M"] >= F["rs"]) & (df["RVOL"].fillna(0) >= F["rvol"])
    if F["trend"]:
        m &= df["Trend"]
    return df[m].drop(columns="Trend").sort_values("Score", ascending=False).reset_index(drop=True)


def signals(data, names, project=True):
    """{symbol: [strategy names]} whose entry rule fires on the LATEST candle. Same rules as the Backtest tab."""
    def clean(d):
        d = d.copy()
        d.index = pd.DatetimeIndex(d.index).tz_localize(None).normalize()
        return d
    idx63 = clean(data["^NSEI"])["Close"].pct_change(63) * 100
    frac = session_frac() if project else None
    live = session_frac() is not None  # market open: today's candle is unfinished
    out = {}
    for t, d in data.items():
        if t == "^NSEI":
            continue
        f = feats(clean(d), idx63)
        if frac:  # today's volume is partial: project it to a full day, like the screener does
            f.iloc[-1, f.columns.get_loc("RVOL")] /= frac
        hit = [n for n in names if not (live and n.startswith("Short swing:")) and bool(STRATS[n]["fn"](f)[0].iloc[-1])
               and not (n == "Weekly trend breakout" and f.index[-1].weekday() != 4)]  # weekly: only on a Friday candle
        if hit:
            out[t[:-3]] = hit
    return out


def strategy_hits(df, sig):
    """Rows of the indicator table whose symbol is in sig, with a Strategies column, best score first."""
    out = df[df["Symbol"].isin(sig)].drop(columns="Trend")
    out = out.assign(Strategies=out["Symbol"].map(lambda s: ", ".join(sig[s])))
    return out.sort_values("Score", ascending=False).reset_index(drop=True)


def resolve(name):
    """('preset', name) or ('strategy', name). 'Strategy: X' always means the backtest strategy X; a bare name that is a
    screener preset means the preset (so 'Pullback to EMA20' = filter preset, 'Strategy: Pullback to EMA20' = strategy)."""
    if name in PRESETS:
        return "preset", name
    bare = name.removeprefix("Strategy: ")
    return ("strategy", bare) if bare in STRATS else (None, name)


def trade_plan(price, atr_pct, name, risk_rs=1000.0):
    """Ready-to-use plan for strategy `name`: stop/target from the strategy's own ATR multiples, quantity sized so a
    stopped-out trade loses about risk_rs. Entry uses the last close as a stand-in for the next open."""
    s = STRATS[name]
    risk = s["stop"] * atr_pct / 100 * price
    qty = int(risk_rs // risk) if risk > 0 else 0
    return {"Buy near": price, "Stop": price - risk,
            "Target": price + s["target"] * risk if s["target"] > 0 else np.nan,
            "Risk/sh": risk, "Qty": qty, "Capital": qty * price, "Hold d": s["hold"]}
