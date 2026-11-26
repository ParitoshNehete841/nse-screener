"""Portfolio backtest: signals compete for limited cash and position slots (risk-based sizing), with approximate Indian
delivery charges, optional STCG tax, an equity curve against Nifty buy-and-hold, and an in-sample / out-of-sample split."""
import arrow_guard  # noqa: F401  (must run before pandas)
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

import datastore
from backtest import STRATS, get_frames, run, table
from glossary import T

TAX_RATE = 0.20 * 1.04  # STCG 20% + 4% cess (approximation: ignores surcharge and loss carry-forward)


def charges(buy_value, sell_value, slip_pct):
    """Approximate delivery round-trip cost in Rs: STT 0.1% both sides, exchange fee, SEBI fee, stamp duty on buy, GST on the
    fees, DP charge on the sell, plus slippage per side. Rates change: verify with your broker."""
    tot = buy_value + sell_value
    txn, sebi = 0.0000297 * tot, 0.000001 * tot
    return 0.001 * tot + txn + sebi + 0.00015 * buy_value + 0.18 * (txn + sebi) + 15.93 + slip_pct / 100 * tot


def simulate(trades, closes, capital, risk_pct, max_pos, max_pos_pct, slip_pct):
    """Replay trades day by day. Returns (equity Series, executed-trades DataFrame, skipped-signal counts)."""
    skip = {"slots": 0, "dup": 0, "cash": 0}
    if trades.empty:
        return pd.Series(dtype=float), pd.DataFrame(), skip, 0.0
    t = trades.reset_index(drop=True)
    lo, hi = t["Entry"].min(), t["Exit"].max()
    dates = sorted({d for s in closes.values() for d in s.index if lo <= d <= hi})
    order = lambda g: g.sort_values("SigRS", ascending=False, na_position="last").index.tolist()
    by_entry = {d: order(g) for d, g in t.groupby("Entry")}
    same = t["Entry"] == t["Exit"]
    by_exit = {d: g.index.tolist() for d, g in t[~same].groupby("Exit")}
    by_same = {d: g.index.tolist() for d, g in t[same].groupby("Exit")}
    cash, held, mark, eq, log, exposure = float(capital), {}, {}, {}, [], []

    def close_trade(i, qty):
        nonlocal cash
        r = t.loc[i]
        buy, sell = qty * r["EntryPx"], qty * r["ExitPx"]
        fee = charges(buy, sell, slip_pct)
        cash += sell - fee
        log.append(dict(Symbol=r["Symbol"], Strategy=r["Strategy"], Entry=r["Entry"], Exit=r["Exit"], Qty=qty,
                        EntryPx=r["EntryPx"], ExitPx=r["ExitPx"], Charges=fee, NetPnL=sell - buy - fee, Why=r["Why"]))

    for d in dates:
        for i in by_exit.get(d, []):
            if i in held:
                close_trade(i, held.pop(i))
        eq_prev = cash + sum(q * mark.get(t.at[j, "Symbol"], t.at[j, "EntryPx"]) for j, q in held.items())
        for i in by_entry.get(d, []):
            r = t.loc[i]
            if r["Symbol"] in {t.at[j, "Symbol"] for j in held}:
                skip["dup"] += 1
                continue
            if len(held) >= max_pos:
                skip["slots"] += 1
                continue
            risk_ps = r["EntryPx"] - r["Stop"]
            qty = int(min(eq_prev * risk_pct / 100 / risk_ps if risk_ps > 0 else 0, eq_prev * max_pos_pct / 100 / r["EntryPx"],
                          cash / r["EntryPx"]))
            if qty < 1:
                skip["cash"] += 1
                continue
            cash -= qty * r["EntryPx"]
            held[i] = qty
            mark[r["Symbol"]] = r["EntryPx"]
        for i in by_same.get(d, []):  # entered and exited on the same day (e.g. stopped out)
            if i in held:
                close_trade(i, held.pop(i))
        value = 0.0
        for j, q in held.items():
            sym = t.at[j, "Symbol"]
            s = closes[sym]
            if d in s.index:
                mark[sym] = s.at[d]
            value += q * mark.get(sym, t.at[j, "EntryPx"])
        eq[d] = cash + value
        exposure.append(value / eq[d] if eq[d] > 0 else 0.0)
    return pd.Series(eq), pd.DataFrame(log), skip, float(np.mean(exposure) * 100 if exposure else 0.0)


