"""Zerodha (Kite Connect) integration with safety rails, using only the standard library (no compiled packages).
Reading trades, holdings and positions works from any IP. Placing orders only works from the static IP registered in
the Kite developer console. Everything defaults to DRY RUN: nothing reaches Zerodha until you switch live orders on.
Your own account only. Under NSE's retail algo framework you accept responsibility for your algo's logic and losses."""
import arrow_guard  # noqa: F401  (must run before pandas)
import hashlib
import json
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

import auth
import journal
from backtest import table
from core import IST, session_frac
from glossary import T

HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("NSE_SCREENER_DATA") or HERE / "data")
ROOT, LOGIN = "https://api.kite.trade", "https://kite.zerodha.com/connect/login"
DEFAULTS = {"live": False, "max_order_value": 20000, "max_orders_per_day": 5, "price_band_pct": 3.0, "stop_limit_buffer_pct": 0.5}


# ------------------------------------------------------------------ settings, kill switch, audit
def _env():
    env, f = {}, HERE / ".env"
    if f.exists():
        for ln in f.read_text(encoding="utf-8").splitlines():
            if "=" in ln and not ln.lstrip().startswith("#"):
                k, v = ln.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    env.update({k: v for k, v in os.environ.items() if k.startswith("KITE_")})
    return env


def settings():
    f = DATA / "broker_settings.json"
    return {**DEFAULTS, **(json.loads(f.read_text()) if f.exists() else {})}


def save_settings(s):
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "broker_settings.json").write_text(json.dumps(s))


def kill_active():
    return (DATA / "KILL").exists()


def set_kill(on):
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "KILL").write_text("stop") if on else (DATA / "KILL").unlink(missing_ok=True)


def audit(event, **fields):
    """Append-only log of every order action (never contains secrets)."""
    DATA.mkdir(parents=True, exist_ok=True)
    with open(DATA / "audit.log", "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": datetime.now(IST).isoformat(timespec="seconds"), "event": event, **fields}, default=str) + "\n")


# ------------------------------------------------------------------ minimal Kite REST client
class KiteError(Exception):
    def __init__(self, message, error_type="", status=0):
        super().__init__(message)
        self.error_type, self.status = error_type, status


class Kite:
    def __init__(self, api_key, access_token=None, timeout=15):
        self.api_key, self.access_token, self.timeout = api_key, access_token, timeout

    def _call(self, method, route, params=None):
        headers, url, body = {"X-Kite-Version": "3", "User-Agent": "nse-swing-screener"}, ROOT + route, None
        if self.access_token:
            headers["Authorization"] = f"token {self.api_key}:{self.access_token}"
        if method in ("GET", "DELETE"):
            url += ("?" + urllib.parse.urlencode(params)) if params else ""
        else:
            body = urllib.parse.urlencode(params or {}).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers, method=method), timeout=self.timeout) as r:
                raw, status = r.read(), getattr(r, "status", 200)
        except urllib.error.HTTPError as e:
            raw, status = e.read(), e.code
        try:
            out = json.loads(raw)
        except ValueError:
            raise KiteError(f"Unexpected reply from Zerodha (HTTP {status}).", "DataException", status)
        if out.get("status") == "error" or out.get("error_type"):
            raise KiteError(out.get("message", "error"), out.get("error_type", ""), status)
        return out["data"]

    def profile(self): return self._call("GET", "/user/profile")
    def holdings(self): return self._call("GET", "/portfolio/holdings")
    def positions(self): return self._call("GET", "/portfolio/positions")
    def trades(self): return self._call("GET", "/trades")
    def orders(self): return self._call("GET", "/orders")
    def gtts(self): return self._call("GET", "/gtt/triggers")

    def place_order(self, variety, symbol, side, qty, order_type, price, tag, algo_id=None):
        p = {"variety": variety, "exchange": "NSE", "tradingsymbol": symbol, "transaction_type": side, "quantity": int(qty), "product": "CNC",
             "order_type": order_type, "price": price, "validity": "DAY", "algo_id": algo_id, "tag": tag}
        return self._call("POST", f"/orders/{variety}", {k: v for k, v in p.items() if v is not None})["order_id"]

    def place_oco(self, symbol, qty, stop, target, last_price, stop_limit):
        orders = [{"exchange": "NSE", "tradingsymbol": symbol, "transaction_type": "SELL", "quantity": int(qty), "order_type": "LIMIT",
                   "product": "CNC", "price": float(stop_limit)},
                  {"exchange": "NSE", "tradingsymbol": symbol, "transaction_type": "SELL", "quantity": int(qty), "order_type": "LIMIT",
                   "product": "CNC", "price": float(target)}]
        cond = {"exchange": "NSE", "tradingsymbol": symbol, "trigger_values": [stop, target], "last_price": last_price}
        return self._call("POST", "/gtt/triggers", {"condition": json.dumps(cond), "orders": json.dumps(orders), "type": "two-leg"})


