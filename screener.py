"""NSE swing-trading screener (Yahoo Finance, ~15 min delayed).
Run:  pip install streamlit yfinance pandas numpy plotly
      streamlit run screener.py
"""
import arrow_guard  # noqa: F401  (must run before pandas)
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import yfinance as yf
from plotly.subplots import make_subplots

from backtest import render_backtest
from core import (NIFTY50, PRESETS, add_indicators, apply_filters, build_df, download, session_frac,
                  signals, strategy_hits, trade_plan)
from glossary import COLS, T, header_html, render_glossary
from news import render_news
from paper import render_paper
from auth import gate
from broker import render_broker
from journal import render_journal
from portfolio import render_portfolio
from replay import render_replay
from today import render_today
import datastore
import universe
from strategies import STRATS

try:  # pyarrow powers st.dataframe; locked-down Windows setups can block its DLLs
    import pyarrow  # noqa: F401
    HAS_ARROW = True
except ImportError:
    HAS_ARROW = False

st.set_page_config(page_title="NSE Screener", layout="wide")
gate()  # password sign-in when APP_PASSWORD is set

load = st.cache_data(ttl=240, show_spinner="Fetching prices...")(download)


@st.cache_data(show_spinner="Checking strategy rules...")
def cached_signals(_data, key, names, project, live):  # key (fetch time + symbols) invalidates the cache
    return signals(_data, names, project)


@st.cache_data(ttl=600, show_spinner="Loading stock lists...")
def load_universe(selected, extra, uploads, force):
    return universe.resolve(list(selected), extra, uploads, force)


@st.cache_data(ttl=300)
def data_health(tickers):
    return datastore.health(list(tickers))


ANY, ANY_SHORT = "Any backtest strategy", "Any short-swing strategy"


def strat_names(p):
    if p == ANY:
        return tuple(STRATS)
    if p == ANY_SHORT:
        return tuple(n for n in STRATS if n.startswith("Short swing:"))
    return (p.removeprefix("Strategy: "),)


def candle(d, sym):
    d = add_indicators(d).tail(150)
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.75, 0.25], vertical_spacing=0.03)
    fig.add_trace(go.Candlestick(x=d.index, open=d["Open"], high=d["High"], low=d["Low"], close=d["Close"], name=sym), 1, 1)
    for n, col in ((20, "#f59e0b"), (50, "#3b82f6"), (200, "#ef4444")):
        fig.add_trace(go.Scatter(x=d.index, y=d[f"E{n}"], name=f"EMA{n}", line=dict(width=1.2, color=col)), 1, 1)
    fig.add_trace(go.Bar(x=d.index, y=d["Volume"], name="Volume", marker_color="#94a3b8"), 2, 1)
    fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])])
    fig.update_layout(height=560, xaxis_rangeslider_visible=False, margin=dict(l=0, r=0, t=30, b=0),
                      title=f"{sym} - daily (EMA 20/50/200)", legend=dict(orientation="h"))
    return fig


