"""News tab: market-moving headlines (geopolitics, oil, RBI, rupee, trade...) mapped to the stocks they usually move,
plus headlines for one stock. Source: Google News RSS (no API key). The mapping is rule-based, not a prediction."""
import arrow_guard  # noqa: F401  (must run before pandas)
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime

import pandas as pd
import streamlit as st

from backtest import table
from core import IST
from glossary import T

MACRO_QUERIES = (
    "India Pakistan military border tension", "India China border military", "war missile airstrike geopolitical markets",
    "defence ministry order contract India", "crude oil price OPEC", "RBI repo rate monetary policy", "US Fed rate decision",
    "rupee dollar exchange rate", "India US tariff trade deal", "Sensex Nifty market outlook", "gold price record",
    "monsoon rainfall India crop", "USFDA warning pharma India", "bank NPA fraud RBI penalty")

# name, regex on the lowercase headline, stocks that usually gain (up) / lose (down), why
THEMES = [
    dict(name="Military / war / border tension",
         kw=r"\b(war|missile|airstrike|air strike|border (clash|tension|skirmish)|terror attack|drone attack|troops|military (strike|escalation|tension|conflict))\b",
         up="HAL BEL MAZDOCK COCHINSHIP BHARATFORG", down="INDIGO ASIANPAINT MARUTI",
         note="Defence names usually rally; the broad market often turns risk-off and oil can spike."),
    dict(name="Ceasefire / de-escalation", kw=r"(ceasefire|de-escalat|peace (deal|talks)|truce)",
         up="HDFCBANK ICICIBANK RELIANCE MARUTI INDIGO", down="HAL BEL MAZDOCK",
         note="Relief rally in the broad market; defence stocks often cool off."),
    dict(name="Defence orders / procurement",
         kw=r"(defence (order|contract|deal)|dac (approv|clear)|procurement|fighter jet|submarine|missile (order|deal))",
         up="HAL BEL MAZDOCK COCHINSHIP BHARATFORG", down="", note="Direct order-book positive for listed defence companies."),
    dict(name="Crude oil rising", kw=r"\b(crude|brent|oil prices?)\W+(?:\w+\W+){0,2}(surg\w*|jump\w*|spik\w*|rall\w*|soar\w*|climb\w*|ris\w*|higher)\b",
         up="ONGC", down="ASIANPAINT MARUTI INDIGO BPCL", note="Higher crude helps producers; hurts paints, autos, airlines, oil marketers."),
    dict(name="Crude oil falling", kw=r"\b(crude|brent|oil prices?)\W+(?:\w+\W+){0,2}(fall\w*|slump\w*|drop\w*|slid\w*|tumbl\w*|plung\w*|eas\w*|declin\w*|lower\w*)\b",
         up="ASIANPAINT MARUTI INDIGO BPCL", down="ONGC", note="Lower crude helps paints, autos, airlines, oil marketers; hurts producers."),
    dict(name="RBI easing", kw=r"(rbi|reserve bank).*(rate cut|cuts? (the )?repo|easing|liquidity infusion)|repo rate cut",
         up="HDFCBANK ICICIBANK AXISBANK BAJFINANCE DLF MARUTI", down="", note="Lower rates support banks, NBFCs, realty and autos."),
    dict(name="RBI tightening", kw=r"(rbi|reserve bank).*(rate hike|hikes? (the )?repo|hawkish|tighten)",
         up="", down="HDFCBANK ICICIBANK BAJFINANCE DLF MARUTI", note="Higher rates pressure banks, NBFCs, realty and autos."),
    dict(name="Rupee weakening", kw=r"\brupee\W+(?:\w+\W+){0,3}(weak\w*|fall\w*|slump\w*|record low|depreciat\w*|slid\w*|tumbl\w*)|\bdollar\W+(?:\w+\W+){0,2}(surg\w*|strengthen\w*|jump\w*)",
         up="TCS INFY HCLTECH WIPRO TECHM SUNPHARMA DRREDDY", down="INDIGO ASIANPAINT",
         note="Exporters (IT, pharma) gain; importers (airlines, paints) pay more."),
    dict(name="Rupee strengthening", kw=r"\brupee\W+(?:\w+\W+){0,3}(gain\w*|strengthen\w*|rall\w*|surg\w*|ris\w*|recover\w*)\b",
         up="INDIGO ASIANPAINT", down="TCS INFY HCLTECH WIPRO TECHM", note="Importers gain; IT exporters lose some margin."),
    dict(name="US tariffs / trade-war worries", kw=r"^(?!.*(trade deal|trade pact|tariff (cut|relief|reduction)|tariffs? (eased|lifted))).*(tariff|trade war|export curb|visa fee|h-?1b)",
         up="", down="TCS INFY WIPRO SUNPHARMA DRREDDY BHARATFORG", note="IT, pharma and auto-parts exporters are most exposed."),
    dict(name="Trade deal / tariff relief", kw=r"(trade deal|trade pact|tariff (cut|relief|reduction)|tariffs? (eased|lifted))",
         up="TCS INFY WIPRO SUNPHARMA DRREDDY BHARATFORG", down="", note="Exporters benefit from lower trade barriers."),
    dict(name="Global risk-off",
         kw=r"(wall street|global markets?|stocks?) (sell-?off|rout|crash|tumble|slump)|recession fears?|fed.*(hike|hawkish|higher for longer)",
         up="", down="HDFCBANK ICICIBANK RELIANCE INFY TCS", note="Foreign selling hits heavyweights first (banks, Reliance, IT)."),
    dict(name="Global risk-on", kw=r"(wall street|global markets?|stocks?) (rally|record|surge)|fed.*(cut|dovish)",
         up="HDFCBANK ICICIBANK RELIANCE INFY TCS", down="", note="Foreign buying lifts heavyweights first."),
    dict(name="Gold surging", kw=r"\bgold\W+(?:\w+\W+){0,3}(record|surg\w*|rall\w*|jump\w*|soar\w*)", up="MUTHOOTFIN", down="TITAN",
         note="Gold lenders gain on collateral value; jewellers can face demand and margin pressure."),
    dict(name="Good monsoon / farm outlook", kw=r"monsoon.*(above[- ]normal|good|bountiful|healthy|on track)|record (kharif|rabi|crop)",
         up="HINDUNILVR NESTLEIND TATACONSUMER M&M ITC", down="", note="Better rural income supports FMCG and tractors."),
    dict(name="Weak monsoon / El Nino", kw=r"(monsoon.*(deficit|weak|below normal|delayed|erratic)|el ni.o|drought)",
         up="", down="HINDUNILVR NESTLEIND TATACONSUMER M&M ITC", note="Weak rural income hurts FMCG and tractors."),
    dict(name="China stimulus", kw=r"china.*(stimulus|rebound|recovery)", up="TATASTEEL JSWSTEEL HINDALCO VEDL", down="",
         note="Chinese demand lifts metal prices."),
    dict(name="USFDA action", kw=r"(usfda|us fda|fda).*(warning|import alert|observation|483|ban|inspection)",
         up="", down="SUNPHARMA DRREDDY CIPLA LUPIN AUROPHARMA", note="Sector caution for pharma exporters; check which company is named."),
    dict(name="Banking / NBFC stress", kw=r"(npa|bank fraud|nbfc stress|rbi (penalt|action)|bad loans|asset quality)",
         up="", down="HDFCBANK ICICIBANK AXISBANK KOTAKBANK SBIN BAJFINANCE", note="Asset-quality worries hit lenders."),
    dict(name="Infra / capex push", kw=r"(capex|infrastructure (spending|push)|railway (order|capex)|highway (project|order))",
         up="LT RVNL NTPC POWERGRID ULTRACEMCO", down="", note="Government spending supports infra and cement."),
]

