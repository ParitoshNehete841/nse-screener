"""Journal and risk: your real trades (manual entry or Zerodha tradebook CSV import, matched FIFO), performance by strategy,
and a view of the open book: value, open risk to stops, sector exposure and event-risk flags."""
import arrow_guard  # noqa: F401  (must run before pandas)
import csv
import io
import os
import sqlite3
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import events
import universe
from backtest import STRATS, table
from core import download
from glossary import T
from portfolio import charges

DB = Path(os.environ.get("NSE_SCREENER_JOURNAL") or Path(__file__).resolve().parent / "data" / "journal.db")
UNTAGGED, DISCRETIONARY = "Untagged", "Discretionary"
RISK_LIMIT_PCT, SECTOR_LIMIT_PCT, POSITION_LIMIT_PCT = 5.0, 30.0, 25.0


def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=30)
    con.execute("CREATE TABLE IF NOT EXISTS journal (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, strategy TEXT, entry_date TEXT, "
                "entry_px REAL, qty INTEGER, stop REAL, target REAL, exit_date TEXT, exit_px REAL, notes TEXT, source TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS imported (trade_id TEXT PRIMARY KEY)")
    return con


def add(symbol, strategy, entry_date, entry_px, qty, stop=None, target=None, notes="", source="manual", exit_date=None, exit_px=None):
    con = connect()
    con.execute("INSERT INTO journal (symbol, strategy, entry_date, entry_px, qty, stop, target, exit_date, exit_px, notes, source) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (symbol.upper().removesuffix(".NS"), strategy, str(entry_date), float(entry_px), int(qty),
                                                   stop or None, target or None, exit_date and str(exit_date), exit_px, notes, source))
    con.commit()
    con.close()


def trades():
    con = connect()
    df = pd.read_sql_query("SELECT * FROM journal ORDER BY entry_date, id", con)
    con.close()
    df = df.rename(columns={"id": "ID", "symbol": "Symbol", "strategy": "Strategy", "entry_date": "Entry", "entry_px": "EntryPx",
                            "qty": "Qty", "stop": "Stop", "target": "Target", "exit_date": "Exit", "exit_px": "ExitPx",
                            "notes": "Notes", "source": "Source"})
    df["Entry"], df["Exit"] = pd.to_datetime(df["Entry"]), pd.to_datetime(df["Exit"])
    df["Status"] = np.where(df["Exit"].isna(), "open", "closed")
    return df


def close(trade_id, exit_date, exit_px, qty=None):
    """Close a position (all of it, or `qty` shares: the rest stays open)."""
    con = connect()
    row = con.execute("SELECT qty FROM journal WHERE id=?", (trade_id,)).fetchone()
    if row:
        if qty and qty < row[0]:
            con.execute("INSERT INTO journal (symbol, strategy, entry_date, entry_px, qty, stop, target, exit_date, exit_px, notes, source) "
                        "SELECT symbol, strategy, entry_date, entry_px, ?, stop, target, ?, ?, notes, source FROM journal WHERE id=?",
                        (int(qty), str(exit_date), float(exit_px), trade_id))
            con.execute("UPDATE journal SET qty=qty-? WHERE id=?", (int(qty), trade_id))
        else:
            con.execute("UPDATE journal SET exit_date=?, exit_px=? WHERE id=?", (str(exit_date), float(exit_px), trade_id))
    con.commit()
    con.close()


def update(trade_id, strategy=None, stop=None, target=None, notes=None):
    con = connect()
    for col, val in (("strategy", strategy), ("stop", stop), ("target", target), ("notes", notes)):
        if val is not None:
            con.execute(f"UPDATE journal SET {col}=? WHERE id=?", (val or None, trade_id))
    con.commit()
    con.close()


def delete(trade_id):
    con = connect()
    con.execute("DELETE FROM journal WHERE id=?", (trade_id,))
    con.commit()
    con.close()


# ------------------------------------------------------------------ broker import
def _num(s):
    return pd.to_numeric(s.astype(str).str.replace(",", "", regex=False), errors="coerce")


