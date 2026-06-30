
import numpy as np
import pandas as pd
import logging, warnings
from datetime import datetime, timedelta
warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)

import yfinance as yf
import sys
sys.path.insert(0, "/Users/vinayaka/Desktop/trading-bot")
from smart_money_engine import SmartMoneyOrchestrator, MarketBias

def fetch(ticker, years=2):
    end   = datetime.now()
    start = end - timedelta(days=365*years)
    df = yf.download(ticker, start=start.strftime("%Y-%m-%d"),
                     end=end.strftime("%Y-%m-%d"), auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [c.lower() for c in df.columns]
    df.index = pd.to_datetime(df.index, utc=True)
    return df[["open","high","low","close","volume"]].dropna()

print("Fetching 2 years of data...")
gold = fetch("GC=F")
btc  = fetch("BTC-USD")
eth  = fetch("ETH-USD")
print(f"Gold: {len(gold)} days | BTC: {len(btc)} days | ETH: {len(eth)} days")

CAPITAL  = 500000
capital  = CAPITAL
peak     = CAPITAL
trades   = []
equity   = [{"date": str(gold.index[0].date()), "value": CAPITAL}]
open_pos = {}
LOOKBACK = 60

assets = {"XAUUSD": gold, "BTCUSDT": btc, "ETHUSDT": eth}
common = gold.index.intersection(btc.index).intersection(eth.index)
print(f"Running backtest on {len(common)} aligned days...")
print("This may take 2-3 minutes...")

tid = 0
for i in range(LOOKBACK, len(common)):
    ts = common[i]
    slices = {a: data.loc[common[:i+1]].tail(100) for a, data in assets.items()}

    # Check exits
    for asset, trade in list(open_pos.items()):
        df  = slices[asset]
        bar = df.iloc[-1]
        reason = None
        if trade["dir"] == "BUY":
            if float(bar["low"])  <= trade["sl"]: reason = "SL"
            if float(bar["high"]) >= trade["tp"]: reason = "TP"
        else:
            if float(bar["high"]) >= trade["sl"]: reason = "SL"
            if float(bar["low"])  <= trade["tp"]: reason = "TP"
        if reason:
            ep = trade["sl"] if reason == "SL" else trade["tp"]
            pnl_pct = (ep - trade["entry"]) / trade["entry"] if trade["dir"] == "BUY" else (trade["entry"] - ep) / trade["entry"]
            pnl_pct -= 0.001  # commission
            pnl_abs = pnl_pct * trade["capital"]
            capital += pnl_abs
            peak = max(peak, capital)
            trades.append({"asset": asset, "dir": trade["dir"], "reason": reason,
                          "pnl_pct": pnl_pct, "pnl_abs": pnl_abs,
                          "entry": trade["entry"], "exit": ep})
            del open_pos[asset]
            equity.append({"date": str(ts.date()), "value": round(capital, 0)})

    # Scan signals every 5 days (weekly) to save time
    if i % 5 != 0:
        continue

    try:
        orch = SmartMoneyOrchestrator(capital=capital, win_rate=0.55)
        sigs = orch.evaluate_all(slices["XAUUSD"], slices["BTCUSDT"], slices["ETHUSDT"])
        for s in sigs:
            if s.asset in open_pos:
                continue
            direction = "BUY" if s.bias in [MarketBias.BULLISH, MarketBias.STRONG_BULLISH] else "SELL"
            entry = s.entry_price * (1.001 if direction == "BUY" else 0.999)
            tid += 1
            open_pos[s.asset] = {
                "dir": direction, "entry": entry,
                "sl": s.stop_loss, "tp": s.take_profit,
                "capital": s.position_size_pct * capital
            }
    except: pass

# Force close remaining
for asset, trade in open_pos.items():
    df  = slices[asset]
    ep  = float(df["close"].iloc[-1])
    pnl_pct = (ep - trade["entry"]) / trade["entry"] if trade["dir"] == "BUY" else (trade["entry"] - ep) / trade["entry"]
    pnl_abs = pnl_pct * trade["capital"]
    capital += pnl_abs
    trades.append({"asset": asset, "dir": trade["dir"], "reason": "EOD",
                  "pnl_pct": pnl_pct, "pnl_abs": pnl_abs})

# Metrics
wins   = [t for t in trades if t["pnl_abs"] > 0]
losses = [t for t in trades if t["pnl_abs"] <= 0]
total_ret = (capital - CAPITAL) / CAPITAL
years = 2
cagr  = (capital / CAPITAL) ** (1/years) - 1
wr    = len(wins) / len(trades) if trades else 0
gp    = sum(t["pnl_abs"] for t in wins)
gl    = abs(sum(t["pnl_abs"] for t in losses))
pf    = gp / gl if gl > 0 else float("inf")
eq_df = pd.DataFrame(equity)
eq_df["ret"] = eq_df["value"].pct_change()
sharpe = ((eq_df["ret"].mean() - 0.065/365) / (eq_df["ret"].std() + 1e-9)) * np.sqrt(365)
max_dd = max((peak - capital) / peak, 0)

D = "=" * 58
print(f"\n{D}")
print("  SMART MONEY BACKTEST — RESULTS")
print(D)
print(f"  Period:        2 years (daily candles)")
print(f"  Capital:       Rs{CAPITAL:,.0f}  ->  Rs{capital:,.0f}")
print(f"\n  RETURNS")
print(f"  Total Return:  {total_ret:+.2%}")
print(f"  CAGR:          {cagr:+.2%}")
print(f"\n  RISK")
print(f"  Sharpe Ratio:  {sharpe:.3f}   (target > 1.5)")
print(f"  Max Drawdown:  {max_dd:.2%}  (target < 15%)")
print(f"\n  TRADES")
print(f"  Total Trades:  {len(trades)}")
print(f"  Win Rate:      {wr:.2%}   (target > 45%)")
print(f"  Profit Factor: {pf:.3f}   (target > 1.5)")
print(f"  Avg Win:       {sum(t['pnl_pct'] for t in wins)/len(wins)*100:.2f}%" if wins else "  Avg Win:       N/A")
print(f"  Avg Loss:      {sum(t['pnl_pct'] for t in losses)/len(losses)*100:.2f}%" if losses else "  Avg Loss:      N/A")
print(f"\n  PER ASSET")
for a in ["XAUUSD","BTCUSDT","ETHUSDT"]:
    at = [t for t in trades if t["asset"]==a]
    if not at: continue
    aw = [t for t in at if t["pnl_abs"]>0]
    print(f"  {a:10s}  Trades:{len(at):3d}  Win:{len(aw)/len(at):.0%}  PnL:Rs{sum(t['pnl_abs'] for t in at):>10,.0f}")
print(f"\n  GO / NO-GO")
checks = [
    ("Sharpe > 1.5",    sharpe > 1.5),
    ("Max DD < 15%",    max_dd < 0.15),
    ("Win Rate > 45%",  wr > 0.45),
    ("Profit Factor>1.5", pf > 1.5),
    ("CAGR > 15%",      cagr > 0.15),
    ("Positive Return", total_ret > 0),
    ("Trades > 20",     len(trades) > 20),
]
score = sum(1 for _,p in checks if p)
for label, passed in checks:
    print(f"  {'pass' if passed else 'FAIL'}  {label}")
grade = "EXCELLENT" if score>=6 else "GOOD" if score>=4 else "MARGINAL" if score>=2 else "FAIL"
print(f"\n  Score: {score}/7   Grade: {grade}")
print(f"\n{D}\n")