NAMES = dict(p.split("=") for p in (
    "ADANIENT=Adani Enterprises;ADANIPORTS=Adani Ports;APOLLOHOSP=Apollo Hospitals;ASIANPAINT=Asian Paints;AXISBANK=Axis Bank;"
    "BAJAJ-AUTO=Bajaj Auto;BAJFINANCE=Bajaj Finance;BAJAJFINSV=Bajaj Finserv;BEL=Bharat Electronics;BHARTIARTL=Bharti Airtel;"
    "CIPLA=Cipla;COALINDIA=Coal India;DRREDDY=Dr Reddys Laboratories;EICHERMOT=Eicher Motors;ETERNAL=Eternal Zomato;GRASIM=Grasim;"
    "HCLTECH=HCL Technologies;HDFCBANK=HDFC Bank;HDFCLIFE=HDFC Life;HEROMOTOCO=Hero MotoCorp;HINDALCO=Hindalco;"
    "HINDUNILVR=Hindustan Unilever;ICICIBANK=ICICI Bank;INDUSINDBK=IndusInd Bank;INFY=Infosys;ITC=ITC Limited;JIOFIN=Jio Financial;"
    "JSWSTEEL=JSW Steel;KOTAKBANK=Kotak Mahindra Bank;LT=Larsen and Toubro;M&M=Mahindra and Mahindra;MARUTI=Maruti Suzuki;"
    "NESTLEIND=Nestle India;NTPC=NTPC;ONGC=ONGC;POWERGRID=Power Grid;RELIANCE=Reliance Industries;SBILIFE=SBI Life;"
    "SBIN=State Bank of India;SHRIRAMFIN=Shriram Finance;SUNPHARMA=Sun Pharma;TATACONSUMER=Tata Consumer;TATASTEEL=Tata Steel;"
    "TCS=Tata Consultancy Services;TECHM=Tech Mahindra;TITAN=Titan Company;TRENT=Trent;ULTRACEMCO=UltraTech Cement;WIPRO=Wipro;"
    "HAL=Hindustan Aeronautics;BHEL=BHEL;MAZDOCK=Mazagon Dock;COCHINSHIP=Cochin Shipyard;BHARATFORG=Bharat Forge;"
    "RVNL=Rail Vikas Nigam;MUTHOOTFIN=Muthoot Finance;IRCTC=IRCTC;DLF=DLF;POLYCAB=Polycab").split(";"))