def login_url(api_key):
    return f"{LOGIN}?v=3&api_key={urllib.parse.quote(api_key)}"


def create_session(api_key, api_secret, request_token):
    checksum = hashlib.sha256((api_key + request_token + api_secret).encode()).hexdigest()
    d = Kite(api_key)._call("POST", "/session/token", {"api_key": api_key, "request_token": request_token, "checksum": checksum})
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "kite_session.json").write_text(json.dumps({"access_token": d["access_token"], "user": d.get("user_name", ""),
                                                        "saved": datetime.now(IST).isoformat()}))
    return d


def session_valid(saved_iso, now=None):
    """Kite tokens expire around 06:00 IST the next morning."""
    now, saved = now or datetime.now(IST), datetime.fromisoformat(saved_iso)
    expiry = saved.replace(hour=6, minute=0, second=0, microsecond=0)
    expiry = expiry if expiry > saved else expiry + timedelta(days=1)
    return now < expiry


def current_kite():
    """(Kite client or None, status message)."""
    env = _env()
    if not env.get("KITE_API_KEY") or not env.get("KITE_API_SECRET"):
        return None, "Add KITE_API_KEY and KITE_API_SECRET to your .env file."
    f = DATA / "kite_session.json"
    if not f.exists():
        return None, "Not logged in today."
    s = json.loads(f.read_text())
    if not session_valid(s["saved"]):
        return None, "Yesterday's login has expired. Log in again."
    return Kite(env["KITE_API_KEY"], s["access_token"]), f"Logged in as {s.get('user') or 'your Zerodha account'}."


# ------------------------------------------------------------------ order ledger and guards
def _db():
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DATA / "broker.db", timeout=30)
    con.execute("CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, day TEXT, symbol TEXT, side TEXT, "
                "qty INTEGER, price REAL, tag TEXT, mode TEXT, kind TEXT, status TEXT, broker_id TEXT, message TEXT)")
    return con


def ledger(day=None):
    con = _db()
    q, args = "SELECT * FROM orders" + (" WHERE day=?" if day else "") + " ORDER BY id DESC", ((day,) if day else ())
    df = pd.read_sql_query(q, con, params=args)
    con.close()
    return df


def _record(**r):
    con = _db()
    con.execute("INSERT INTO orders (ts, day, symbol, side, qty, price, tag, mode, kind, status, broker_id, message) VALUES "
                "(:ts,:day,:symbol,:side,:qty,:price,:tag,:mode,:kind,:status,:broker_id,:message)",
                {"ts": datetime.now(IST).isoformat(timespec="seconds"), "broker_id": "", "message": "", **r})
    con.commit()
    con.close()


def tick(x):
    return round(round(x / 0.05) * 0.05, 2)


