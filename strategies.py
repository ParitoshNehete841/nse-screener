"""Indicator features and the 7 strategy entry/exit rules. No Streamlit, so the web app, backtester, paper trading
and alert bot all use exactly the same rules. Each rule: fn(features) -> (entry_signal, exit_signal)."""
import arrow_guard  # noqa: F401  (must run before pandas)
import pandas as pd


def _rsi(c, n):
    dl = c.diff()
    up = dl.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-dl.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)


def feats(d, idx63):
    f = d.copy()
    c, h, l, v = f["Close"], f["High"], f["Low"], f["Volume"]
    pc = c.shift()
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    f["ATR"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    f["RSI"], f["RSI2"] = _rsi(c, 14), _rsi(c, 2)
    for n in (20, 50, 200):
        f[f"E{n}"] = c.ewm(span=n, adjust=False).mean()
    f["S5"] = c.rolling(5).mean()
    f["H10"], f["H20"] = h.shift().rolling(10).max(), h.shift().rolling(20).max()
    f["L10"], f["L20"] = l.shift().rolling(10).min(), l.shift().rolling(20).min()
    f["H252"] = h.shift().rolling(252, min_periods=200).max()
    f["L252"] = l.rolling(252, min_periods=200).min()
    f["RVOL"] = v / v.shift().rolling(20).mean()
    f["SQ"] = (4 * c.rolling(20).std() / c.rolling(20).mean()).rolling(120).rank(pct=True) * 100
    f["RS"] = c.pct_change(63) * 100 - idx63.reindex(f.index).ffill()
    f["IBS"] = (c - l) / (h - l).where(h > l)  # internal bar strength: where the close sits in the day's range (0 = low, 1 = high)
    f["PC"], f["PO"], f["PH"], f["PL"] = c.shift(), f["Open"].shift(), h.shift(), l.shift()  # previous day close/open/high/low
    # weekly signals, stamped on the last trading day of each (completed) week
    per = f.index.to_period("W-FRI")
    g = f.groupby(per)
    w = pd.DataFrame({"H": g["High"].max(), "C": g["Close"].last(), "V": g["Volume"].sum(),
                      "D": pd.Series(f.index, index=f.index).groupby(per).last()})
    e10, e30 = w["C"].ewm(span=10, adjust=False).mean(), w["C"].ewm(span=30, adjust=False).mean()
    wen = (w["C"] > e10) & (e10 > e30) & (w["C"] > w["H"].shift().rolling(10).max()) \
        & (w["V"] >= 1.2 * w["V"].shift().rolling(10).mean())
    f["WE"], f["WX"] = False, False
    f.loc[w["D"][wen].values, "WE"] = True
    f.loc[w["D"][w["C"] < e10].values, "WX"] = True
    return f


def _trend(f):
    return (f["Close"] > f["E50"]) & (f["E50"] > f["E200"])


def _hammer(f):
    body = (f["Close"] - f["Open"]).abs()
    wick = f[["Open", "Close"]].min(axis=1) - f["Low"]
    return (body > 0) & (wick >= 2 * body) & (f["IBS"] >= 0.6)


def _engulf(f):
    return (f["PC"] < f["PO"]) & (f["Close"] > f["Open"]) & (f["Close"] >= f["PO"]) & (f["Open"] <= f["PC"])


STRATS = {
    "Breakout 20D + volume": dict(
        fn=lambda f: (_trend(f) & (f["Close"] > f["H20"]) & (f["RVOL"] >= 1.5), f["Close"] < f["L10"]),
        stop=2.0, target=0.0, hold=40,
        about="Uptrend (Close>EMA50>EMA200), close above prior 20-day high on 1.5x volume. Exit: close below 10-day low."),
    "Pre-breakout squeeze": dict(
        fn=lambda f: (_trend(f) & (f["Close"] <= f["H20"]) & (f["Close"] >= 0.97 * f["H20"]) & (f["SQ"] <= 40)
                      & f["RSI"].between(50, 70), f["Close"] < f["L10"]),
        stop=2.0, target=0.0, hold=40,
        about="Uptrend, within 3% below the 20-day high, tight Bollinger squeeze, RSI 50-70. Buys before the break. Exit: close below 10-day low."),
    "Pullback to EMA20": dict(
        fn=lambda f: (_trend(f) & (f["Low"] <= f["E20"] * 1.01) & (f["Close"] > f["E20"]) & f["RSI"].between(40, 58)
                      & (f["Close"] > f["Open"]), f["Close"] < f["E50"]),
        stop=1.5, target=3.0, hold=15,
        about="Uptrend stock dips to EMA20 and closes back above it as a green candle, RSI 40-58. Exit: 3R target, stop, or close below EMA50."),
    "Trend template (VCP-lite)": dict(
        fn=lambda f: (_trend(f) & (f["Close"] >= 0.75 * f["H252"]) & (f["Close"] >= 1.3 * f["L252"]) & (f["RS"] > 0)
                      & (f["SQ"] <= 35) & (f["Close"] > f["H10"]), f["Close"] < f["E20"]),
        stop=2.5, target=0.0, hold=60,
        about="Minervini-style: uptrend, within 25% of 52W high, 30%+ off the low, beating Nifty, squeezed, then 10-day high break. Exit: close below EMA20."),
    "52-week-high momentum": dict(
        fn=lambda f: (_trend(f) & (f["Close"] > f["H252"]) & (f["RVOL"] >= 1.2), f["Close"] < f["L20"]),
        stop=2.5, target=0.0, hold=80,
        about="Close makes a new 52-week high in an uptrend on 1.2x volume (52-week-high momentum effect). Exit: close below 20-day low."),
    "RSI(2) mean reversion": dict(
        fn=lambda f: ((f["Close"] > f["E200"]) & (f["RSI2"] < 10), f["Close"] > f["S5"]),
        stop=3.0, target=0.0, hold=10,
        about="Short-term oversold dip (RSI2<10) in a long-term uptrend (above EMA200). Exit: close above 5-day average, or 10 days."),
    "Weekly trend breakout": dict(
        fn=lambda f: (f["WE"], f["WX"]),
        stop=3.0, target=0.0, hold=60,
        about="Weekly bars: close above prior 10-week high, above rising 10/30-week EMAs, weekly volume 1.2x. Exit: weekly close below 10-week EMA."),
    "Short swing: Strong close + volume": dict(
        fn=lambda f: ((f["Close"] > f["E20"]) & (f["E20"] > f["E50"]) & (f["IBS"] >= 0.8) & (f["RVOL"] >= 1.3)
                      & (f["Close"] > f["PH"]) & (f["Close"] > f["Open"]), f["Close"] < f["E20"]),
        stop=1.5, target=2.0, hold=5,
        about="Buy next open, sell within ~5 days. Green candle closing in the top 20% of its range, above yesterday's high, on 1.3x volume, in an uptrend (EMA20>EMA50). Strong closes tend to carry into the next session."),
    "Short swing: Reversal candle at EMA20": dict(
        fn=lambda f: (_trend(f) & (f["Low"] <= f["E20"] * 1.01) & (f["Close"] >= f["E20"] * 0.98) & f["RSI"].between(35, 65)
                      & (_hammer(f) | _engulf(f)), f["Close"] < f["E50"]),
        stop=1.5, target=2.0, hold=5,
        about="Buy next open, sell within ~5 days. Hammer or bullish-engulfing candle touching the 20-day EMA in an uptrend, RSI 35-65: a bounce off support."),
    "Short swing: IBS oversold bounce": dict(
        fn=lambda f: ((f["Close"] > f["E200"]) & (f["IBS"] < 0.2) & (f["Close"] < f["PC"]), f["Close"] > f["PH"]),
        stop=2.0, target=0.0, hold=5,
        about="Buy next open, sell within ~5 days. Down day closing in the bottom 20% of its range while above the 200-day EMA: short-term snap-back. Exit: close above yesterday's high."),
    "Short swing: 3-day dip in uptrend": dict(
        fn=lambda f: ((f["Close"] > f["E200"]) & (f["Close"] < f["Close"].shift(1)) & (f["Close"].shift(1) < f["Close"].shift(2))
                      & (f["Close"].shift(2) < f["Close"].shift(3)) & (f["RSI"] < 50), f["Close"] > f["S5"]),
        stop=2.0, target=0.0, hold=5,
        about="Buy next open, sell within ~5 days. Three lower closes in a row while above the 200-day EMA, RSI under 50. Exit: close above the 5-day average."),
}