def parse_tradebook(text):
    """Fills DataFrame[symbol, when, side, qty, price, trade_id] from a Zerodha tradebook CSV (or any CSV with symbol, date,
    buy/sell, quantity, price columns). Only equity (segment EQ) rows are kept when a segment column exists."""
    df = pd.read_csv(io.StringIO(text.lstrip("\ufeff")))
    df.columns = [c.strip().lower() for c in df.columns]
    pick = lambda *names: next((n for n in names if n in df.columns), None)
    sym, day, side, qty, px = (pick("symbol", "tradingsymbol", "scrip"), pick("trade_date", "date"),
                               pick("trade_type", "side", "type"), pick("quantity", "qty"), pick("price", "rate"))
    if not all((sym, day, side, qty, px)):
        raise ValueError("Could not find symbol, date, buy/sell, quantity and price columns in this CSV.")
    if "segment" in df.columns:
        df = df[df["segment"].astype(str).str.upper() == "EQ"]
    tm = pick("order_execution_time", "time")
    when = pd.to_datetime(df[tm], errors="coerce") if tm else pd.Series(pd.NaT, index=df.index)
    when = when.fillna(pd.to_datetime(df[day], errors="coerce"))
    out = pd.DataFrame({"symbol": df[sym].astype(str).str.upper().str.replace(r"\.NS$", "", regex=True), "when": when,
                        "side": df[side].astype(str).str.upper().str[:1].map({"B": "BUY", "S": "SELL"}),
                        "qty": _num(df[qty]), "price": _num(df[px])})
    tid = pick("trade_id", "id")
    out["trade_id"] = df[tid].astype(str) if tid else (out["symbol"] + "|" + out["when"].astype(str) + "|" + out["side"].astype(str) + "|" +
                                                       out["qty"].astype(str) + "|" + out["price"].astype(str) + "|" +
                                                       out.groupby(["symbol", "when", "side", "qty", "price"]).cumcount().astype(str))
    return out.dropna(subset=["symbol", "when", "side", "qty", "price"]).query("qty > 0 and price > 0")


def import_fills(fills):
    """Apply fills in time order: buys open lots, sells close the oldest open lots first (FIFO). Re-importing is safe."""
    con = connect()
    seen = {r[0] for r in con.execute("SELECT trade_id FROM imported")}
    new = fills[~fills["trade_id"].isin(seen)].drop_duplicates("trade_id").copy()
    new["_o"] = (new["side"] == "SELL").astype(int)  # buys before sells at the same timestamp
    new = new.sort_values(["when", "_o"], kind="stable")
    res = {"fills": len(fills), "new": len(new), "opened": 0, "closed": 0, "unmatched_qty": 0}
    for r in new.itertuples():
        day = r.when.strftime("%Y-%m-%d")
        if r.side == "BUY":
            con.execute("INSERT INTO journal (symbol, strategy, entry_date, entry_px, qty, exit_date, exit_px, notes, source) "
                        "VALUES (?,?,?,?,?,NULL,NULL,'','import')", (r.symbol, UNTAGGED, day, r.price, int(r.qty)))
            res["opened"] += 1
        else:
            left = int(r.qty)
            for lid, lq in con.execute("SELECT id, qty FROM journal WHERE symbol=? AND exit_date IS NULL ORDER BY entry_date, id", (r.symbol,)).fetchall():
                if left <= 0:
                    break
                take = min(lq, left)
                if take == lq:
                    con.execute("UPDATE journal SET exit_date=?, exit_px=? WHERE id=?", (day, r.price, lid))
                else:
                    con.execute("INSERT INTO journal (symbol, strategy, entry_date, entry_px, qty, stop, target, exit_date, exit_px, notes, source) "
                                "SELECT symbol, strategy, entry_date, entry_px, ?, stop, target, ?, ?, notes, source FROM journal WHERE id=?",
                                (take, day, r.price, lid))
                    con.execute("UPDATE journal SET qty=qty-? WHERE id=?", (take, lid))
                left -= take
                res["closed"] += 1
            res["unmatched_qty"] += left
        con.execute("INSERT OR IGNORE INTO imported VALUES (?)", (r.trade_id,))
    con.commit()
    con.close()
    return res