# ---------------- sidebar ----------------
with st.sidebar:
    st.caption("Hover the (?) icons for plain-English explanations. Full list in the Glossary tab.")
    st.header("Universe")
    sel = st.multiselect("Indices / lists", universe.labels(), default=["Nifty 50"], help=T["Universe"])
    custom = st.text_area("Extra symbols (space/comma separated)", "", help=T["Custom symbols"])
    ups = st.file_uploader("Or upload a constituent CSV (needs a Symbol column)", type="csv", accept_multiple_files=True)
    refresh = st.button("Refresh lists from NSE", help="Download the latest constituents instead of using the copy cached today.")
    symbols, meta, notes = load_universe(tuple(sel), custom, tuple(u.getvalue().decode("utf-8", "ignore") for u in ups), refresh)
    if not symbols:
        st.warning("No stocks selected or loaded. Using the Nifty 50 built-in list.")
        symbols, meta = list(NIFTY50), meta.iloc[0:0]
    st.caption(f"{len(symbols)} stocks in scope")
    for n in notes:
        st.caption(n)
    if len(symbols) > 250:
        st.warning("Large lists download in batches and may take a few minutes. Yahoo may rate-limit; start with Nifty 100 or 200.")
    with st.expander("Data health"):
        try:
            hl = data_health(tuple(s + ".NS" for s in symbols))
            last = datastore.last_sync()
            st.caption(f"{len(hl)} of {len(symbols)} stocks stored; last download {last:%d %b %H:%M}." if last else "Nothing stored yet.")
            bad = hl[hl["Flags"] != ""] if len(hl) else hl
            st.caption(f"{len(bad)} flagged" + (": " + ", ".join(bad["Symbol"].head(12)) if len(bad) else ". All clear."))
            if len(bad):
                st.caption("; ".join(f"{r.Symbol}: {r.Flags}" for r in bad.head(6).itertuples()))
        except Exception as e:
            st.caption(f"Data health unavailable ({type(e).__name__}).")
    st.header("Scan")
    preset = st.selectbox("Preset", list(PRESETS) + [ANY, ANY_SHORT] + [f"Strategy: {n}" for n in STRATS], help=T["Preset"])
    strat_mode = preset in (ANY, ANY_SHORT) or preset.startswith("Strategy: ")
    if strat_mode:
        F = None
        st.info({ANY: "Flags stocks where any backtest strategy fires on the latest candle.",
                 ANY_SHORT: "Flags stocks where any short-swing strategy (buy next open, sell within ~5 days) fired on the latest candle."}
                .get(preset, STRATS.get(preset.removeprefix("Strategy: "), {}).get("about", ""))
                + " Rules are fixed and identical to the Backtest tab, so there are no sliders.")
        if any(n.startswith("Short swing:") for n in strat_names(preset)) and session_frac() is not None:
            st.warning("Market is open. Short-swing signals need the finished daily candle, so they appear after about 15:50 IST.")
    else:
        P = PRESETS[preset]
        k = lambda n: f"{n}_{preset}"  # per-preset widget keys, so switching preset resets the defaults
        F = dict(
            trend=st.checkbox("Uptrend: Close > EMA50 > EMA200", P["trend"], key=k("trend"), help=T["Uptrend filter"]),
            c20=st.slider("Close vs prior 20D high (%)", -15.0, 15.0, P["c20"], 0.5, key=k("c20"), help=T["vs 20D High %"]),
            h52=st.slider("Within X% of 52W high", 0, 50, P["h52"], key=k("h52"), help=T["vs 52W High %"]),
            e20=st.slider("Close vs EMA20 (%)", -15.0, 15.0, P["e20"], 0.5, key=k("e20"), help=T["vs EMA20 %"]),
            rsi=st.slider("RSI(14)", 0, 100, P["rsi"], key=k("rsi"), help=T["RSI"]),
            bbw=st.slider("Max squeeze percentile (low = tight)", 0, 100, P["bbw"], key=k("bbw"), help=T["Squeeze pctl"]),
            rvol=st.slider("Min relative volume (x)", 0.0, 5.0, P["rvol"], 0.1, key=k("rvol"), help=T["RVOL"]),
            rs=st.slider("Min 3M strength vs Nifty (pts)", -50.0, 50.0, P["rs"], 1.0, key=k("rs"), help=T["3M RS"]),
            turn=st.slider("Min avg turnover (Rs Cr/day)", 0.0, 200.0, P["turn"], 1.0, key=k("turn"), help=T["Turnover Cr"]),
        )
    proj = st.checkbox("Project intraday volume to full day", True, help=T["Project volume"])
    every = {"Off": None, "5 min": "5m", "15 min": "15m"}[st.selectbox("Auto-refresh", ["Off", "5 min", "15 min"], index=2, help=T["Auto-refresh"])]
    if st.button("Refresh now", help="Clear the cached prices and download them again."):
        load.clear()


