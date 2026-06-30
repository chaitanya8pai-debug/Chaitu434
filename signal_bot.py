
import time, requests, os, warnings
import pandas as pd
import yfinance as yf
from datetime import datetime
from dotenv import load_dotenv
warnings.filterwarnings("ignore")

load_dotenv("/Users/vinayaka/Desktop/trading-bot/.env")

TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg}, timeout=5)
        print("Telegram sent!")
    except Exception as e:
        print(f"Telegram error: {e}")

def fetch(ticker):
    df = yf.download(ticker, period="6mo", auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [c.lower() for c in df.columns]
    df.index = pd.to_datetime(df.index, utc=True)
    return df[["open","high","low","close","volume"]].dropna()

def scan():
    now = datetime.now().strftime("%d %b %H:%M")
    print(f"Scanning at {now}...")
    try:
        from strategy_engine import StrategyOrchestrator
        gold = fetch("GC=F"); brent = fetch("BZ=F")
        btc  = fetch("BTC-USD")
        eth  = fetch("ETH-USD")
        gp = gold["close"].iloc[-1]
        bp = btc["close"].iloc[-1]
        ep = eth["close"].iloc[-1]
        orch = StrategyOrchestrator(capital=500000, win_rate=0.55)
        sigs = orch.evaluate(gold, btc, eth, current_portfolio_value=500000)
        if not sigs:
            msg = (
                "No Trade Signals - " + now +
                "\nGold: $" + f"{gp:,.0f}" +
                " | BTC: $" + f"{bp:,.0f}" +
                " | ETH: $" + f"{ep:,.0f}" +
                "\nMarkets watched. No setup found. Next scan in 1 hour."
            )
            print("No signals this scan.")
            send(msg)
            return
        for s in sigs:
            direction = "BUY" if s.signal.value > 0 else "SELL"
            cap = s.position_size_pct * 500000
            msg = (
                direction + " SIGNAL: " + s.asset.value +
                "\nConfidence: " + f"{s.confidence:.0%}" +
                "\nRegime: " + s.regime.value +
                "\nEntry: " + f"{s.entry_price:,.2f}" +
                "\nStop Loss: " + f"{s.stop_loss:,.2f}" +
                "\nTake Profit: " + f"{s.take_profit:,.2f}" +
                "\nCapital to deploy: Rs" + f"{cap:,.0f}" +
                "\n[PAPER TRADE - No real money placed]"
            )
            print(f"Signal: {s.asset.value} {direction}")
            send(msg)
    except Exception as e:
        print(f"Scan error: {e}")
        send("Bot scan error: " + str(e))

print("Starting bot...")
send("Trading Bot LIVE! Watching Gold, BTC and ETH every hour. Paper trade mode.")
while True:
    scan()
    print("Sleeping 1 hour...")
    time.sleep(3600)