def check_order(symbol, qty, price, ref_price, s, today):
    """Reasons the order must not be sent (empty list = all guards passed)."""
    bad, mode = [], ("live" if s["live"] else "dry")
    if kill_active():
        bad.append("The kill switch is on.")
    if int(qty) < 1 or price <= 0 or ref_price <= 0:
        bad.append("Quantity and prices must be positive.")
        return bad
    if qty * price > s["max_order_value"]:
        bad.append(f"Order value Rs {qty * price:,.0f} is above your cap of Rs {s['max_order_value']:,.0f}.")
    if abs(price / ref_price - 1) * 100 > s["price_band_pct"]:
        bad.append(f"Limit price is more than {s['price_band_pct']:.1f}% away from the last close ({ref_price:.2f}).")
    day = ledger(today)
    day = day[(day["mode"] == mode) & (day["kind"] == "entry") & day["status"].isin(["sent", "error", "dry-run", "OPEN", "COMPLETE"])]
    if len(day) >= s["max_orders_per_day"]:
        bad.append(f"Daily order limit reached ({s['max_orders_per_day']}).")
    if (day["symbol"] == symbol).any():
        bad.append(f"An order for {symbol} was already placed today. Use 'Check order status' before retrying.")
    return bad


def place_entry(kite, symbol, qty, price, ref_price, s=None, today=None, algo_id=None):
    """Guarded LIMIT BUY (NSE, CNC). Dry run records it without contacting Zerodha."""
    s, today = s or settings(), today or date.today().isoformat()
    mode = "live" if s["live"] else "dry"
    bad = check_order(symbol, qty, price, ref_price, s, today)
    if bad:
        audit("entry_refused", symbol=symbol, qty=qty, price=price, reasons=bad)
        return {"ok": False, "problems": bad}
    tag = re.sub(r"[^A-Za-z0-9]", "", f"S{today[2:]}{symbol}")[:20]
    base = dict(day=today, symbol=symbol, side="BUY", qty=int(qty), price=price, tag=tag, mode=mode, kind="entry")
    if mode == "dry":
        _record(**base, status="dry-run", message="Dry run: nothing was sent to Zerodha.")
        audit("entry_dry_run", **base)
        return {"ok": True, "dry": True, "tag": tag}
    variety = "regular" if session_frac() is not None else "amo"
    try:
        oid = kite.place_order(variety, symbol, "BUY", qty, "LIMIT", price, tag, algo_id)
    except Exception as e:
        hint = " Zerodha only accepts orders from the static IP registered in the developer console." if "ip" in str(e).lower() else ""
        _record(**base, status="error", message=str(e))
        audit("entry_error", **base, error=str(e))
        return {"ok": False, "problems": [f"Zerodha did not accept the order: {e}.{hint} Check the order book before retrying."]}
    _record(**base, status="sent", broker_id=str(oid), message=f"{variety} order sent")
    audit("entry_sent", **base, broker_id=oid, variety=variety)
    return {"ok": True, "dry": False, "order_id": oid, "variety": variety, "tag": tag}


def place_protection(kite, symbol, qty, stop, target, last_price, s=None, today=None):
    """Guarded GTT OCO sell (stop-loss leg + target leg) for shares you already hold."""
    s, today = s or settings(), today or date.today().isoformat()
    if kill_active():
        return {"ok": False, "problems": ["The kill switch is on."]}
    if not (0 < stop < last_price < target):
        return {"ok": False, "problems": [f"Need stop < last price < target (got {stop}, {last_price}, {target})."]}
    if int(qty) < 1:
        return {"ok": False, "problems": ["Quantity must be at least 1."]}
    limit = tick(stop * (1 - s["stop_limit_buffer_pct"] / 100))
    base = dict(day=today, symbol=symbol, side="SELL", qty=int(qty), price=stop, tag="oco", mode="live" if s["live"] else "dry", kind="oco")
    if not s["live"]:
        _record(**base, status="dry-run", message=f"Dry run: OCO stop {stop} / target {target}")
        audit("oco_dry_run", **base, target=target)
        return {"ok": True, "dry": True}
    try:
        res = kite.place_oco(symbol, qty, tick(stop), tick(target), last_price, limit)
    except Exception as e:
        _record(**base, status="error", message=str(e))
        audit("oco_error", **base, error=str(e))
        return {"ok": False, "problems": [f"Zerodha did not accept the GTT: {e}"]}
    _record(**base, status="sent", broker_id=str(res.get("trigger_id", "")), message=f"OCO target {target}")
    audit("oco_sent", **base, target=target, result=res)
    return {"ok": True, "dry": False, "trigger_id": res.get("trigger_id")}


