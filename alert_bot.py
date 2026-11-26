"""Phone alerts for the NSE screener. Runs in a terminal, no Streamlit needed.

Setup: put credentials in a .env file next to this script (never commit it):
    TELEGRAM_BOT_TOKEN=123456:ABC-your-token      # from @BotFather
    TELEGRAM_CHAT_ID=123456789                    # see --find-chat-id
    NTFY_TOPIC=a-long-random-string               # optional alternative: free ntfy app, no bot needed
    RISK_PER_TRADE=1000                           # Rs you risk per trade; sizes the quantity in strategy alerts

Commands:
    python alert_bot.py --find-chat-id   # after you have sent your bot a message
    python alert_bot.py --test           # send one test message
    python alert_bot.py --once           # scan now and send a full summary
    python alert_bot.py                  # run: new-match alerts every N minutes 09:30-15:30 IST,
                                         # then a closing summary at 15:50 IST, then exit
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, time as dtime
from pathlib import Path

from core import (IST, MIDCAPS, NIFTY50, PRESETS, apply_filters, build_df, download, resolve, signals, strategy_hits,
                  trade_plan)
from strategies import STRATS  # noqa: F401

HERE = Path(__file__).resolve().parent
STATE = HERE / "alert_state.json"
OPEN, CLOSE, EOD, STOP = dtime(9, 30), dtime(15, 30), dtime(15, 50), dtime(16, 30)


def read_env():
    env = {}
    f = HERE / ".env"
    if f.exists():
        for ln in f.read_text(encoding="utf-8").splitlines():
            if "=" in ln and not ln.lstrip().startswith("#"):
                k, v = ln.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    env.update({k: v for k, v in os.environ.items() if k.startswith(("TELEGRAM_", "NTFY_"))})
    return env


ENV = read_env()
try:
    RISK = float(ENV.get("RISK_PER_TRADE", "1000"))  # Rs you risk per trade; sizes the quantity in alerts
except ValueError:
    RISK = 1000.0


# ------------------------------------------------------------------ sending
def _post(url, data, headers=None):
    try:
        req = urllib.request.Request(url, data=data, headers=headers or {})
        with urllib.request.urlopen(req, timeout=20) as r:
            return 200 <= r.status < 300
    except Exception as e:  # never print the URL: it contains the bot token
        print(f"  send failed: {type(e).__name__}: {e}")
        return False


def notify(text):
    sent = False
    tok, chat, topic = ENV.get("TELEGRAM_BOT_TOKEN"), ENV.get("TELEGRAM_CHAT_ID"), ENV.get("NTFY_TOPIC")
    if tok and chat:
        body = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000], "disable_web_page_preview": "true"})
        sent |= _post(f"https://api.telegram.org/bot{tok}/sendMessage", body.encode())
    if topic:
        sent |= _post(f"https://ntfy.sh/{topic}", text.encode("utf-8"), {"Title": "NSE screener"})
    if not (tok and chat) and not topic:
        print("(no TELEGRAM_* / NTFY_TOPIC configured - printing instead)")
    if not sent:
        print(text)
    return sent


def find_chat_id():
    tok = ENV.get("TELEGRAM_BOT_TOKEN")
    if not tok:
        sys.exit("Put TELEGRAM_BOT_TOKEN in .env first.")
    with urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/getUpdates", timeout=20) as r:
        updates = json.load(r).get("result", [])
    chats = {}
    for u in updates:
        c = (u.get("message") or u.get("channel_post") or {}).get("chat")
        if c:
            chats[c["id"]] = c.get("first_name") or c.get("title") or ""
    if not chats:
        sys.exit("No messages found. Open your bot in Telegram, press Start, send 'hi', then run this again.")
    for cid, name in chats.items():
        print(f"TELEGRAM_CHAT_ID={cid}   ({name})")


# ------------------------------------------------------------------ scanning + formatting
def scan(symbols):
    data, _ = download(symbols)
    if "^NSEI" not in data:
        raise RuntimeError("no Nifty data downloaded")
    df, _ = build_df(data, True)
    if df.empty:
        raise RuntimeError("no stock data downloaded")
    return df, data["^NSEI"].index[-1].date(), data


def hits_for(name, df, data):
    """Screener preset (filters) or one of the 7 backtest strategies (exact backtest rules)."""
    kind, key = resolve(name)
    if kind == "preset":
        return apply_filters(df, PRESETS[key])
    return strategy_hits(df, signals(data, [key], True))


def strat_of(name):
    kind, key = resolve(name)
    return key if kind == "strategy" else None


def block(title, hits, limit=10, strat=None):
    lines = [f"{title} ({len(hits)})"]
    for r in hits.head(limit).itertuples():
        lines.append(f"{r.Symbol}  {r.Price:.2f} ({r.ChgPct:+.1f}%)  RVOL {r.RVOL:.1f}x  RSI {r.RSI:.0f}  score {r.Score:.0f}")
        if strat:  # actionable plan, sized to RISK_PER_TRADE
            p = trade_plan(r.Price, r.ATRpct, strat, RISK)
            tgt = f"target {p['Target']:.2f}" if p["Target"] == p["Target"] else "exit by rule"
            lines.append(f"   buy ~{p['Buy near']:.2f} next open | stop {p['Stop']:.2f} | {tgt} | qty {p['Qty']} | hold <= {p['Hold d']}d")
    if len(hits) > limit:
        lines.append(f"... +{len(hits) - limit} more")
    return "\n".join(lines)


def load_state(today):
    try:
        s = json.loads(STATE.read_text())
    except Exception:
        s = {}
    return s if s.get("date") == today else {"date": today, "sent": {}, "eod": False}


def intraday_alerts(df, data, presets, state, stamp):
    for p in presets:
        hits = hits_for(p, df, data)
        new = hits[~hits["Symbol"].isin(state["sent"].get(p, []))]
        if len(new):
            notify(f"NEW {stamp}\n" + block(p, new, strat=strat_of(p)))
            state["sent"].setdefault(p, []).extend(new["Symbol"].tolist())
            STATE.write_text(json.dumps(state))


def summary(df, data, presets, title):
    return title + "\n\n" + "\n\n".join(block(p, hits_for(p, df, data), strat=strat_of(p)) for p in presets)


# ------------------------------------------------------------------ main
def main(argv=None):
    ap = argparse.ArgumentParser(description="NSE screener phone alerts")
    ap.add_argument("--universe", choices=["nifty50", "both"], default="nifty50")
    ap.add_argument("--indices", help='comma separated index lists, e.g. "Nifty 100,Nifty Midcap 150,F&O stocks" (downloaded from NSE)')
    ap.add_argument("--symbols", help="space separated NSE symbols; overrides --universe")
    ap.add_argument("--presets", default="Pre-breakout,Breakout + volume", help="comma separated screener presets and/or backtest strategies, e.g. \"Pre-breakout,Strategy: Weekly trend breakout\"")
    ap.add_argument("--every", type=int, default=10, help="minutes between intraday scans (default 10)")
    ap.add_argument("--eod-only", action="store_true", help="only send the closing summary")
    ap.add_argument("--once", action="store_true", help="scan now, send a full summary, exit (ignores market hours)")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--find-chat-id", action="store_true")
    a = ap.parse_args(argv)

    if a.find_chat_id:
        return find_chat_id()
    if a.test:
        return notify("Test alert from your NSE screener bot. If you can read this on your phone, alerts work.")
    presets = [p.strip() for p in a.presets.split(",") if p.strip()]
    bad = [p for p in presets if resolve(p)[0] is None]
    if bad:
        sys.exit(f"Unknown preset(s): {bad}. Choose from: {list(PRESETS) + ['Strategy: ' + n for n in STRATS]}")
    if a.indices:
        import universe
        symbols, _, notes = universe.resolve([x.strip() for x in a.indices.split(",") if x.strip()], a.symbols or "", [])
        print("\n".join(notes))
        if not symbols:
            sys.exit("No symbols could be loaded for --indices. Check the names or your connection.")
    else:
        symbols = ([s.upper().removesuffix(".NS") for s in a.symbols.split()] if a.symbols
                   else NIFTY50 + MIDCAPS if a.universe == "both" else NIFTY50)

    if a.once:
        df, last, data = scan(symbols)
        notify(summary(df, data, presets, f"Scan {datetime.now(IST):%d %b %H:%M} IST (last bar {last:%d %b})"))
        return

    print(f"Watching {len(symbols)} stocks | presets: {presets} | every {a.every} min | Ctrl+C to stop")
    fails = 0
    while True:
        now = datetime.now(IST)
        if now.weekday() >= 5 or now.time() > STOP:
            print("Outside market hours - exiting.")
            return
        if now.time() < OPEN:
            time.sleep(min(60, a.every * 60))
            continue
        try:
            df, last, data = scan(symbols)
            fails = 0
        except Exception as e:
            fails += 1
            print(f"{now:%H:%M} scan failed ({fails}): {e}")
            if fails == 3:
                notify("Alert bot: data download has failed 3 times in a row (Yahoo rate limit or network).")
            time.sleep(a.every * 60)
            continue
        state = load_state(f"{now:%Y-%m-%d}")
        if last != now.date():  # last bar is not today -> exchange holiday
            print("No bar for today (market holiday?) - exiting.")
            return
        if now.time() >= EOD:
            if not state["eod"]:
                notify(summary(df, data, presets, f"Closing scan {now:%d %b} (final daily candle)"))
                state["eod"] = True
                STATE.write_text(json.dumps(state))
            return
        if not a.eod_only and now.time() <= CLOSE:
            intraday_alerts(df, data, presets, state, f"{now:%H:%M}")
        print(f"{now:%H:%M} scanned {len(df)} stocks")
        time.sleep(a.every * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped.")
