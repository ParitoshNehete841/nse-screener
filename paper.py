"""Paper trading: log trades you would take, then let the app track how they end (same exit rules as the backtest)."""
import arrow_guard  # noqa: F401  (must run before pandas)
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from backtest import STRATS, get_frames, prepared, stats, table
from glossary import T

FILE = Path(__file__).resolve().parent / "paper_trades.csv"
COLS = ["ID", "Symbol", "Strategy", "Opened", "EntryPx", "Stop", "Target", "Hold", "Status", "Closed", "ExitPx", "Why",
        "R", "Ret", "Bars", "Notes"]


def load_log():
    df = pd.read_csv(FILE) if FILE.exists() else pd.DataFrame(columns=COLS)
    for c in COLS:
        if c not in df:
            df[c] = np.nan
    df = df[COLS].copy()
    for c in ("Opened", "Closed"):
        df[c] = pd.to_datetime(df[c])
    for c in ("ID", "EntryPx", "Stop", "Target", "Hold", "ExitPx", "R", "Ret", "Bars"):
        df[c] = pd.to_numeric(df[c])
    for c in ("Symbol", "Strategy", "Status", "Why", "Notes"):
        df[c] = df[c].astype(object)
    return df


def add_trade(log, sym, strat, opened, price, stop_x, tgt_r, hold, notes):
    frames, err = get_frames((sym,), 2)
    if err or sym not in frames:
        return log, err or f"No price data for {sym}. Check the symbol (NSE, no .NS)."
    f = frames[sym]
    i = f.index.searchsorted(pd.Timestamp(opened), side="right") - 1  # last trading day on/before the entry date
    if i < 0:
        return log, "No data before that date."
    ep = price if price > 0 else float(f["Close"].iloc[i])
    risk = stop_x * float(f["ATR"].iloc[i])
    row = dict(ID=int(log["ID"].max() + 1) if len(log) else 1, Symbol=sym, Strategy=strat, Opened=f.index[i], EntryPx=ep,
               Stop=ep - risk, Target=ep + tgt_r * risk if tgt_r > 0 else np.nan, Hold=hold, Status="open", Notes=notes)
    log = pd.concat([log, pd.DataFrame([row])], ignore_index=True)
    log.to_csv(FILE, index=False)
    return load_log(), f"Logged {sym} ({strat}) at {ep:.2f}, stop {row['Stop']:.2f}."


def evaluate(r, f, cost):
    """Walk the trade forward from the bar after entry, using the strategy's own exit rules."""
    x = STRATS[r["Strategy"]]["fn"](f)[1].fillna(False).to_numpy(bool)
    o, h, l, c = (f[k].to_numpy(float) for k in ("Open", "High", "Low", "Close"))
    n, j0 = len(f), f.index.searchsorted(r["Opened"])
    ep, sl = r["EntryPx"], r["Stop"]
    tp = r["Target"] if pd.notna(r["Target"]) else np.inf
    risk, last = ep - sl, j0 + int(r["Hold"]) - 1
    for j in range(j0 + 1, min(last, n - 1) + 1):
        why = None
        if l[j] <= sl:
            xi, xp, why = j, min(sl, o[j]), "stop"
        elif h[j] >= tp:
            xi, xp, why = j, max(tp, o[j]), "target"
        elif x[j] and j + 1 < n:
            xi, xp, why = j + 1, o[j + 1], "signal"
        elif j == last:
            xi, xp, why = j, c[j], "time"
        if why:
            return dict(Status="closed", Closed=f.index[xi], ExitPx=xp, Why=why, Bars=xi - j0,
                        R=(xp - ep) / risk - cost / 100 * ep / risk, Ret=(xp / ep - 1) * 100 - cost)
    alert = "exit signal fired - sells at next open" if x[-1] and n - 1 > j0 else ""
    return dict(Status="open", MarkPx=c[-1], MarkR=(c[-1] - ep) / risk, Alert=alert)


def refresh(log, cost):
    opn = log[log["Status"] == "open"]
    if opn.empty:
        return log, {}
    prepared.clear()  # force a fresh download
    frames, err = get_frames(sorted(set(opn["Symbol"])), 2)
    if err:
        st.error(err)
        return log, {}
    marks = {}
    for idx, r in opn.iterrows():
        f = frames.get(r["Symbol"])
        if f is None:
            continue
        res = evaluate(r, f, cost)
        if res["Status"] == "closed":
            for k, v in res.items():
                log.loc[idx, k] = v
        else:
            marks[r["ID"]] = res
    log.to_csv(FILE, index=False)
    return load_log(), marks


