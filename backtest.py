"""Backtest tab: tests swing strategies on daily Yahoo data (long-only, delivery trades).
Signal on a bar's close -> enter next open. Stop is checked before target on the same bar (conservative).
"""
import arrow_guard  # noqa: F401  (must run before pandas)
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import yfinance as yf

from glossary import T, header_html
from core import download
from strategies import STRATS, feats  # noqa: F401  (re-exported for replay.py / paper.py)

try:  # pyarrow powers st.dataframe; some locked-down Windows setups block it
    import pyarrow  # noqa: F401
    HAS_ARROW = True
except ImportError:
    HAS_ARROW = False


def table(df):
    df = df.round(2)
    if HAS_ARROW:
        cfg = {c: st.column_config.Column(help=T[c]) for c in df.columns if c in T}
        st.dataframe(df, hide_index=True, use_container_width=True, column_config=cfg)
    else:
        view = df.rename(columns={c: header_html(c, T[c]) for c in df.columns if c in T})
        html = view.style.format(precision=2, na_rep="-").hide(axis="index").to_html()
        st.markdown(f'<div style="overflow-x:auto">{html}</div>', unsafe_allow_html=True)


# ------------------------------------------------------------------ data + features
@st.cache_data(ttl=3600, show_spinner="Downloading history and computing indicators...")
def prepared(symbols: tuple, years: int):
    start = (datetime.now() - timedelta(days=365 * (years + 1))).strftime("%Y-%m-%d")  # +1y warm-up
    raw, _ = download(symbols, start=start)  # raises (and so is never cached) if nothing came back
    data = {}
    for t, d in raw.items():
        if len(d) > 300:
            d.index = pd.DatetimeIndex(d.index).tz_localize(None).normalize()
            data[t] = d[~d.index.duplicated()]
    if "^NSEI" not in data or len(data) < 2:
        raise RuntimeError("Not enough price history (needs the Nifty index plus a stock with about 300+ trading days).")
    idx63 = data["^NSEI"]["Close"].pct_change(63) * 100
    return {t[:-3]: feats(d, idx63) for t, d in data.items() if t != "^NSEI"}


def get_frames(symbols, years):
    """(frames, error_message). Never raises into the page and never caches a failure."""
    try:
        return prepared(tuple(symbols), years), None
    except Exception as e:
        return {}, f"{e} If Yahoo is rate limiting, wait 10-15 minutes and try again."


# ------------------------------------------------------------------ simulator
def run(frames, name, stop, target, hold, cost, cutoff, end=None):
    fn, rows = STRATS[name]["fn"], []
    for sym, f in frames.items():
        entry, exit_ = fn(f)
        e, x = entry.fillna(False).to_numpy(bool), exit_.fillna(False).to_numpy(bool)
        o, h, l, c, a = (f[k].to_numpy(float) for k in ("Open", "High", "Low", "Close", "ATR"))
        rs = f["RS"].to_numpy(float)  # relative strength at the signal bar: used to rank signals in the portfolio backtest
        dates, n, i = f.index, len(f), 0
        while i < n - 1:
            if end is not None and dates[i] > end:
                break
            if not e[i] or dates[i] < cutoff or not a[i] > 0 or not o[i + 1] > 0:
                i += 1
                continue
            j0, ep = i + 1, o[i + 1]
            risk = stop * a[i]
            sl, tp = ep - risk, (ep + target * risk if target > 0 else np.inf)
            last = min(j0 + hold - 1, n - 1)
            xi, xp, why = last, c[last], ("time" if j0 + hold - 1 <= n - 1 else "open")
            for j in range(j0, last + 1):
                if l[j] <= sl:
                    xi, xp, why = j, min(sl, o[j]), "stop"
                    break
                if h[j] >= tp:
                    xi, xp, why = j, max(tp, o[j]), "target"
                    break
                if x[j] and j + 1 < n:
                    xi, xp, why = j + 1, o[j + 1], "signal"
                    break
            gross = (xp / ep - 1) * 100
            rows.append(dict(Symbol=sym, Entry=dates[j0], Exit=dates[xi], EntryPx=ep, ExitPx=xp, Bars=xi - j0,
                             Ret=gross - cost, R=(xp - ep) / risk - cost / 100 * ep / risk, Why=why,
                             Stop=sl, Target=(tp if np.isfinite(tp) else np.nan),
                             NextRet=(c[j0] / c[i] - 1) * 100, SigRS=rs[i]))  # signal-day close -> next-day close
            i = max(xi, i + 1)
    return pd.DataFrame(rows)