def apply_tax(eq, log):
    """Deduct STCG per Indian financial year (Apr-Mar) on net realised profit, paid at the FY end."""
    if log.empty or eq.empty:
        return eq
    fy = log["Exit"].map(lambda d: d.year if d.month >= 4 else d.year - 1)
    tax = {}
    for y, p in log.groupby(fy)["NetPnL"].sum().items():
        if p > 0:
            due = min(pd.Timestamp(y + 1, 3, 31), eq.index[-1])
            tax[due] = tax.get(due, 0.0) + TAX_RATE * p
    if not tax:
        return eq
    paid = pd.Series(tax).sort_index().cumsum()
    paid = paid.reindex(eq.index.union(paid.index)).ffill().reindex(eq.index).fillna(0.0)
    return eq - paid


def metrics(eq, capital):
    if len(eq) < 2:
        return {}
    days = max((eq.index[-1] - eq.index[0]).days, 1)
    ret = eq.pct_change().dropna()
    cagr = ((eq.iloc[-1] / capital) ** (365.25 / days) - 1) * 100
    mdd = (eq / eq.cummax() - 1).min() * 100
    return {"Final equity (Rs)": eq.iloc[-1], "Total return %": (eq.iloc[-1] / capital - 1) * 100, "CAGR %": cagr,
            "Max drawdown %": mdd, "Sharpe": ret.mean() / ret.std() * np.sqrt(252) if ret.std() > 0 else np.nan,
            "Calmar": cagr / abs(mdd) if mdd < 0 else np.nan}


def run_sim(trades, closes, cfg, tax):
    eq, log, skip, expo = simulate(trades, closes, **cfg)
    eq_net = apply_tax(eq, log) if tax else eq
    m = metrics(eq_net, cfg["capital"])
    if m:
        w = log["NetPnL"] > 0
        m.update({"Trades taken": len(log), "Win %": w.mean() * 100, "Avg net P&L / trade (Rs)": log["NetPnL"].mean(),
                  "Charges paid (Rs)": log["Charges"].sum(), "Avg exposure %": expo,
                  "Signals skipped (full / held / cash)": f"{skip['slots']} / {skip['dup']} / {skip['cash']}"})
    return eq_net, log, m