def render_paper():
    st.title("Paper trading")
    st.caption("Log trades you would take (no real money). The app tracks each one forward with the strategy's own stop, "
               "target and exit rule, then shows your real success rate. Saved in paper_trades.csv next to the app.")
    log = load_log()
    with st.expander("Log a paper trade", expanded=len(log) == 0):
        a, b, c = st.columns(3)
        sym = a.text_input("Symbol (NSE, no .NS)", key="pp_sym").strip().upper().removesuffix(".NS")
        strat = b.selectbox("Strategy", list(STRATS), help=T["Strategy"], key="pp_strat")
        opened = c.date_input("Entry date", date.today(), max_value=date.today(), key="pp_date")
        S = STRATS[strat]
        d, e, f_, g = st.columns(4)
        price = d.number_input("Entry price (0 = close of entry date)", 0.0, step=0.5, key="pp_price")
        stop_x = e.slider("Stop (x ATR)", 0.5, 5.0, S["stop"], 0.5, key=f"pp_stop_{strat}", help=T["Stop (x ATR)"])
        tgt = f_.slider("Target (R, 0 = none)", 0.0, 6.0, S["target"], 0.5, key=f"pp_tgt_{strat}", help=T["Target (R)"])
        hold = g.slider("Max hold (days)", 3, 120, S["hold"], key=f"pp_hold_{strat}", help=T["Max hold"])
        notes = st.text_input("Notes (why you would take it)", key="pp_notes")
        if st.button("Add paper trade", type="primary"):
            if not sym:
                st.error("Enter a symbol.")
            else:
                log, msg = add_trade(log, sym, strat, opened, price, stop_x, tgt, hold, notes)
                st.info(msg)

    cost = st.slider("Round-trip cost + slippage (%)", 0.0, 1.0, 0.30, 0.05, help=T["Cost + slippage"], key="pp_cost")
    if st.button("Update open trades from latest prices", help="Downloads fresh prices and checks every open trade against its stop, target and exit rule."):
        log, st.session_state["marks"] = refresh(log, cost)
    marks = st.session_state.get("marks", {})

    opn, closed = log[log["Status"] == "open"], log[log["Status"] == "closed"]
    st.subheader(f"Open trades ({len(opn)})")
    if opn.empty:
        st.info("No open paper trades.")
    else:
        view = opn.assign(
            Opened=opn["Opened"].dt.strftime("%Y-%m-%d"),
            **{"Mark px": opn["ID"].map(lambda i: marks.get(i, {}).get("MarkPx")),
               "Unrealized R": opn["ID"].map(lambda i: marks.get(i, {}).get("MarkR")),
               "Alert": opn["ID"].map(lambda i: marks.get(i, {}).get("Alert", ""))})
        table(view.rename(columns={"EntryPx": "Entry px"})[["ID", "Symbol", "Strategy", "Opened", "Entry px", "Stop", "Target",
                                                           "Mark px", "Unrealized R", "Alert", "Notes"]])
        st.caption("Press the update button to refresh Mark px and Unrealized R.")

    st.subheader(f"Closed trades ({len(closed)})")
    if closed.empty:
        st.info("No closed paper trades yet. Update open trades after a few trading days.")
    else:
        cl = closed.rename(columns={"Closed": "Exit"})
        table(pd.DataFrame([{"Strategy": "ALL", **stats(cl)}] + [{"Strategy": k, **stats(g)} for k, g in cl.groupby("Strategy")]))
        s = cl.sort_values("Exit")
        fig = go.Figure(go.Scatter(x=s["Exit"], y=s["R"].cumsum(), mode="lines+markers", name="Paper"))
        fig.update_layout(title="Paper trading: cumulative R", height=320, margin=dict(l=0, r=0, t=40, b=0))
        st.plotly_chart(fig, use_container_width=True)
        table(closed.assign(Opened=closed["Opened"].dt.strftime("%Y-%m-%d"), Closed=closed["Closed"].dt.strftime("%Y-%m-%d"))
              .rename(columns={"EntryPx": "Entry px", "ExitPx": "Exit px", "Ret": "Ret %", "Why": "Exit reason"})
              [["ID", "Symbol", "Strategy", "Opened", "Closed", "Entry px", "Exit px", "Exit reason", "R", "Ret %", "Bars", "Notes"]])
        st.caption("Under about 30 closed trades the success rate is mostly luck. Keep logging.")

    if len(log):
        with st.expander("Delete a trade"):
            did = st.selectbox("Trade", log["ID"].astype(int), format_func=lambda i: f"#{i} " + str(log.loc[log['ID'] == i, 'Symbol'].iloc[0]), key="pp_del")
            if st.button("Delete"):
                log[log["ID"] != did].to_csv(FILE, index=False)
                st.rerun()