def stats(t):
    if t.empty:
        return {"Trades": 0}
    gains, losses = t.loc[t["R"] > 0, "R"].sum(), -t.loc[t["R"] <= 0, "R"].sum()
    eq = t.sort_values("Exit")["R"].cumsum()
    out = {"Trades": len(t), "Win %": (t["R"] > 0).mean() * 100, "Avg R": t["R"].mean(), "Avg ret %": t["Ret"].mean(),
           "Profit factor": gains / losses if losses > 0 else np.inf, "Avg bars": t["Bars"].mean(),
           "Total R": t["R"].sum(), "Max DD (R)": (eq - eq.cummax()).min()}
    if "NextRet" in t:
        out["Next-day up %"] = (t["NextRet"] > 0).mean() * 100
        out["Avg next-day %"] = t["NextRet"].mean()
    return out


# ------------------------------------------------------------------ UI
def render_backtest(symbols):
    st.title("Backtest")
    st.warning("Read the results with suspicion. The universe is today's list, so it only contains stocks that survived "
               "(survivorship bias inflates results). Assumes every signal is taken with equal risk (1R), no capital limit, "
               "no tax (STCG 20%), and fills at the open or at the stop. Past results do not predict future returns.")
    c1, c2, c3 = st.columns(3)
    name = c1.selectbox("Strategy", list(STRATS), help=T["Strategy"])
    years = c2.slider("Years of history", 2, 10, 6, help=T["Years of history"])
    cost = c3.slider("Round-trip cost + slippage (%)", 0.0, 1.0, 0.30, 0.05, help=T["Cost + slippage"])
    S = STRATS[name]
    st.caption(S["about"])
    d1, d2, d3 = st.columns(3)
    stop = d1.slider("Stop (x ATR)", 0.5, 5.0, S["stop"], 0.5, key=f"stop_{name}", help=T["Stop (x ATR)"])
    target = d2.slider("Target (R, 0 = none)", 0.0, 6.0, S["target"], 0.5, key=f"tgt_{name}", help=T["Target (R)"])
    hold = d3.slider("Max hold (days)", 3, 120, S["hold"], key=f"hold_{name}", help=T["Max hold"])
    b1, b2 = st.columns(2)
    one, every = b1.button("Run selected strategy", type="primary", help="Test only the strategy chosen above with the settings above."), b2.button("Compare all strategies", help="Run every strategy with its own default stop, target and hold, so you can compare them side by side.")

    if one or every:
        frames, err = get_frames(symbols, years)
        if err:
            st.error(err)
            return
        cutoff = pd.Timestamp(datetime.now() - timedelta(days=365 * years))
        with st.spinner("Simulating trades..."):
            res = ({name: run(frames, name, stop, target, hold, cost, cutoff)} if one else
                   {k: run(frames, k, s["stop"], s["target"], s["hold"], cost, cutoff) for k, s in STRATS.items()})
        st.session_state["bt"] = (res, len(frames), years)

    if "bt" not in st.session_state:
        return
    res, nsym, yrs = st.session_state["bt"]
    st.subheader(f"Results - {nsym} stocks, last {yrs} years")
    table(pd.DataFrame([{"Strategy": k, **stats(t)} for k, t in res.items()]))
    fig = go.Figure()
    for k, t in res.items():
        if len(t):
            s = t.sort_values("Exit")
            fig.add_trace(go.Scatter(x=s["Exit"], y=s["R"].cumsum(), name=k, mode="lines"))
    fig.update_layout(title="Cumulative R by exit date", height=400, margin=dict(l=0, r=0, t=40, b=0),
                      legend=dict(orientation="h"))
    st.plotly_chart(fig, use_container_width=True)
    st.caption(T["Cumulative R"])

    pick = st.selectbox("Inspect strategy", list(res), key="bt_pick", help=T["Inspect"])
    t = res[pick]
    if t.empty:
        st.info("No trades for this strategy with these settings.")
        return
    yr = t.assign(Year=t["Exit"].dt.year).groupby("Year").agg(
        Trades=("R", "size"), WinPct=("R", lambda x: (x > 0).mean() * 100), AvgR=("R", "mean"), TotalR=("R", "sum")
    ).reset_index().rename(columns={"WinPct": "Win %", "AvgR": "Avg R", "TotalR": "Total R"})
    st.write("Per year (by exit date) - consistency across years matters more than the total.")
    table(yr)
    out = t.assign(Entry=t["Entry"].dt.strftime("%Y-%m-%d"), Exit=t["Exit"].dt.strftime("%Y-%m-%d")).rename(
        columns={"EntryPx": "Entry px", "ExitPx": "Exit px", "Ret": "Ret %", "Why": "Exit reason", "NextRet": "Next day %"})
    st.write("Exit reasons: " + ", ".join(f"{k} {v}" for k, v in t["Why"].value_counts().items()))
    st.download_button("Download trades CSV", out.to_csv(index=False), f"trades_{pick.split()[0].lower()}.csv", "text/csv")
    table(out.tail(200))
