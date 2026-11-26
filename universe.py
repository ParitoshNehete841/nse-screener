"""Stock lists loaded dynamically from NSE index files (current constituents), cached on disk for 24 hours.
No Streamlit here, so the web app and the alert bot share it. If NSE cannot be reached the last cached copy is used,
then (for Nifty 50 only) a built-in list; otherwise upload the CSV from niftyindices.com in the sidebar."""
import arrow_guard  # noqa: F401  (must run before pandas)
import csv
import io
import re
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import pandas as pd

from core import NIFTY50

CACHE = Path(__file__).resolve().parent / "cache_universe"
HOSTS = ("https://nsearchives.nseindia.com/content/indices/", "https://www.niftyindices.com/IndexConstituent/")
INDEXES = {  # label -> file name on the hosts above
    "Nifty 50": "ind_nifty50list.csv", "Nifty Next 50": "ind_niftynext50list.csv", "Nifty 100": "ind_nifty100list.csv",
    "Nifty 200": "ind_nifty200list.csv", "Nifty 500": "ind_nifty500list.csv",
    "Nifty Midcap 150": "ind_niftymidcap150list.csv", "Nifty Smallcap 250": "ind_niftysmallcap250list.csv",
    "Nifty Bank": "ind_niftybanklist.csv", "Nifty Financial Services": "ind_niftyfinancelist.csv",
    "Nifty IT": "ind_niftyitlist.csv", "Nifty Pharma": "ind_niftypharmalist.csv", "Nifty Auto": "ind_niftyautolist.csv",
    "Nifty FMCG": "ind_niftyfmcglist.csv", "Nifty Metal": "ind_niftymetallist.csv", "Nifty Energy": "ind_niftyenergylist.csv",
    "Nifty Realty": "ind_niftyrealtylist.csv", "Nifty PSU Bank": "ind_niftypsubanklist.csv",
    "Nifty Infrastructure": "ind_niftyinfralist.csv",
}
FNO = "F&O stocks"
FNO_URLS = ("https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv",)
INDEX_UNDERLYINGS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
           "Accept": "text/csv,*/*", "Referer": "https://www.nseindia.com/"}
_offline_until = 0.0  # after a network failure, skip the network for a minute so other lists do not each wait for a timeout


def labels():
    return list(INDEXES) + [FNO]


def parse_constituents(text):
    """DataFrame[Symbol, Company, Industry] from an NSE constituent CSV, an F&O lot-size CSV, or one symbol per line."""
    text = text.lstrip("\ufeff")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return pd.DataFrame(columns=["Symbol", "Company", "Industry"])
    head = [h.strip().lower() for h in rows[0]]
    if "symbol" in head:
        i = head.index("symbol")
        ci = head.index("company name") if "company name" in head else None
        ii = head.index("industry") if "industry" in head else None
        body = rows[1:]
        get = lambda r, k: (r[k].strip() if k is not None and k < len(r) else "")
        out = [(get(r, i).upper(), get(r, ci), get(r, ii)) for r in body]
    else:  # plain list: one or more symbols per row
        out = [(re.sub(r"\.NS$", "", t.strip().upper()), "", "") for r in rows for t in r if re.fullmatch(r"[A-Za-z0-9&\-\.]{1,20}", t.strip())]
    df = pd.DataFrame(out, columns=["Symbol", "Company", "Industry"])
    df["Symbol"] = df["Symbol"].str.replace(r"\.NS$", "", regex=True)
    return df[df["Symbol"].str.fullmatch(r"[A-Z0-9&\-]{1,20}") & ~df["Symbol"].isin(INDEX_UNDERLYINGS)].drop_duplicates("Symbol")


def _fetch(urls):
    global _offline_until
    if time.time() < _offline_until:
        return None, "offline"
    last = "no response"
    for u in urls:
        try:
            req = urllib.request.Request(u, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=8) as r:
                text = r.read().decode("utf-8", "ignore")
            if len(parse_constituents(text)) >= 5:  # sanity check: it really is a constituent list
                return text, None
            last = "unexpected file contents"
        except Exception as e:
            last = f"{type(e).__name__}"
    if last != "unexpected file contents":
        _offline_until = time.time() + 60
    return None, last


def get_list(label, max_age=86400, force=False):
    """(DataFrame[Symbol, Company, Industry], note) for one index label."""
    CACHE.mkdir(exist_ok=True)
    path = CACHE / (re.sub(r"[^a-z0-9]+", "_", label.lower()) + ".csv")
    fresh = path.exists() and time.time() - path.stat().st_mtime < max_age
    if fresh and not force:
        return parse_constituents(path.read_text(encoding="utf-8")), f"{label}: cached {datetime.fromtimestamp(path.stat().st_mtime):%d %b %H:%M}"
    urls = FNO_URLS if label == FNO else [h + INDEXES[label] for h in HOSTS]
    text, err = _fetch(urls)
    if text:
        path.write_text(text, encoding="utf-8")
        return parse_constituents(text), f"{label}: downloaded from NSE just now"
    if path.exists():
        return parse_constituents(path.read_text(encoding="utf-8")), (
            f"{label}: NSE unreachable ({err}), using copy from {datetime.fromtimestamp(path.stat().st_mtime):%d %b}")
    if label == "Nifty 50":
        return pd.DataFrame({"Symbol": NIFTY50, "Company": "", "Industry": ""}), "Nifty 50: NSE unreachable, using the built-in list"
    return pd.DataFrame(columns=["Symbol", "Company", "Industry"]), (
        f"{label}: could not load ({err}). Upload its constituent CSV in the sidebar (niftyindices.com publishes one per index).")


def resolve(selected, extra_text="", uploads=(), force=False):
    """Union of the selected lists + extra symbols + uploaded CSVs -> (symbols, meta DataFrame, notes)."""
    parts, notes = [], []
    for lab in selected:
        if lab in INDEXES or lab == FNO:
            df, note = get_list(lab, force=force)
            parts.append(df)
            notes.append(note)
    for i, text in enumerate(uploads):
        df = parse_constituents(text)
        parts.append(df)
        notes.append(f"Uploaded file {i + 1}: {len(df)} symbols")
    if extra_text.strip():
        df = parse_constituents(extra_text.replace(",", " ").replace(" ", "\n"))
        parts.append(df)
        notes.append(f"Extra symbols: {len(df)}")
    meta = (pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=["Symbol", "Company", "Industry"]))
    meta = meta.sort_values("Industry", key=lambda s: s.eq(""), kind="stable").drop_duplicates("Symbol").reset_index(drop=True)  # keep rows that have an industry
    return meta["Symbol"].tolist(), meta, notes
