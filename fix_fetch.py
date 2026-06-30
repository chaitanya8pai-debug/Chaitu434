import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta

def get_data(ticker, years=2):
    end   = datetime.now()
    start = end - timedelta(days=365 * years)
    df = yf.download(ticker, start=start.strftime("%Y-%m-%d"),
                     end=end.strftime("%Y-%m-%d"), auto_adjust=True, progress=False)
    # Fix for new yfinance multi-level columns
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [c.lower() for c in df.columns]
    df = df[["open","high","low","close","volume"]].dropna()
    print(f"✅ {ticker}: {len(df)} rows | {df.index[0].date()} → {df.index[-1].date()}")
    print(df.tail(3))
    print()

print("Testing data fetch...\n")
get_data("GC=F")
get_data("BTC-USD")
get_data("ETH-USD")
print("🎉 All working! Data is fetching correctly.")