# ------------------------------------------------------------------ analytics
def closed_metrics(df):
    c = df[df["Status"] == "closed"].copy()
    if c.empty:
        return c
    c["Days"] = (c["Exit"] - c["Entry"]).dt.days
    c["Gross"] = (c["ExitPx"] - c["EntryPx"]) * c["Qty"]
    c["Fees"] = [charges(r.EntryPx * r.Qty, r.ExitPx * r.Qty, 0.0) for r in c.itertuples()]
    c["Net"] = c["Gross"] - c["Fees"]
    c["Ret %"] = (c["ExitPx"] / c["EntryPx"] - 1) * 100
    risk = c["EntryPx"] - c["Stop"]
    c["R"] = np.where(risk > 0, (c["ExitPx"] - c["EntryPx"]) / risk, np.nan)
    return c


def perf_table(c):
    def row(name, g):
        wins, losses = g.loc[g["Net"] > 0, "Net"].sum(), -g.loc[g["Net"] <= 0, "Net"].sum()
        return {"Strategy": name, "Trades": len(g), "Win %": (g["Net"] > 0).mean() * 100,
                "Avg win %": g.loc[g["Net"] > 0, "Ret %"].mean(), "Avg loss %": g.loc[g["Net"] <= 0, "Ret %"].mean(),
                "Expectancy %": g["Ret %"].mean(), "Profit factor": wins / losses if losses > 0 else np.inf,
                "Avg days": g["Days"].mean(), "Avg R": g["R"].mean(), "Net P&L (Rs)": g["Net"].sum()}
    return pd.DataFrame([row("ALL", c)] + [row(k, g) for k, g in c.groupby("Strategy")])


def book(open_df, last_px, industry, account):
    """Open positions with value, unrealised P&L, risk to stop and sector; plus totals and warnings."""
    o = open_df.copy()
    o["LTP"] = [last_px.get(s, e) for s, e in zip(o["Symbol"], o["EntryPx"])]
    o["Value (Rs)"] = o["LTP"] * o["Qty"]
    o["Unreal P&L (Rs)"] = (o["LTP"] - o["EntryPx"]) * o["Qty"]
    o["Unreal %"] = (o["LTP"] / o["EntryPx"] - 1) * 100
    o["Risk now (Rs)"] = np.where(o["Stop"].notna(), (o["LTP"] - o["Stop"]).clip(lower=0) * o["Qty"], np.nan)
    o["Industry"] = o["Symbol"].map(lambda s: industry.get(s, "Unknown"))
    tot = {"value": o["Value (Rs)"].sum(), "unreal": o["Unreal P&L (Rs)"].sum(), "risk": o["Risk now (Rs)"].sum(skipna=True),
           "n": len(o), "no_stop": int(o["Stop"].isna().sum())}
    warn = []
    if tot["no_stop"]:
        warn.append(f"{tot['no_stop']} open position(s) have no stop, so their risk is unknown.")
    if account and tot["risk"] / account * 100 > RISK_LIMIT_PCT:
        warn.append(f"Open risk Rs {tot['risk']:,.0f} is {tot['risk'] / account * 100:.1f}% of the account (above {RISK_LIMIT_PCT:.0f}%).")
    sect = o.groupby("Industry")["Value (Rs)"].sum()
    for k, v in sect.items():
        if tot["value"] > 0 and v / tot["value"] * 100 > SECTOR_LIMIT_PCT and len(o) > 2:
            warn.append(f"{k} is {v / tot['value'] * 100:.0f}% of open positions (above {SECTOR_LIMIT_PCT:.0f}%).")
    for sym, val in zip(o["Symbol"], o["Value (Rs)"]):
        if account and val / account * 100 > POSITION_LIMIT_PCT:
            warn.append(f"{sym} is {val / account * 100:.0f}% of the account (above {POSITION_LIMIT_PCT:.0f}%).")
    return o, tot, sect, warn


# ------------------------------------------------------------------ UI
@st.cache_data(ttl=120, show_spinner="Getting latest prices...")
def last_prices(symbols: tuple):
    try:
        data, fetched = download(list(symbols))
        return {t[:-3]: float(d["Close"].iloc[-1]) for t, d in data.items() if t != "^NSEI"}, fetched, None
    except Exception as e:
        return {}, None, str(e)


@st.cache_data(ttl=86400)
def broad_industry_map():
    df, _ = universe.get_list("Nifty 500")
    if df.empty or not df["Industry"].astype(bool).any():
        raise RuntimeError("industry list unavailable")  # raising keeps a failure out of the cache
    return dict(zip(df["Symbol"], df["Industry"]))


