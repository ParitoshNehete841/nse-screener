"""Replay tab: what would each strategy have done in a past date window (one stock or the whole universe)?"""
import arrow_guard  # noqa: F401  (must run before pandas)
from datetime import date
from math import ceil

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from backtest import STRATS, get_frames, run, stats, table
from glossary import T


def trade_chart(f, t):
    i, j = f.index.searchsorted(t["Entry"]), f.index.searchsorted(t["Exit"])
    d = f.iloc[max(i - 40, 0): min(j + 20, len(f))]
    fig = go.Figure(go.Candlestick(x=d.index, open=d["Open"], high=d["High"], low=d["Low"], close=d["Close"], name=t["Symbol"]))
    for n, col in ((20, "#f59e0b"), (50, "#3b82f6"), (200, "#ef4444")):
        fig.add_trace(go.Scatter(x=d.index, y=d[f"E{n}"], name=f"EMA{n}", line=dict(width=1.2, color=col)))
    fig.add_trace(go.Scatter(x=[t["Entry"]], y=[t["EntryPx"]], mode="markers", name="Entry",
                             marker=dict(symbol="triangle-up", size=14, color="#16a34a")))
    fig.add_trace(go.Scatter(x=[t["Exit"]], y=[t["ExitPx"]], mode="markers", name=f"Exit ({t['Why']})",
                             marker=dict(symbol="x", size=13, color="#dc2626")))
    fig.add_hline(y=t["Stop"], line_dash="dot", line_color="#dc2626", annotation_text="stop")
    if pd.notna(t["Target"]):
        fig.add_hline(y=t["Target"], line_dash="dot", line_color="#16a34a", annotation_text="target")
    fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])])
    fig.update_layout(height=520, xaxis_rangeslider_visible=False, margin=dict(l=0, r=0, t=40, b=0),
                      title=f"{t['Symbol']} - {t['Strategy']} ({t['R']:+.1f}R)", legend=dict(orientation="h"))
    return fig


def render_replay(symbols):
    st.title("Replay a past window")
    st.caption("Pick a date window and see every stock that triggered a strategy in it, and how each trade ended. "
               "Same rules, stops and costs as the Backtest tab. Open = still running at the latest price.")
    today = date.today()
    y = today.year if today > date(today.year, 4, 30) else today.year - 1
    c1, c2 = st.columns(2)
    scope = c1.radio("Stocks", ["Sidebar universe", "One stock"], horizontal=True, help=T["Replay scope"])
    sym = c2.text_input("Symbol (NSE, no .NS)", "RELIANCE", disabled=scope != "One stock").strip().upper().removesuffix(".NS")
    d1, d2, d3 = st.columns(3)
    win = d1.date_input("Signals between", (date(y, 4, 1), date(y, 4, 30)), max_value=today, help=T["Replay window"])
    strat = d2.selectbox("Strategy", ["All strategies"] + list(STRATS), help=T["Strategy"], key="rp_strat")
    cost = d3.slider("Round-trip cost + slippage (%)", 0.0, 1.0, 0.30, 0.05, help=T["Cost + slippage"], key="rp_cost")

    if st.button("Replay", type="primary"):
        if len(win) != 2:
            st.error("Pick both a start date and an end date.")
            return
        syms = (sym,) if scope == "One stock" else tuple(symbols)
        years = max(2, ceil((today - win[0]).days / 365.25))
        frames, err = get_frames(syms, years)
        if err:
            st.error(err)
            return
        parts = []
        for k in (list(STRATS) if strat == "All strategies" else [strat]):
            s = STRATS[k]
            t = run(frames, k, s["stop"], s["target"], s["hold"], cost, pd.Timestamp(win[0]), pd.Timestamp(win[1]))
            if len(t):
                parts.append(t.assign(Strategy=k))
        st.session_state["replay"] = (pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(), syms, years, win)

    if "replay" not in st.session_state:
        return
    trades, syms, years, win = st.session_state["replay"]
    st.subheader(f"{win[0]:%d %b %Y} to {win[1]:%d %b %Y}: {len(trades)} trades")
    if trades.empty:
        st.info("No signals in this window. Try a longer window, another strategy, or more stocks.")
        return
    table(pd.DataFrame([{"Strategy": k, **stats(g)} for k, g in trades.groupby("Strategy", sort=False)]))
    out = trades.sort_values("Entry").reset_index(drop=True)
    table(out.assign(Entry=out["Entry"].dt.strftime("%Y-%m-%d"), Exit=out["Exit"].dt.strftime("%Y-%m-%d"))
          .rename(columns={"EntryPx": "Entry px", "ExitPx": "Exit px", "Ret": "Ret %", "Why": "Exit reason", "NextRet": "Next day %"})
          [["Symbol", "Strategy", "Entry", "Exit", "Entry px", "Exit px", "Stop", "Target", "Bars", "Next day %", "Ret %", "R", "Exit reason"]])
    labels = [f"{r.Symbol} | {r.Strategy} | {r.Entry:%d %b %Y} | {r.R:+.1f}R" for r in out.itertuples()]
    pick = st.selectbox("Chart a trade", range(len(out)), format_func=lambda i: labels[i], key="rp_pick")
    row = out.iloc[pick]
    frames, err = get_frames(syms, years)
    if err:
        st.error(err)
        return
    st.plotly_chart(trade_chart(frames[row["Symbol"]], row), use_container_width=True)
    st.caption("Green triangle = entry, red cross = exit, dotted lines = stop and target. Trades are entered by signals "
               "inside the window but may exit after it.")
