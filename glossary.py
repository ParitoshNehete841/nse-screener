"""Plain-English explanations used for hover (?) help, table header tooltips and the Glossary tab."""
import html

import streamlit as st

T = {
    # --- screener columns
    "LTP": "Last traded price. Yahoo's NSE data is about 15 minutes delayed.",
    "Chg %": "Change versus the previous day's close. During market hours it is today's move so far.",
    "RVOL": "Relative volume = today's volume divided by the average volume of the last 20 days. 1.5x means 50% more shares traded than normal. High RVOL on a breakout suggests real buying interest. Intraday, the app projects today's volume to a full day, so it is rough in the first hour.",
    "RSI": "Relative Strength Index (14 days), a 0-100 momentum meter. Above 70 = stretched (overbought), below 30 = oversold. 50-70 usually means a healthy uptrend. Not the same as '3M RS', which compares the stock with Nifty.",
    "vs 20D High %": "Distance from the highest high of the previous 20 trading days (today excluded). Negative = still below it (-2 means 2% below). Positive = it has broken above it, a 20-day breakout.",
    "vs 52W High %": "Distance from the 52-week (1 year) high. Close to 0 means the stock is near its yearly high. Stocks near 52-week highs often have strong momentum.",
    "vs EMA20 %": "Distance from the 20-day EMA. Near 0 means price is sitting on its short-term average, the zone where pullback trades are looked for.",
    "Squeeze pctl": "Bollinger Band width compared with the last 120 days, as a percentile. Low (say under 30) = the price range is unusually tight, like a coiled spring. Tight ranges often come before a big move, but this does not tell you the direction.",
    "3M RS": "3-month relative strength: the stock's 3-month return minus Nifty 50's 3-month return, in percentage points. Positive = beating the market.",
    "ATR %": "ATR as a percentage of price, i.e. the stock's typical daily move. 3 means it normally moves about 3% a day. Higher = more volatile, needs a wider stop.",
    "Turnover Cr": "Average daily traded value in Rs crore (20-day average volume x price). A liquidity check. Low turnover means wide spreads and trouble exiting, so avoid it.",
    "Score": "0-100 ranking that mixes trend, closeness to a breakout, squeeze, volume, RSI and relative strength. Use it to sort the list, not as a buy signal.",
    "TradingView": "Opens this stock's chart on TradingView.",
    # --- sidebar controls
    "Universe": "Which stocks are scanned. Pick one or more NSE index lists (Nifty 50, Nifty 100, Nifty 200, Midcap 150, sector indices, F&O stocks...). They are downloaded from NSE, cached for a day, and refreshed on demand, so they track the current constituents. Bigger lists take longer and risk Yahoo rate limits.",
    "Custom symbols": "Extra NSE symbols to add to the selected lists, separated by spaces or commas, without .NS. Example: RELIANCE TCS INFY",
    "Preset": "One-click filter sets. Pre-breakout: uptrend, just under the 20-day high, tight squeeze. Breakout + volume: just broke the 20-day high on heavy volume. Pullback to EMA20: uptrend stock dipping to its average. Momentum leaders: near 52-week high and beating Nifty. Custom: no filters, set your own. You can also pick any of the 7 backtest strategies to see which stocks fire it on the latest candle; their rules are fixed and identical to the Backtest tab, so the sliders disappear.",
    "Strategies": "Which backtest strategies fired on the latest candle for this stock. Weekly trend breakout only counts on a Friday candle.",
    "Uptrend filter": "Close > EMA50 > EMA200: price is above its medium-term average and that is above the long-term average. A simple way to only look at stocks that are already rising.",
    "Project volume": "During market hours today's volume is only partial, so RVOL looks low. This scales it up to an estimate for a full day. Turn it off to see raw volume so far.",
    "Auto-refresh": "How often the screener downloads fresh prices. Yahoo data is about 15 minutes delayed, so refreshing faster than every 5 minutes adds little.",
    # --- core terms
    "EMA": "Exponential Moving Average: the average closing price of the last N days, giving recent days more weight. EMA20 = short term, EMA50 = medium term, EMA200 = long term.",
    "ATR": "Average True Range (14 days): the typical size of a day's price movement in Rs, including gaps. Used to set stops, for example a stop 2 x ATR below your entry.",
    "RSI(2)": "A very fast RSI over just 2 days. Under 10 means the stock has dropped sharply in a few days, which mean-reversion traders treat as a possible bounce.",
    "20D high": "Highest high of the previous 20 trading days. Closing above it is called a 20-day breakout.",
    "52W high": "Highest price of the last year. Staying near it signals strength.",
    "Relative strength": "How a stock performs compared with the market (Nifty 50). Do not confuse it with RSI, which only looks at the stock's own price.",
    # --- setups
    "Pre-breakout": "The stock is still just below resistance (such as its 20-day high), trading tight, in an uptrend. You buy before the break instead of after it.",
    "Breakout": "A close above a recent high (here the 20-day high), ideally on high volume. The idea: new highs attract more buyers.",
    "Pullback": "A temporary dip inside an uptrend, for example back to the EMA20. You buy the dip expecting the uptrend to resume.",
    "Momentum": "Stocks that have been rising tend to keep rising for a while, for example stocks making new 52-week highs.",
    "Mean reversion": "The opposite idea: an overstretched fall (for example RSI(2) under 10) tends to snap back toward the average. Holds are short, a few days.",
    "Trend template (VCP-lite)": "Minervini-style checklist: uptrend, near the 52-week high, well above the 52-week low, beating Nifty, price range tightening (VCP = volatility contraction pattern), then a breakout.",
    "Weekly trend breakout": "The same breakout idea on weekly candles. Slower, fewer trades, holds for weeks. Signals appear only after the week closes.",
    # --- backtest controls
    "Short swing": "Setups meant to be bought at the next open and sold within about a week (max hold 5 days). They need the finished daily candle, so scan after 15:50 IST. No setup makes the next day reliably bullish; check the Next-day up % column in the backtest before trusting one.",
    "IBS": "Internal bar strength = (close - low) / (high - low). Near 1 the stock closed at the top of the day's range, near 0 at the bottom. Very low IBS in an uptrend often bounces next day.",
    "Next-day up %": "Share of signals where the next day's close was above the signal day's close. Near 50% means no edge for a next-day pop; clearly above (and consistent across years) is what you want.",
    "Avg next-day %": "Average return from the signal day's close to the next day's close, before costs.",
    "Next day %": "This trade's return from the signal day's close to the next day's close, before costs. Positive = the next session was bullish.",
    "News lookback": "How far back to search headlines. 1 day = fresh events only; 7 days catches slower stories but includes stale ones.",
    "Net": "Up-hits minus down-hits across the matched headlines: a rough gauge of the news flow for the stock. Positive = more supportive headlines. A heuristic, not a forecast.",
    "Up": "Number of matched headlines whose theme usually helps this stock.",
    "Down": "Number of matched headlines whose theme usually hurts this stock.",
    "Themes": "The news themes (e.g. military tension, crude oil rising, RBI easing) behind the Up/Down counts.",
    "News theme": "A recurring kind of market-moving event with a typical effect on certain stocks, e.g. a defence order helps HAL and BEL. The mapping is a simple rule, not a prediction.",
    "Risk per trade": "The most you are willing to lose on one trade if its stop is hit, in Rs. Quantity is sized from it: qty = risk per trade / risk per share.",
    "Buy near": "Last close, used as a stand-in for tomorrow's open. Check the real price before ordering.",
    "Risk/sh": "Rupees lost per share if the stop is hit (buy price minus stop).",
    "Qty": "Shares to buy so a stopped-out trade loses about your Risk per trade. Check lot rules and available cash.",
    "Capital": "Approximate cash needed for that quantity (qty x price).",
    "Hold d": "Maximum days to hold. Exit earlier if the stop, target or the strategy's exit rule triggers.",
    "Fit": "How well the current Nifty regime suits this type of strategy: Good, Selective or Weak.",
    "Verdict": "Edge = 100+ tested trades and positive average R after costs. Low sample = under 100 trades. No tested edge = average R at or below zero.",
    "Regime": "Nifty's trend. Uptrend: close above EMA50 above EMA200. Pullback: still above EMA200 but under EMA50. Downtrend: under EMA200. Breakouts work best in uptrends; dip-buying is the better fit in pullbacks.",
    "Tested edge": "A strategy that, on your own stock list and period, produced positive average R after costs over at least 100 trades. This is evidence from the past on survivors, not a promise.",
    "Portfolio backtest": "Unlike the Backtest tab (every signal = 1R, unlimited capital), this simulates an account: limited cash, a cap on open positions, risk-based sizing, approximate Indian delivery charges and optional tax. It reports equity, drawdown and CAGR next to Nifty buy-and-hold, and splits the period into in-sample and out-of-sample.",
    "Risk % per trade": "Share of current equity you risk per trade (shares x distance from entry to stop). 1% of Rs 10 lakh means about Rs 10,000 at risk per trade.",
    "Max open positions": "How many stocks the account can hold at once. When more signals fire than there are free slots, the ones with the highest 3-month relative strength are taken first.",
    "Position cap %": "Largest share of equity allowed in one stock, even if the risk formula would allow more (protects against tight stops creating huge positions).",
    "Slippage %": "Extra cost per side from fills worse than the quoted price. 0.10% is a modest assumption for liquid stocks; use more for small caps.",
    "Charges": "Approximate Zerodha delivery charges: STT 0.1% each side, exchange and SEBI fees, stamp duty on the buy, GST on fees and a DP charge on each sell. Rates change, so verify with your broker.",
    "Out-of-sample": "The later part of history, kept apart from the earlier part. If results are much worse there than in-sample, the strategy is probably overfit or depends on one market regime.",
    "CAGR %": "Compound annual growth rate: the steady yearly return that would give the same final result.",
    "Max drawdown %": "Largest fall from a peak in account value. -25% means that at the worst point you were 25% below your best. Decide beforehand whether you could sit through it.",
    "Sharpe": "Return per unit of volatility from daily returns, annualised, with no risk-free rate. Above 1 is decent. Rough guide only.",
    "Calmar": "CAGR divided by the size of the max drawdown. Higher means more return for the pain.",
    "Avg exposure %": "Average share of equity actually invested in stocks. The rest sat in cash.",
    "Data store": "Prices are saved in data/prices.db and only missing days are downloaded. If Yahoo is down the app keeps working from the saved data, and the caption shows the true age of the data.",
    "Data health": "Checks on the stored prices. Stale = last bar older than the Nifty's; short history = under 250 bars; zero-volume days; extreme move = a one-day change over 25% (a split or a bad tick).",
    "Account size": "Your trading capital in Rs. Used only to express open risk and position sizes as a percentage; nothing is sent anywhere.",
    "Events": "Results or ex-dividend dates inside your holding window, plus F&O ban status. Dates come from Yahoo and are often incomplete for Indian stocks, so confirm on the company's announcements or NSE board meetings before holding through results.",
    "Risk now (Rs)": "What you would lose from the latest price if the stop is hit: (latest price - stop) x quantity. Zero once the price is below the stop. Blank when no stop is set.",
    "Unreal P&L (Rs)": "Profit or loss on an open position at the latest price, before charges. Not final until you sell.",
    "Unreal %": "Open position's percentage gain or loss at the latest price.",
    "Value (Rs)": "Latest price x quantity.",
    "Expectancy %": "Average percentage return per closed trade (wins and losses together). Needs to stay positive after charges.",
    "Avg win %": "Average percentage gain of the winning trades.",
    "Avg loss %": "Average percentage loss of the losing trades (shown negative).",
    "Net P&L (Rs)": "Gross profit minus approximate Zerodha delivery charges. No slippage is deducted for real fills.",
    "Avg days": "Average holding time in calendar days.",
    "Journal": "Your real trades, entered by hand or imported from a Zerodha tradebook CSV (matched first-in-first-out), tagged by strategy so you can compare real results with the backtest.",
    "Sector exposure": "Share of the open book in each industry. Stocks in one industry tend to move together, so a heavy tilt is concentrated risk.",
    "Order caps": "Hard limits the app enforces before any order: maximum rupee value per order, maximum entry orders per day, and how far the limit price may be from the last close. They protect you from typos and runaway behaviour.",
    "Dry run": "Orders are recorded in the ledger but never sent to Zerodha. Keep it on until the whole flow looks right, then switch live with a very small quantity first.",
    "Reconcile": "Compares the app's order ledger with Zerodha's order book (matched on the order tag) so you can see whether an order really went through, especially after an error or timeout. Always do this before retrying.",
    "Static IP": "A fixed internet address. Zerodha accepts API order requests only from the static IP you register in the Kite developer console (reading data works from anywhere). Get one from a cloud server, a broadband static-IP plan or a VPN with a dedicated IP.",
    "GTT OCO": "Good-Till-Triggered, one-cancels-other: a stop-loss and a target sell order held by Zerodha for up to a year. Whichever price is hit first sells your shares and cancels the other.",
    "Algo rules": "SEBI/NSE's retail algo framework applies since 1 April 2026: orders carry an exchange algo ID, API access needs a static IP and two-factor login, own-use strategies up to 10 orders per second need no registration, and you carry responsibility for your algo's logic. Do not run it for other people.",
    "Password gate": "Set APP_PASSWORD (in .env, the environment or Streamlit secrets) and the app asks for it before showing anything. Always use it on a deployed copy: your journal and the Broker tab are otherwise open to anyone with the link.",
    "Strategy": "The rule set being tested. A one-line description appears under the dropdown; the terms are explained in the Glossary tab.",
    "Years of history": "How far back to test. More years covers more market conditions (bull, bear, sideways) but downloads more data.",
    "Cost + slippage": "Everything that eats profit on a round trip: STT, brokerage, exchange/GST/stamp charges, DP charge, plus slippage (the gap between the price you expect and the price you actually get). 0.30% is a rough default for delivery trades; set it to what you really pay.",
    "Stop (x ATR)": "Stop-loss distance in ATRs below the entry price. 2 means the stop sits 2 x ATR under entry. Wider stops survive noise but make each loss bigger.",
    "Target (R)": "Profit target as a multiple of the risk. 3 means exit when the trade has made 3 times what you risked. 0 = no target; the strategy's exit rule decides.",
    "Max hold": "Exit at the close after this many trading days if nothing else has triggered an exit.",
    "Inspect": "Pick one strategy to see its per-year breakdown, exit reasons and individual trades.",
    # --- backtest results
    "Backtest": "Replaying a strategy's rules on past prices to see how it would have done. It shows what would have happened, not what will happen.",
    "R": "R is the amount you risk on a trade (entry price minus stop price). A trade that makes twice its risk is +2R; one stopped out is about -1R. Measuring in R lets you compare trades regardless of price or position size.",
    "Trades": "Number of simulated trades. With fewer than about 100, the other numbers are not trustworthy.",
    "Win %": "Share of trades that made money after costs. A low win rate can still be profitable if winners are much bigger than losers.",
    "Avg R": "Average result per trade in R. 0.2 means +0.2R per trade on average (the 'expectancy'). It must stay above zero after costs.",
    "Avg ret %": "Average percentage gain per trade after the cost and slippage you set.",
    "Profit factor": "Total gains divided by total losses (in R). Above 1 = profitable overall, 1 = break-even. Unreliable when trades are few.",
    "Avg bars": "Average holding time in trading days.",
    "Total R": "Sum of R across all trades: the rough overall result if you risked the same amount every trade.",
    "Max DD (R)": "Maximum drawdown: the worst fall from a peak in cumulative R. -15 means that at the worst point you were 15R below your best. Ask yourself whether you could sit through that.",
    "Cumulative R": "Running total of R over time, plotted by exit date. A steadily rising line is good; long flat or falling stretches show how painful the strategy can be.",
    "Year": "Calendar year in which the trade exited. Consistency across years matters more than one great year.",
    "Symbol": "NSE symbol.",
    "Entry": "Entry date (the open of the day after the signal).",
    "Exit": "Exit date.",
    "Entry px": "Entry fill price.",
    "Exit px": "Exit fill price.",
    "Bars": "Trading days held.",
    "Ret %": "Percentage return of the trade after costs.",
    "Exit reason": "stop = stop-loss hit. target = profit target hit. signal = the strategy's exit rule fired (filled at the next open). time = max holding days reached. open = still running; shown at the latest price.",
    "Stop": "Stop-loss price of the trade: entry price minus (stop multiple x ATR).",
    "Target": "Profit-target price. Blank means no target; the strategy's exit rule decides.",
    "Replay scope": "Replay one stock you name, or every stock in the sidebar universe.",
    "Replay window": "Signals are counted only if they fired between these two dates. Trades may still exit after the end date. Pick start and end, e.g. 1 Apr to 30 Apr.",
    "Paper trade": "A pretend trade recorded with its entry, stop and target but no real money. The app tracks it forward with the same exit rules, so you learn the real success rate before risking cash.",
    "Opened": "Date the paper trade was entered (a trading day).",
    "Closed": "Date the paper trade ended.",
    "Mark px": "Latest price used to value a trade that is still open.",
    "Unrealized R": "Profit or loss so far on an open trade, in R. Not final until the trade closes.",
    "Alert": "Notes from the last update, e.g. the strategy exit rule fired and the sale would happen at the next open.",
    # --- cautions
    "Survivorship bias": "The stock list is today's list. Stocks that crashed or were dropped from the index years ago are missing, so past results look better than real trading would have been.",
    "Overfitting": "Tweaking settings until past results look great. The strategy then fits noise and fails on new data. Prefer simple rules and settings that work across many years.",
    "Fills": "Entries fill at the next day's open after the signal. Stops fill at the stop price (or the open if the stock gaps below it). If stop and target are both touched on the same day, the stop counts.",
    "STCG": "Short-term capital gains tax on shares sold within a year (currently 20% in India; check current rules). The backtest does not deduct it.",
}