def render_portfolio(symbols):
    st.title("Portfolio backtest")
    st.caption("A realistic account instead of '1R per signal': limited cash and position slots, risk-based sizing, approximate "
               "Indian delivery charges, optional STCG tax, and a comparison with Nifty buy-and-hold. Uses each strategy's own "
               "stop, target and hold limit.")
    pick = st.multiselect("Strategies in the portfolio", list(STRATS), default=["Breakout 20D + volume", "Pullback to EMA20"],
                          help=T["Portfolio backtest"])
    a, b, c, d = st.columns(4)
    years = a.slider("Years of history", 3, 10, 6, key="pf_years", help=T["Years of history"])
    capital = b.number_input("Starting capital (Rs)", 100_000, 100_000_000, 1_000_000, 100_000, key="pf_cap")
    risk = c.slider("Risk % per trade", 0.25, 3.0, 1.0, 0.25, key="pf_risk", help=T["Risk % per trade"])
    maxpos = d.slider("Max open positions", 1, 30, 8, key="pf_maxpos", help=T["Max open positions"])
    e, f, g, h = st.columns(4)
    cap_pct = e.slider("Max % of equity in one stock", 5, 50, 20, key="pf_cap_pct", help=T["Position cap %"])
    slip = f.slider("Slippage % per side", 0.0, 0.5, 0.10, 0.05, key="pf_slip", help=T["Slippage %"])
    split = g.slider("In-sample share of the period (%)", 50, 90, 70, 5, key="pf_split", help=T["Out-of-sample"])
    tax = h.checkbox("Deduct STCG tax", True, key="pf_tax", help=T["STCG"])

    if st.button("Run portfolio backtest", type="primary"):
        if not pick:
            st.error("Pick at least one strategy.")
            return
        frames, err = get_frames(symbols, years)
        if err:
            st.error(err)
            return
        cutoff = pd.Timestamp(datetime.now() - timedelta(days=365 * years))
        trades = pd.concat([run(frames, k, STRATS[k]["stop"], STRATS[k]["target"], STRATS[k]["hold"], 0.0, cutoff).assign(Strategy=k)
                            for k in pick], ignore_index=True)
        if trades.empty:
            st.warning("No signals in this period for these strategies.")
            return
        closes = {s: f_["Close"] for s, f_ in frames.items()}
        cfg = dict(capital=capital, risk_pct=risk, max_pos=maxpos, max_pos_pct=cap_pct, slip_pct=slip)
        t0, t1 = trades["Entry"].min(), trades["Exit"].max()
        cut = t0 + (t1 - t0) * (split / 100)  # scale by the fraction: span x 70 overflows int64 nanoseconds
        res = {"Full period": run_sim(trades, closes, cfg, tax),
               "In-sample": run_sim(trades[trades["Entry"] < cut], closes, cfg, tax),
               "Out-of-sample": run_sim(trades[trades["Entry"] >= cut], closes, cfg, tax)}
        nifty = datastore.read([datastore.INDEX], (t0 - timedelta(days=5)).strftime("%Y-%m-%d"), min_rows=20).get(datastore.INDEX)
        bench = None
        if nifty is not None:
            nc = nifty["Close"].loc[res["Full period"][0].index[0]: res["Full period"][0].index[-1]]
            bench = nc / nc.iloc[0] * capital
        st.session_state["pf"] = (res, bench, cut, capital, pick)

    if "pf" not in st.session_state:
        return
    res, bench, cut, capital, pick = st.session_state["pf"]
    eq, log, m = res["Full period"]
    if not m:
        st.warning("The simulation produced no trades (position limits or sizing blocked every signal).")
        return
    cols = {k: v[2] for k, v in res.items() if v[2]}
    if bench is not None:
        cols["Nifty buy & hold"] = metrics(bench, capital)
    order = ["Final equity (Rs)", "Total return %", "CAGR %", "Max drawdown %", "Sharpe", "Calmar", "Trades taken", "Win %",
             "Avg net P&L / trade (Rs)", "Charges paid (Rs)", "Avg exposure %", "Signals skipped (full / held / cash)"]
    st.subheader("Results")
    show = lambda x: f"{x:,.2f}" if isinstance(x, (float, np.floating)) else str(x)  # all-text cells: mixed types break Arrow tables
    tbl = pd.DataFrame({k: {o: show(v.get(o, "")) for o in order} for k, v in cols.items()}).reset_index().rename(columns={"index": "Metric"})
    table(tbl)
    ins, oos = cols.get("In-sample"), cols.get("Out-of-sample")
    if ins and oos:
        if oos["CAGR %"] <= 0:
            st.error("Out-of-sample CAGR is not positive: the strategy mix did not hold up on the later data.")
        elif oos["CAGR %"] < 0.5 * ins["CAGR %"]:
            st.warning("Out-of-sample CAGR is under half of in-sample: likely overfit or regime-dependent. Do not size up on this.")
        else:
            st.success("Out-of-sample results held up reasonably against in-sample.")
    if bench is not None and "Nifty buy & hold" in cols:
        st.info(("Beats" if m["CAGR %"] > cols["Nifty buy & hold"]["CAGR %"] else "Lags") +
                f" Nifty buy & hold on CAGR ({m['CAGR %']:.1f}% vs {cols['Nifty buy & hold']['CAGR %']:.1f}%), "
                f"with max drawdown {m['Max drawdown %']:.1f}% vs {cols['Nifty buy & hold']['Max drawdown %']:.1f}%.")
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.7, 0.3], vertical_spacing=0.04)
    fig.add_trace(go.Scatter(x=eq.index, y=eq.values, name="Portfolio"), 1, 1)
    if bench is not None:
        fig.add_trace(go.Scatter(x=bench.index, y=bench.values, name="Nifty buy & hold", line=dict(dash="dot")), 1, 1)
    fig.add_trace(go.Scatter(x=eq.index, y=(eq / eq.cummax() - 1) * 100, name="Drawdown %", fill="tozeroy"), 2, 1)
    fig.add_vline(x=cut, line_dash="dash", annotation_text="out-of-sample starts")
    fig.update_layout(height=520, margin=dict(l=0, r=0, t=30, b=0), legend=dict(orientation="h"))
    st.plotly_chart(fig, use_container_width=True)
    yr = eq.groupby(eq.index.year).last()
    yr = pd.DataFrame({"Year": yr.index, "Return %": (yr / yr.shift(1).fillna(capital) - 1).values * 100})
    st.write("Calendar-year returns (consistency matters more than the total)")
    table(yr)
    st.write(f"Trades taken ({len(log)}), strategies: {', '.join(pick)}")
    out = log.assign(Entry=log["Entry"].dt.strftime("%Y-%m-%d"), Exit=log["Exit"].dt.strftime("%Y-%m-%d")).rename(
        columns={"EntryPx": "Entry px", "ExitPx": "Exit px", "Why": "Exit reason"})
    st.download_button("Download trades CSV", out.to_csv(index=False), "portfolio_trades.csv", "text/csv")
    table(out.tail(150))
    st.caption("Survivorship bias still flatters these results. Charges are approximate Zerodha delivery rates; tax ignores "
               "surcharge and loss carry-forward. Not financial advice.")
