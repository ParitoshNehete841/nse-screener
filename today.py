"""Today tab, a decision page: market regime -> ranked trade plans (buy near, stop, target, quantity) -> only for
strategies that have shown an edge in your own backtest -> one click to paper trade -> reality check of paper vs backtest."""
import arrow_guard  # noqa: F401  (must run before pandas)
from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st

from backtest import prepared, run, stats, table
import events
from core import session_frac, trade_plan
from glossary import T
from journal import closed_metrics, perf_table, trades as journal_trades
from paper import add_trade, load_log
from strategies import STRATS

DIPS = {"Pullback to EMA20", "RSI(2) mean reversion", "Short swing: IBS oversold bounce",
        "Short swing: 3-day dip in uptrend", "Short swing: Reversal candle at EMA20"}
MIN_TRADES = 100


def regime(close):
    last = close.iloc[-1]
    e50, e200 = (close.ewm(span=n, adjust=False).mean().iloc[-1] for n in (50, 200))
    label = "UPTREND" if last > e50 > e200 else "PULLBACK" if last > e200 else "DOWNTREND"
    return label, last, e50, e200, (last / close.iloc[-64] - 1) * 100


def fit(strategy, label):
    if label == "UPTREND":
        return "Good"
    if label == "PULLBACK":
        return "Good" if strategy in DIPS else "Selective"
    return "Selective" if strategy in DIPS else "Weak"


@st.cache_data(ttl=3600, show_spinner="Backtesting every strategy on this stock list (about a minute the first time)...")
def track_record(symbols: tuple, years: int, cost: float):
    frames = prepared(symbols, years)  # raises on a failed download, so failures are never cached
    cutoff = pd.Timestamp(datetime.now() - timedelta(days=365 * years))
    return pd.DataFrame([{"Strategy": k, **stats(run(frames, k, s["stop"], s["target"], s["hold"], cost, cutoff))}
                         for k, s in STRATS.items()])


def verdict(row):
    if pd.isna(row.get("Trades")) or row["Trades"] < MIN_TRADES:
        return "Low sample"
    return "Edge" if row["Avg R"] > 0 else "No tested edge"


def candidates(df, sig, rec, label, risk, industry):
    d = df.set_index("Symbol")
    rows = []
    for sym, names in sig.items():
        r = d.loc[sym]
        for n in names:
            rows.append({"Symbol": sym, "Strategy": n, "Fit": fit(n, label), **trade_plan(r["Price"], r["ATRpct"], n, risk),
                         "Score": r["Score"], "Industry": industry.get(sym, "")})
    c = pd.DataFrame(rows)
    if c.empty:
        return c
    if rec is not None:
        keep = ["Strategy", "Trades", "Win %", "Avg R", "Profit factor", "Next-day up %"]
        c = c.merge(rec[[k for k in keep if k in rec]], on="Strategy", how="left")
        c["Verdict"] = c.apply(verdict, axis=1)
        c["_rank"] = c["Verdict"].map({"Edge": 0, "Low sample": 1, "No tested edge": 2})
        return c.sort_values(["_rank", "Avg R", "Score"], ascending=[True, False, False]).drop(columns="_rank")
    return c.sort_values("Score", ascending=False)


def plan_text(r):
    tgt = (f"Target {r['Target']:.2f}" if pd.notna(r["Target"]) else f"Target (suggested 2R) {r['Buy near'] + 2 * r['Risk/sh']:.2f}")
    return (f"{r['Symbol']} | {r['Strategy']}\nBuy ~{r['Buy near']:.2f} at next open | qty {int(r['Qty'])} (~Rs {r['Capital']:,.0f})\n"
            f"Stop {r['Stop']:.2f} | {tgt}\nExit by the strategy rule or after {int(r['Hold d'])} trading days")