def reconcile(kite, today=None):
    """Update today's live ledger rows from Zerodha's order book (matched on the order tag)."""
    today = today or date.today().isoformat()
    book = {t: o for o in kite.orders() for t in ({o.get("tag")} | set(o.get("tags") or [])) if t}
    con, n = _db(), 0
    for rid, tag, status in con.execute("SELECT id, tag, status FROM orders WHERE day=? AND mode='live' AND kind='entry'", (today,)).fetchall():
        o = book.get(tag)
        new = o["status"] if o else ("not-found" if status in ("sent", "error") else status)
        if new != status:
            con.execute("UPDATE orders SET status=?, broker_id=COALESCE(NULLIF(?, ''), broker_id) WHERE id=?", (new, str(o["order_id"]) if o else "", rid))
            n += 1
    con.commit()
    con.close()
    audit("reconcile", updated=n)
    return n


# ------------------------------------------------------------------ syncing with the journal
def fills_from_trades(trades):
    rows = [dict(symbol=t["tradingsymbol"], when=pd.to_datetime(t["fill_timestamp"]), side=t["transaction_type"].upper(),
                 qty=float(t["quantity"]), price=float(t["average_price"]), trade_id=str(t["trade_id"]))
            for t in trades if t.get("exchange") == "NSE" and t.get("product") == "CNC"]
    return pd.DataFrame(rows, columns=["symbol", "when", "side", "qty", "price", "trade_id"])


def holdings_diff(holdings, open_df):
    """Compare broker holdings with the journal's open quantity per symbol."""
    broker = {h["tradingsymbol"]: (int(h.get("quantity", 0)) + int(h.get("t1_quantity", 0)), float(h.get("average_price", 0))) for h in holdings}
    jr = open_df.groupby("Symbol")["Qty"].sum().astype(int).to_dict() if len(open_df) else {}
    rows = [{"Symbol": s, "Broker qty": broker.get(s, (0, 0))[0], "Journal qty": jr.get(s, 0), "Broker avg price": broker.get(s, (0, 0))[1]}
            for s in sorted(set(broker) | set(jr))]
    df = pd.DataFrame(rows, columns=["Symbol", "Broker qty", "Journal qty", "Broker avg price"])
    df["Difference"] = df["Broker qty"] - df["Journal qty"]
    return df[df["Difference"] != 0].reset_index(drop=True)


def adopt_missing(diff):
    n = 0
    for sym, avg, extra in zip(diff["Symbol"], diff["Broker avg price"], diff["Difference"]):
        if extra > 0:
            journal.add(sym, journal.UNTAGGED, date.today(), avg, int(extra), notes="from broker holdings; real entry date unknown", source="broker")
            n += 1
    return n