def render_journal(symbols, industry):
    st.title("Journal and risk")
    st.caption("Your real trades and the risk in your open book. Import your broker tradebook or log trades by hand, tag each "
               "with the strategy it came from, and see what is actually working.")
    df = trades()
    opn = df[df["Status"] == "open"]
    try:
        base = broad_industry_map()
    except Exception:
        base = {}
    ind = {**base, **{k: v for k, v in industry.items() if v}}
    if st.session_state.get("jr_flash"):
        st.success(st.session_state.pop("jr_flash"))
    account = st.number_input("Account size (Rs)", 10_000, 1_000_000_000, 1_000_000, 50_000, key="acct_rs", help=T["Account size"])

    st.subheader(f"Open book ({len(opn)})")
    if opn.empty:
        st.info("No open positions yet. Log a trade or import your tradebook below.")
    else:
        px, fetched, err = last_prices(tuple(sorted(set(opn["Symbol"]))))
        if err:
            st.warning(f"Could not refresh prices ({err}). Showing entry prices.")
        o, tot, sect, warn = book(opn, px, ind, account)
        a, b, c, d = st.columns(4)
        a.metric("Market value", f"Rs {tot['value']:,.0f}")
        b.metric("Unrealised P&L", f"Rs {tot['unreal']:,.0f}", f"{tot['unreal'] / max(o['EntryPx'].mul(o['Qty']).sum(), 1) * 100:+.1f}%")
        c.metric("Open risk to stops", f"Rs {tot['risk']:,.0f}", f"{tot['risk'] / account * 100:.1f}% of account", delta_color="off")
        d.metric("Positions", tot["n"])
        for w in warn:
            st.warning(w)
        if st.button("Check results / ex-dividend dates", help=T["Events"]):
            st.session_state["book_events"] = events.upcoming(sorted(set(opn["Symbol"])), days=14)
        ev = st.session_state.get("book_events", {})
        ban, ban_ok = events.fo_ban() if "book_events" in st.session_state else (set(), False)
        o["Events"] = o["Symbol"].map(lambda s: "; ".join(x for x in (events.flag_text(ev.get(s, [])), "F&O ban" if s in ban else "") if x))
        table(o.rename(columns={"EntryPx": "Entry px", "Strategy": "Strategy"})[
            ["ID", "Symbol", "Strategy", "Qty", "Entry px", "LTP", "Unreal P&L (Rs)", "Unreal %", "Stop", "Risk now (Rs)", "Industry", "Events"]])
        if "book_events" in st.session_state:
            st.caption("Event dates come from Yahoo and are often incomplete for Indian stocks. Confirm on the company's "
                       "announcements / NSE board meetings. " + ("" if ban_ok else "F&O ban list unavailable."))
        fig = go.Figure(go.Bar(x=sect.index, y=sect.values / max(tot["value"], 1) * 100))
        fig.update_layout(title="Sector exposure (% of open value)", height=280, margin=dict(l=0, r=0, t=40, b=0))
        st.plotly_chart(fig, use_container_width=True)

    tags = [UNTAGGED, DISCRETIONARY] + list(STRATS)
    with st.expander("Log a trade", expanded=df.empty):
        a, b, c, d = st.columns(4)
        sym = a.text_input("Symbol (NSE, no .NS)", key="jr_sym").strip().upper()
        tag = b.selectbox("Strategy", tags, key="jr_tag")
        qty = c.number_input("Quantity", 1, 10_000_000, 10, key="jr_qty")
        epx = d.number_input("Entry price", 0.0, step=0.5, key="jr_epx")
        e, f, g, h = st.columns(4)
        edt = e.date_input("Entry date", date.today(), max_value=date.today(), key="jr_edt")
        stp = f.number_input("Stop price (0 = none)", 0.0, step=0.5, key="jr_stp")
        tgt = g.number_input("Target price (0 = none)", 0.0, step=0.5, key="jr_tgt")
        note = h.text_input("Notes", key="jr_note")
        if st.button("Add trade", type="primary"):
            if not sym or epx <= 0:
                st.error("Enter a symbol and an entry price.")
            elif stp and stp >= epx:
                st.error("The stop must be below the entry price for a long trade.")
            elif tgt and tgt <= epx:
                st.error("The target must be above the entry price.")
            else:
                add(sym, tag, edt, epx, qty, stp or None, tgt or None, note)
                st.session_state["jr_flash"] = f"Added {sym}."
                st.rerun()
    if len(df):
        label = lambda i: f"#{i} {df.loc[df['ID'] == i, 'Symbol'].iloc[0]} ({df.loc[df['ID'] == i, 'Status'].iloc[0]})"
        with st.expander("Close, tag or delete a trade"):
            tid = st.selectbox("Trade", df["ID"].astype(int).tolist(), format_func=label, key="jr_pick")
            row = df[df["ID"] == tid].iloc[0]
            a, b, c = st.columns(3)
            xpx = a.number_input("Exit price", 0.0, step=0.5, key="jr_xpx")
            xqty = b.number_input("Shares to close (0 = all)", 0, 10_000_000, 0, key="jr_xqty")
            xdt = c.date_input("Exit date", date.today(), max_value=date.today(), key="jr_xdt")
            if st.button("Close position", disabled=row["Status"] == "closed" or xpx <= 0):
                close(tid, xdt, xpx, xqty or None)
                st.session_state["jr_flash"] = f"Closed {row['Symbol']}."
                st.rerun()
            a, b, c = st.columns(3)
            ntag = a.selectbox("Strategy tag", tags, index=tags.index(row["Strategy"]) if row["Strategy"] in tags else 0, key="jr_ntag")
            nstp = b.number_input("Stop", 0.0, step=0.5, value=float(row["Stop"]) if pd.notna(row["Stop"]) else 0.0, key=f"jr_nstp{tid}")
            nnote = c.text_input("Notes", value=str(row["Notes"] or ""), key=f"jr_nnote{tid}")
            if st.button("Save tag / stop / notes"):
                update(tid, strategy=ntag, stop=nstp, notes=nnote)
                st.session_state["jr_flash"] = "Saved."
                st.rerun()
            if st.button("Delete this trade"):
                delete(tid)
                st.rerun()
    with st.expander("Import broker tradebook (Zerodha CSV)"):
        st.caption("Console > Reports > Tradebook > download CSV. Equity rows only. Buys open positions and sells close the oldest "
                   "ones first (FIFO). Importing the same file twice is safe. Tag the strategy afterwards.")
        up = st.file_uploader("Tradebook CSV", type="csv", key="jr_up")
        if up is not None and st.button("Import file"):
            try:
                r = import_fills(parse_tradebook(up.getvalue().decode("utf-8", "ignore")))
                st.success(f"{r['new']} new fills of {r['fills']}: {r['opened']} buys, {r['closed']} lot closures."
                           + (f" {r['unmatched_qty']} shares sold had no matching buy in the journal (bought before this file?)." if r["unmatched_qty"] else ""))
            except Exception as e:
                st.error(f"Import failed: {e}")

    st.subheader("Performance of closed trades")
    cm = closed_metrics(trades())
    if cm.empty:
        st.info("No closed trades yet.")
        return
    table(perf_table(cm))
    st.caption("Net P&L subtracts approximate Zerodha delivery charges (no slippage, since these are real fills). "
               "Avg R needs a stop on the trade. Compare strategies here with their backtest numbers on the Today tab.")
    s = cm.sort_values("Exit")
    fig = go.Figure(go.Scatter(x=s["Exit"], y=s["Net"].cumsum(), mode="lines+markers", name="Cumulative net P&L"))
    fig.update_layout(title="Cumulative net P&L (Rs)", height=300, margin=dict(l=0, r=0, t=40, b=0))
    st.plotly_chart(fig, use_container_width=True)
    out = cm.assign(Entry=cm["Entry"].dt.strftime("%Y-%m-%d"), Exit=cm["Exit"].dt.strftime("%Y-%m-%d")).rename(
        columns={"EntryPx": "Entry px", "ExitPx": "Exit px", "Net": "Net P&L (Rs)"})
    st.download_button("Download closed trades CSV", out.to_csv(index=False), "journal_closed.csv", "text/csv")
    table(out[["ID", "Symbol", "Strategy", "Entry", "Exit", "Qty", "Entry px", "Exit px", "Ret %", "Net P&L (Rs)", "R", "Days"]].tail(100))