def render_today(symbols, snapshot, industry):
    st.title("Today")
    st.caption("From signals to a plan: what fired on the latest candle, sized to your risk, and only from strategies "
               "that have proven themselves on your own stock list.")
    data, df, fetched, sig = snapshot()
    label, last, e50, e200, ret63 = regime(data["^NSEI"]["Close"])
    breadth = df["Trend"].mean() * 100
    msg = (f"Market regime: {label}. Nifty {last:,.0f} vs EMA50 {e50:,.0f} and EMA200 {e200:,.0f}; 3-month {ret63:+.1f}%. "
           f"{breadth:.0f}% of your {len(df)} stocks are in an uptrend.")
    {"UPTREND": st.success, "PULLBACK": st.warning, "DOWNTREND": st.error}[label](msg)
    if session_frac() is not None:
        st.caption("Market is open: short-swing setups need the finished daily candle and appear after about 15:50 IST.")

    c1, c2, c3, c4 = st.columns(4)
    risk = c1.number_input("Risk per trade (Rs)", 100, 1_000_000, 1000, 100, key="risk_rs", help=T["Risk per trade"])
    years = c2.slider("Years to test", 3, 8, 6, key="td_years", help=T["Years of history"])
    cost = c3.slider("Round-trip cost (%)", 0.0, 1.0, 0.30, 0.05, key="td_cost", help=T["Cost + slippage"])
    if c4.button("Test every strategy", type="primary", help="Runs all strategies over the chosen years on this stock list. Results rank the setups below."):
        try:
            st.session_state["rec"] = track_record(tuple(symbols), years, cost)
        except Exception as e:
            st.error(f"Could not run the test: {e} If Yahoo is rate limiting, wait 10-15 minutes.")
    rec = st.session_state.get("rec")
    if rec is None:
        st.info("Press 'Test every strategy' to rank setups by their tested edge. Until then they are sorted by stock score only.")
    else:
        rec = rec.assign(Fit=rec["Strategy"].map(lambda n: fit(n, label)), Verdict=rec.apply(verdict, axis=1))

    cand = candidates(df, sig, rec, label, risk, industry)
    only_edge = st.checkbox(f"Only show setups from strategies with a tested edge ({MIN_TRADES}+ trades, average R above 0)", True,
                            disabled=rec is None, help=T["Tested edge"])
    show = cand[cand["Verdict"] == "Edge"] if (rec is not None and only_edge and not cand.empty) else cand
    st.subheader(f"Setups on the latest candle ({len(show)})")
    if show.empty:
        st.info("Nothing qualifies right now." if rec is not None and only_edge else "No strategy fired on the latest candle.")
    else:
        cols = [c for c in ["Symbol", "Strategy", "Verdict", "Fit", "Buy near", "Stop", "Target", "Qty", "Capital", "Hold d", "Avg R",
                            "Win %", "Next-day up %", "Trades", "Industry"] if c in show]
        shown_syms = list(dict.fromkeys(show.head(25)["Symbol"]))
        if st.button("Check event risk for these setups", help=T["Events"]):
            st.session_state["setup_events"] = events.upcoming(shown_syms, days=14)
        ev = st.session_state.get("setup_events")
        view = show[cols].head(40).copy()
        if ev is not None:
            hold = dict(zip(show["Symbol"] + "|" + show["Strategy"], show["Hold d"]))
            view["Events"] = [events.flag_text(ev.get(r.Symbol, []), within=int(hold.get(r.Symbol + "|" + r.Strategy, 7)) + 2)
                              for r in view.itertuples()]
            n_ev = int((view["Events"] != "").sum())
            if n_ev:
                st.warning(f"{n_ev} setup(s) have results or an ex-dividend date inside their holding window. "
                           "Holding through results is a common swing-trade killer: skip them or plan to exit before.")
            st.caption("Dates come from Yahoo and are often incomplete for Indian stocks; confirm on the company's announcements.")
        table(view)
        top = show.head(10)["Industry"].replace("", pd.NA).dropna()
        if len(top) and top.value_counts().iloc[0] >= 4:
            st.warning(f"{top.value_counts().iloc[0]} of the top 10 setups are in {top.value_counts().index[0]}. "
                       "They tend to move together, so size down or pick one.")
        shown = show.head(40).reset_index(drop=True)
        i = st.selectbox("Pick a setup", range(len(shown)), key="td_pick",
                         format_func=lambda k: f"{shown.loc[k, 'Symbol']} | {shown.loc[k, 'Strategy']} | qty {int(shown.loc[k, 'Qty'])}")
        row = shown.loc[i]
        st.session_state["tickets"] = shown.head(15)[["Symbol", "Strategy", "Qty", "Buy near", "Stop", "Target", "Risk/sh"]].to_dict("records")
        st.download_button("Download these plans (CSV)", shown[["Symbol", "Strategy", "Qty", "Buy near", "Stop", "Target", "Capital", "Hold d"]].round(2)
                           .to_csv(index=False), "trade_plans.csv", "text/csv", help="For your own checklist or to enter orders by hand. The Broker tab can send reviewed orders.")
        st.code(plan_text(row), language=None)
        st.caption("Buy near = last close as a stand-in for tomorrow's open. Check the live price and lot rules before ordering. "
                   "Use the stop and target as your GTT/OCO levels.")
        if st.button("Add to paper trades", help="Logs this setup in the Paper trading tab so the app tracks how it ends."):
            S = STRATS[row["Strategy"]]
            _, note = add_trade(load_log(), row["Symbol"], row["Strategy"], date.today(), 0.0, S["stop"], S["target"], S["hold"], "from Today")
            st.info(note)

    if rec is not None:
        st.subheader("Strategy scoreboard (your stock list, tested)")
        table(rec.sort_values("Avg R", ascending=False)[[c for c in ["Strategy", "Verdict", "Fit", "Trades", "Win %", "Avg R", "Profit factor",
                                                                    "Next-day up %", "Max DD (R)"] if c in rec]])
        st.caption("Survivorship bias flatters these numbers (today's index members only). Treat them as an upper bound.")

    st.subheader("Reality check: your paper trades vs the backtest")
    log = load_log()
    closed = log[log["Status"] == "closed"].rename(columns={"Closed": "Exit"})
    if closed.empty:
        st.info("No closed paper trades yet. Add setups above, then press 'Update open trades' in the Paper trading tab.")
    else:
        rows = [{"Strategy": k, "Paper trades": len(g), "Paper win %": (g["R"] > 0).mean() * 100, "Paper avg R": g["R"].mean()}
                for k, g in closed.groupby("Strategy")]
        rc = pd.DataFrame(rows)
        if rec is not None:
            rc = rc.merge(rec[["Strategy", "Avg R"]].rename(columns={"Avg R": "Backtest avg R"}), on="Strategy", how="left")
            rc["Status"] = rc.apply(lambda r: "Too few trades (<30)" if r["Paper trades"] < 30 else
                                    "On track" if r["Paper avg R"] > 0 and r["Paper avg R"] >= 0.5 * max(r["Backtest avg R"], 0.05)
                                    else "Underperforming", axis=1)
        table(rc)
        st.caption("Trust a strategy when paper results hold up over 30+ trades. If they lag the backtest, pause it.")
    real = closed_metrics(journal_trades())
    if not real.empty:
        st.write("Your real trades (Journal tab)")
        table(perf_table(real))