POS = r"(surge|rally|jump|gain|beat|record|upgrade|order win|wins order|bags order|profit (rises|jumps)|buy rating|target (raised|hiked)|soar|outperform|bullish|strong)"
NEG = r"(fall|slump|plunge|drop|loss|probe|fraud|downgrade|penalt|raid|miss|weak|sell-?off|bearish|ban\b|lawsuit|default|resign|cut target|target cut)"


def fetch_rss(query, days):
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": f"{query} when:{days}d", "hl": "en-IN", "gl": "IN", "ceid": "IN:en"})
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        root = ET.fromstring(r.read())
    out = []
    for it in root.iter("item"):
        title, src = (it.findtext("title") or "").strip(), (it.findtext("source") or "").strip()
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3]
        try:
            when = parsedate_to_datetime(it.findtext("pubDate")).astimezone(IST)
        except Exception:
            continue
        out.append(dict(title=title, source=src, link=it.findtext("link") or "", when=when))
    return out


@st.cache_data(ttl=600, show_spinner="Fetching news...")
def fetch_many(queries: tuple, days: int):
    def one(q):
        try:
            return fetch_rss(q, days)[:12], None
        except Exception as e:
            return [], f"{type(e).__name__}: {e}"
    with ThreadPoolExecutor(6) as ex:
        res = list(ex.map(one, queries))
    items, seen = [], set()
    for lst, _ in res:
        for it in lst:
            if it["title"].lower() not in seen:
                seen.add(it["title"].lower())
                items.append(it)
    if not items:  # raising keeps a failure out of the cache
        err = next((e for _, e in res if e), None)
        raise RuntimeError("No news came back" + (f" ({err})" if err else "") + ". Google News may be blocked on this network.")
    return sorted(items, key=lambda x: x["when"], reverse=True)


def themes_for(title):
    t = title.lower()
    return [th for th in THEMES if re.search(th["kw"], t)]


def tone(title):
    t = title.lower()
    s = bool(re.search(POS, t)) - bool(re.search(NEG, t))
    return "Positive" if s > 0 else "Negative" if s < 0 else "Neutral"


def impact_table(hits):
    rows = {}
    for it in hits:
        for th in it["themes"]:
            for side, key in (("Up", "up"), ("Down", "down")):
                for s in th[key].split():
                    r = rows.setdefault(s, {"Up": 0, "Down": 0, "themes": set()})
                    r[side] += 1
                    r["themes"].add(th["name"])
    df = pd.DataFrame([{"Symbol": s, "Net": r["Up"] - r["Down"], "Up": r["Up"], "Down": r["Down"],
                        "Themes": "; ".join(sorted(r["themes"]))} for s, r in rows.items()])
    return df.reindex(df["Net"].abs().sort_values(ascending=False).index).reset_index(drop=True) if len(df) else df


def line(it):
    title = it["title"].replace("[", "(").replace("]", ")")
    return f"- [{title}]({it['link']}) - {it['source']}, {it['when']:%d %b %H:%M}"


def render_news(symbols):
    st.title("News")
    st.caption("Market-moving headlines (war, oil, RBI, rupee, tariffs, monsoon...) mapped to the stocks they usually move. "
               "Rule-based keyword matching, not a prediction: headlines can be old, repeated or wrong. Verify before trading.")
    days = st.selectbox("Look back", [1, 3, 7], index=1, format_func=lambda d: f"{d} day" + ("s" if d > 1 else ""),
                        help=T["News lookback"], key="news_days")
    if st.button("Load market news", type="primary"):
        try:
            st.session_state["news"] = fetch_many(MACRO_QUERIES, days)
        except Exception as e:
            st.error(str(e))
    items = st.session_state.get("news")
    if items:
        for it in items:
            it["themes"] = themes_for(it["title"])
        hits = [it for it in items if it["themes"]]
        st.subheader(f"Market-moving headlines ({len(hits)} of {len(items)} matched a theme)")
        for it in hits[:20]:
            st.markdown(line(it))
            for th in it["themes"]:
                up, down = th["up"].replace(" ", ", "), th["down"].replace(" ", ", ")
                st.caption(f"{th['name']}: " + (f"may help {up}. " if up else "") + (f"may hurt {down}. " if down else "") + th["note"])
        if hits:
            st.subheader("Stocks most affected right now")
            table(impact_table(hits).head(15))
        else:
            st.info("No headline matched a known theme in this window. Try a longer look-back.")

    st.subheader("Stock news")
    c1, c2 = st.columns([1, 2])
    sym = c1.selectbox("Stock", list(symbols), key="news_sym")
    if c2.button("Load stock news"):
        try:
            st.session_state["snews"] = (sym, fetch_many((f'"{NAMES.get(sym, sym)}" shares OR stock',), days))
        except Exception as e:
            st.error(str(e))
    if "snews" in st.session_state:
        s, rows = st.session_state["snews"]
        st.write(f"**{s}** - latest headlines (tone is a rough keyword guess)")
        for it in rows[:12]:
            st.markdown(line(it) + f" - *{tone(it['title'])}*")