def render():
    try:
        data, fetched = load(tuple(symbols))
    except Exception as e:
        st.title("NSE Swing Screener")
        st.error(f"Could not download prices: {e}")
        st.caption("If it is rate limiting, wait 10-15 minutes and press Refresh now (once). Other tabs are unaffected.")
        return
    if "^NSEI" not in data:
        st.error("Could not fetch Nifty index data - check your connection / Yahoo rate limits.")
        return
    df, idx63 = build_df(data, proj)
    missing = [s for s in symbols if s + ".NS" not in data]
    if df.empty:
        st.title("NSE Swing Screener")
        st.error(f"Yahoo Finance returned no usable price data for any of the {len(symbols)} symbols. Look in the terminal "
                 "for 'Too Many Requests' or connection errors. If it is rate limiting, wait 10-15 minutes and press "
                 "Refresh now. Also check the Custom box has plain NSE symbols (RELIANCE, TCS), and that your network "
                 "or VPN allows Yahoo Finance.")
        return

    st.title("NSE Swing Screener")
    status = "market open (live, ~15 min delayed)" if session_frac() else "market closed - showing last close"
    st.caption(f"{status} | fetched {fetched:%d %b %H:%M:%S} IST | last bar {data['^NSEI'].index[-1]:%d %b %Y} | "
               f"Nifty 3M: {idx63:+.1f}% | {len(df)} stocks scanned" + (f" | no data: {', '.join(missing)}" if missing else ""))

    if strat_mode:
        names = strat_names(preset)
        out = strategy_hits(df, cached_signals(data, (fetched, tuple(symbols)), names, proj, session_frac() is not None))
    else:
        out = apply_filters(df, F)
    if strat_mode and len(names) == 1 and len(out):  # single strategy: add the ready-to-use plan columns
        plans = pd.DataFrame([trade_plan(r.Price, r.ATRpct, names[0], st.session_state.get("risk_rs", 1000)) for r in out.itertuples()])
        out = pd.concat([out, plans[["Stop", "Target", "Qty", "Capital"]]], axis=1)
    st.subheader(f"{len(out)} matches - {preset}")

    pick = None
    if HAS_ARROW:
        ev = st.dataframe(
            out, hide_index=True, use_container_width=True, on_select="rerun", selection_mode="single-row",
            column_config={
                "Price": st.column_config.NumberColumn("LTP", format="%.2f", help=T[COLS["Price"]]),
                "ChgPct": st.column_config.NumberColumn("Chg %", format="%+.2f", help=T[COLS["ChgPct"]]),
                "RVOL": st.column_config.NumberColumn("RVOL", format="%.2fx", help=T[COLS["RVOL"]]),
                "RSI": st.column_config.NumberColumn("RSI", format="%.0f", help=T[COLS["RSI"]]),
                "vs20DH": st.column_config.NumberColumn("vs 20D High %", format="%+.2f", help=T[COLS["vs20DH"]]),
                "vs52WH": st.column_config.NumberColumn("vs 52W High %", format="%+.1f", help=T[COLS["vs52WH"]]),
                "vsEMA20": st.column_config.NumberColumn("vs EMA20 %", format="%+.2f", help=T[COLS["vsEMA20"]]),
                "Squeeze": st.column_config.NumberColumn("Squeeze pctl", format="%.0f", help=T[COLS["Squeeze"]]),
                "RS3M": st.column_config.NumberColumn("3M RS", format="%+.1f", help=T[COLS["RS3M"]]),
                "ATRpct": st.column_config.NumberColumn("ATR %", format="%.2f", help=T[COLS["ATRpct"]]),
                "TurnCr": st.column_config.NumberColumn("Turnover Cr", format="%.0f", help=T[COLS["TurnCr"]]),
                "Score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%.0f", help=T[COLS["Score"]]),
                "Chart": st.column_config.LinkColumn("TradingView", display_text="Open", help=T[COLS["Chart"]]),
                "Strategies": st.column_config.TextColumn("Strategies", help=T["Strategies"]),
            },
        )
        if ev.selection.rows:
            pick = out.loc[ev.selection.rows[0], "Symbol"]
    else:  # plain HTML table - no pyarrow needed
        st.warning("pyarrow is blocked on this machine, so this is a plain table. Pick a stock below for its chart.")
        fmt = {"Price": "{:.2f}", "ChgPct": "{:+.2f}", "RVOL": "{:.2f}x", "RSI": "{:.0f}", "vs20DH": "{:+.2f}",
               "vs52WH": "{:+.1f}", "vsEMA20": "{:+.2f}", "Squeeze": "{:.0f}", "RS3M": "{:+.1f}",
               "ATRpct": "{:.2f}", "TurnCr": "{:.0f}", "Score": "{:.0f}",
               "Chart": lambda u: f'<a href="{u}" target="_blank">Open</a>'}
        lab = {c: header_html(COLS[c], T[COLS[c]]) for c in out.columns if c in COLS}
        fmt = {lab.get(c, c): f for c, f in fmt.items()}
        html = out.rename(columns=lab).style.format(fmt, na_rep="-", precision=2).hide(axis="index").to_html()
        st.markdown(f'<div style="overflow-x:auto">{html}</div>', unsafe_allow_html=True)
        if len(out):
            pick = st.selectbox("Chart for", out["Symbol"], key="pick", help="Daily candlesticks with EMA20/50/200 and volume.")
    st.download_button("Download CSV", out.to_csv(index=False), "screener.csv", "text/csv")
    if pick:
        st.plotly_chart(candle(data[pick + ".NS"], pick), use_container_width=True)
    else:
        st.info("Select a stock to see its chart." if len(out) else "No matches - loosen the filters.")

def safe(fn, *args):
    """Run one tab; if it fails, show the error there instead of stopping the whole page (which blanks later tabs)."""
    try:
        fn(*args)
    except Exception as e:
        st.error(f"This tab hit an error: {type(e).__name__}: {e}")
        st.caption("The other tabs still work. Yahoo problems: wait 10-15 minutes. 'Application Control policy' problems: "
                   "run  .\\.venv\\Scripts\\python.exe -m pip uninstall -y pyarrow  and restart.")
        with st.expander("Technical details"):
            st.code(traceback.format_exc())


def snapshot():
    """Everything the Today page needs, from the same cached download as the screener."""
    data, fetched = load(tuple(symbols))
    df, _ = build_df(data, proj)
    sig = cached_signals(data, (fetched, tuple(symbols)), tuple(STRATS), proj, session_frac() is not None)
    return data, df, fetched, sig


def render_safe():
    safe(render)


tab_today, tab_scan, tab_bt, tab_pf, tab_rp, tab_pp, tab_jr, tab_bk, tab_news, tab_gl = st.tabs(["Today", "Screener", "Backtest", "Portfolio", "Replay", "Paper trading", "Journal", "Broker", "News", "Glossary"])
with tab_today:
    safe(render_today, symbols, snapshot, dict(zip(meta["Symbol"], meta["Industry"])))
with tab_scan:
    st.fragment(run_every=every)(render_safe)()
with tab_bt:
    safe(render_backtest, symbols)
with tab_pf:
    safe(render_portfolio, symbols)
with tab_rp:
    safe(render_replay, symbols)
with tab_pp:
    safe(render_paper)
with tab_jr:
    safe(render_journal, symbols, dict(zip(meta["Symbol"], meta["Industry"])))
with tab_bk:
    safe(render_broker, symbols)
with tab_news:
    safe(render_news, symbols)
with tab_gl:
    safe(render_glossary)