COLS = {"Price": "LTP", "ChgPct": "Chg %", "RVOL": "RVOL", "RSI": "RSI", "vs20DH": "vs 20D High %",
        "vs52WH": "vs 52W High %", "vsEMA20": "vs EMA20 %", "Squeeze": "Squeeze pctl", "RS3M": "3M RS",
        "ATRpct": "ATR %", "TurnCr": "Turnover Cr", "Score": "Score", "Chart": "TradingView", "Strategies": "Strategies"}

GROUPS = {
    "Screener columns": ["LTP", "Chg %", "RVOL", "RSI", "vs 20D High %", "vs 52W High %", "vs EMA20 %",
                         "Squeeze pctl", "3M RS", "ATR %", "Turnover Cr", "Score", "TradingView", "Strategies"],
    "Core terms": ["EMA", "Uptrend filter", "ATR", "RSI(2)", "20D high", "52W high", "Relative strength"],
    "Setups and strategies": ["Pre-breakout", "Breakout", "Pullback", "Momentum", "Mean reversion",
                              "Trend template (VCP-lite)", "Weekly trend breakout", "Short swing", "IBS"],
    "Sidebar controls": ["Universe", "Custom symbols", "Preset", "Project volume", "Auto-refresh"],
    "Backtest settings": ["Backtest", "Strategy", "Years of history", "Cost + slippage", "Stop (x ATR)", "Target (R)",
                          "Max hold", "Inspect"],
    "Backtest results": ["R", "Trades", "Win %", "Avg R", "Avg ret %", "Profit factor", "Avg bars", "Total R",
                         "Max DD (R)", "Next-day up %", "Avg next-day %", "Next day %", "Cumulative R", "Year", "Symbol", "Entry", "Exit", "Entry px", "Exit px", "Bars",
                         "Ret %", "Exit reason"],
    "Replay and paper trading": ["Replay scope", "Replay window", "Paper trade", "Opened", "Closed", "Mark px", "Unrealized R", "Alert", "Stop", "Target"],
    "News": ["News theme", "News lookback", "Net", "Up", "Down", "Themes"],
    "Today page": ["Regime", "Risk per trade", "Tested edge", "Fit", "Verdict", "Buy near", "Risk/sh", "Qty", "Capital", "Hold d"],
    "Portfolio backtest and data": ["Portfolio backtest", "Risk % per trade", "Max open positions", "Position cap %", "Slippage %", "Charges", "Out-of-sample", "CAGR %", "Max drawdown %", "Sharpe", "Calmar", "Avg exposure %", "Data store", "Data health"],
    "Journal and risk": ["Journal", "Account size", "Events", "Risk now (Rs)", "Unreal P&L (Rs)", "Unreal %", "Value (Rs)", "Sector exposure", "Expectancy %", "Avg win %", "Avg loss %", "Net P&L (Rs)", "Avg days"],
    "Broker and algo rules": ["Algo rules", "Static IP", "Password gate", "Dry run", "Order caps", "Reconcile", "GTT OCO"],
    "Cautions": ["Survivorship bias", "Overfitting", "Fills", "STCG"],
}


def header_html(label, tip):
    """Table header with a hover tooltip, for the plain-HTML fallback table."""
    return (f'<span title="{html.escape(tip, quote=True)}" style="cursor:help">'
            f'{html.escape(label)} <span style="opacity:.6">&#9432;</span></span>')


def render_glossary():
    st.title("Glossary")
    st.caption("Plain-English meanings of the terms in the screener and backtest. The same text appears when you hover "
               "the (?) icons next to filters and table headers.")
    q = st.text_input("Search a term", placeholder="e.g. ATR, drawdown, squeeze").strip().lower()
    for group, keys in GROUPS.items():
        items = [(k, T[k]) for k in keys if not q or q in k.lower() or q in T[k].lower()]
        if items:
            st.subheader(group)
            for k, v in items:
                st.markdown(f"**{k}** - {v}")
