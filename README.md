# NSE Swing Screener

Streamlit screener for NSE stocks using Yahoo Finance data (about 15 minutes delayed).

## Run

    py -3 -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt
    .\.venv\Scripts\python.exe -m streamlit run screener.py

## Notes

- Data is delayed ~15 min. The last daily candle is today's partial bar during market hours.
- Edit the NIFTY50 / MIDCAPS lists in `screener.py` or use the Custom box in the sidebar.
- `tzdata` is required on Windows for the Asia/Kolkata timezone.
- Screener output is a filter and ranking aid, not trading advice.