# ------------------------------------------------------------------ UI
def render_broker(symbols):
    st.title("Broker (Zerodha)")
    st.caption("Sync your real trades and place reviewed orders. Reading works from any computer; placing orders only works from the "
               "static IP you register with Zerodha. Everything is DRY RUN until you switch live orders on below.")
    s = settings()
    kite, msg = current_kite()
    (st.success if kite else st.info)(msg)
    if kill_active():
        st.error("KILL SWITCH IS ON: all order actions are blocked.")
    if s["live"] and not auth.configured():
        st.warning("Live orders are on but no APP_PASSWORD is set. If this app is reachable by anyone else, set one in .env first.")
    env = _env()
    with st.expander("1. Connect (once a day)", expanded=kite is None):
        if not env.get("KITE_API_KEY"):
            st.markdown("Create an app at developers.kite.trade (set any redirect URL, e.g. `http://127.0.0.1`), then put `KITE_API_KEY` and "
                        "`KITE_API_SECRET` in the `.env` file next to the app and reload.")
        else:
            st.markdown(f"[Step 1: log in to Zerodha]({login_url(env['KITE_API_KEY'])}). After login you are redirected to your redirect URL; "
                        "copy the `request_token=...` value from the address bar.")
            rt = st.text_input("Step 2: paste the request_token", key="kite_rt", type="password")
            if st.button("Create session") and rt.strip():
                try:
                    create_session(env["KITE_API_KEY"], env.get("KITE_API_SECRET", ""), rt.strip())
                    st.session_state["kite_rt"] = ""
                    st.rerun()
                except Exception as e:
                    st.error(f"Login failed: {e}")
    with st.expander("2. Safety settings", expanded=False):
        a, b, c = st.columns(3)
        s["max_order_value"] = a.number_input("Max value per order (Rs)", 1000, 1_000_000, int(s["max_order_value"]), 1000, help=T["Order caps"])
        s["max_orders_per_day"] = b.number_input("Max entry orders per day", 1, 50, int(s["max_orders_per_day"]), help=T["Order caps"])
        s["price_band_pct"] = c.number_input("Max limit-price distance from last close (%)", 0.5, 20.0, float(s["price_band_pct"]), 0.5, help=T["Order caps"])
        live = st.checkbox("Send REAL orders to Zerodha (off = dry run)", s["live"], help=T["Dry run"])
        ok = st.checkbox("I understand real orders use my live account and I am responsible for them", disabled=not live)
        s["live"] = bool(live and ok)
        kill = st.checkbox("Kill switch (block every order action)", kill_active())
        if st.button("Save safety settings"):
            save_settings(s)
            set_kill(kill)
            audit("settings_saved", live=s["live"], kill=kill, caps={k: s[k] for k in ("max_order_value", "max_orders_per_day", "price_band_pct")})
            st.rerun()
        st.caption("Mode now: " + ("LIVE ORDERS" if settings()["live"] else "DRY RUN") + ". Orders are LIMIT, NSE equity, CNC (delivery) only.")

    if kite:
        st.subheader("Sync (read-only)")
        c1, c2 = st.columns(2)
        if c1.button("Import today's trades into Journal", help="Pulls today's executed trades. Run it every trading day; repeats are ignored."):
            try:
                f = fills_from_trades(kite.trades())
                r = journal.import_fills(f) if len(f) else {"new": 0, "opened": 0, "closed": 0, "unmatched_qty": 0}
                st.success(f"{r['new']} new fills: {r['opened']} buys, {r['closed']} lot closures.")
            except Exception as e:
                st.error(f"Sync failed: {e}")
        if c2.button("Compare holdings with Journal"):
            try:
                st.session_state["hold_diff"] = holdings_diff(kite.holdings(), journal.trades().query("Status == 'open'"))
            except Exception as e:
                st.error(f"Could not read holdings: {e}")
        diff = st.session_state.get("hold_diff")
        if diff is not None:
            if diff.empty:
                st.success("Holdings and Journal match.")
            else:
                table(diff)
                if st.button("Add shares missing from the Journal"):
                    st.success(f"Added {adopt_missing(diff)} position(s) (entry date unknown, avg price from Zerodha).")
                    st.session_state.pop("hold_diff")

    st.subheader("Order ticket")
    tickets = st.session_state.get("tickets", [])
    labels = ["Manual entry"] + [f"{t['Symbol']} | {t['Strategy']} | qty {t['Qty']}" for t in tickets]
    pick = st.selectbox("Start from a setup on the Today tab", range(len(labels)), format_func=lambda i: labels[i], key="tk_pick")
    t = tickets[pick - 1] if pick else {}
    a, b, c, d = st.columns(4)
    sym = a.text_input("Symbol", t.get("Symbol", ""), key=f"tk_sym{pick}").strip().upper()
    qty = b.number_input("Quantity", 0, 1_000_000, int(t.get("Qty", 0)), key=f"tk_qty{pick}")
    px = c.number_input("Limit price", 0.0, step=0.05, value=float(round(t.get("Buy near", 0.0), 2)), key=f"tk_px{pick}")
    ref = d.number_input("Last close (reference)", 0.0, step=0.05, value=float(round(t.get("Buy near", 0.0), 2)), key=f"tk_ref{pick}")
    stp = st.number_input("Stop (for protection after fill)", 0.0, step=0.05, value=float(round(t.get("Stop", 0.0), 2)), key=f"tk_stp{pick}")
    tgt = st.number_input("Target (for protection after fill)", 0.0, step=0.05,
                          value=float(round(t["Target"] if t.get("Target") == t.get("Target") and t.get("Target") else (t.get("Buy near", 0) + 2 * t.get("Risk/sh", 0)), 2)),
                          key=f"tk_tgt{pick}")
    if sym and qty and px > 0:
        st.info(f"Ticket: BUY {qty} x {sym} LIMIT {px:.2f} (value Rs {qty * px:,.0f}), NSE, CNC, "
                f"{'REGULAR' if session_frac() is not None else 'AMO (after market)'} order. Mode: {'LIVE' if settings()['live'] else 'DRY RUN'}.")
        for p in check_order(sym, qty, px, ref or px, settings(), date.today().isoformat()):
            st.warning(p)
        typed = st.text_input(f"Type {sym} to confirm", key=f"tk_conf{pick}").strip().upper()
        if st.button("Send BUY order", type="primary", disabled=typed != sym or (kite is None and settings()["live"])):
            r = place_entry(kite, sym, qty, px, ref or px, settings())
            (st.success if r["ok"] else st.error)(("Dry run recorded." if r.get("dry") else f"Order sent (id {r.get('order_id')}).") if r["ok"] else " ".join(r["problems"]))
        if kite is None and settings()["live"]:
            st.caption("Log in first (section 1) to send live orders.")
    with st.expander("Protect a position you hold: stop-loss and target together (GTT OCO)"):
        st.caption("Place this after your buy is filled. Needs stop < last price < target. Zerodha sends the sell when either trigger hits and cancels the other.")
        ltp = st.number_input("Last price now", 0.0, step=0.05, value=float(ref or px or 0.0), key=f"oco_ltp{pick}")
        if st.button("Place stop + target (GTT OCO)"):
            if kite is None and settings()["live"]:
                st.error("Log in first.")
            else:
                r = place_protection(kite, sym, qty, stp, tgt, ltp, settings())
                (st.success if r["ok"] else st.error)(("Dry run recorded." if r.get("dry") else f"GTT placed (id {r.get('trigger_id')}).") if r["ok"] else " ".join(r["problems"]))

    st.subheader("Orders placed by this app")
    if kite and st.button("Check order status", help=T["Reconcile"]):
        try:
            st.info(f"{reconcile(kite)} order status(es) updated from Zerodha.")
        except Exception as e:
            st.error(f"Could not read the order book: {e}")
    lg = ledger()
    if lg.empty:
        st.info("No orders yet.")
    else:
        table(lg[["ts", "symbol", "side", "qty", "price", "kind", "mode", "status", "message"]].head(50).rename(columns={"ts": "Time", "symbol": "Symbol", "side": "Side", "qty": "Qty", "price": "Price", "kind": "Kind", "mode": "Mode", "status": "Status", "message": "Note"}))
        st.caption(f"Full audit trail: {DATA / 'audit.log'}")
    with st.expander("How to get a static IP (required for placing orders)"):
        st.markdown(
            "- **What Zerodha checks:** orders sent through the API must come from an IP you registered; reading trades, holdings and positions "
            "works from anywhere.\n- **Where to register:** Kite developer console > your profile > IP whitelist. A primary IP is mandatory, "
            "a secondary is optional, and changes are limited to once a week, so enter it carefully.\n- **Where to get one:** a cloud server "
            "(AWS Mumbai, Google Cloud, DigitalOcean Bangalore, Oracle Cloud...) with a fixed IP, a static IP plan from your broadband provider, or a "
            "VPN/VPC service with a dedicated IP. Home connections usually have a changing IP.\n- **Easiest setup:** run this app on a small cloud server "
            "(see deploy_server.sh) and open it through an SSH tunnel.\n- **Declaration:** the IP must be used only by you or your immediate family.")
