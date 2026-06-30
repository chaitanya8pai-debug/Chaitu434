import numpy as np
import pandas as pd
import logging
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings("ignore")

import yfinance as yf
from strategy_engine import StrategyOrchestrator, Asset, Signal, Regime

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("Backtester")

# ── Fetch Data ──────────────────────────────────────────────
def fetch_all(years=2, interval="1d"):
    tickers = {Asset.GOLD: "GC=F", Asset.BTC: "BTC-USD", Asset.ETH: "ETH-USD"}
    end   = datetime.now()
    start = end - timedelta(days=365 * years)
    data  = {}
    for asset, ticker in tickers.items():
        log.info(f"Fetching {ticker}...")
        try:
            df = yf.download(ticker, start=start.strftime("%Y-%m-%d"),
                             end=end.strftime("%Y-%m-%d"), auto_adjust=True, progress=False)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df.columns = [c.lower() for c in df.columns]
            df = df[["open","high","low","close","volume"]].dropna()
            df.index = pd.to_datetime(df.index, utc=True)
            data[asset] = df
            log.info(f"  {ticker}: {len(df)} rows | {df.index[0].date()} to {df.index[-1].date()}")
        except Exception as e:
            log.error(f"Failed {ticker}: {e}")
    return data

# ── Trade Record ────────────────────────────────────────────
class Trade:
    def __init__(self, tid, asset, direction, entry_time, entry, sl, tp, size, capital):
        self.tid=tid; self.asset=asset; self.direction=direction
        self.entry_time=entry_time; self.entry=entry; self.sl=sl; self.tp=tp
        self.size=size; self.capital=capital
        self.exit_time=None; self.exit_price=None; self.reason=None
        self.pnl_pct=None; self.pnl_abs=None; self.regime=None; self.confidence=0

# ── Backtester ──────────────────────────────────────────────
class Backtester:
    SLIPPAGE   = 0.001
    COMMISSION = 0.0005
    LOOKBACK   = 60

    def __init__(self, capital=500000, win_rate=0.52):
        self.initial = capital
        self.capital = capital
        self.peak    = capital
        self.trades  = []
        self.equity  = []
        self.open_trades = {}
        self.tid     = 0
        self.win_rate = win_rate

    def _close(self, trade, exit_price, ts, reason):
        slip  = exit_price * self.SLIPPAGE
        exit_p = exit_price - slip if trade.direction=="LONG" else exit_price + slip
        deployed = trade.size * trade.capital
        commission = deployed * self.COMMISSION * 2
        if trade.direction == "LONG":
            pnl_pct = (exit_p - trade.entry) / trade.entry
        else:
            pnl_pct = (trade.entry - exit_p) / trade.entry
        pnl_pct -= commission / deployed
        pnl_abs = pnl_pct * deployed
        trade.exit_time=ts; trade.exit_price=exit_p
        trade.reason=reason; trade.pnl_pct=pnl_pct; trade.pnl_abs=pnl_abs
        self.capital += pnl_abs
        self.peak = max(self.peak, self.capital)

    def run(self, data):
        assets = list(data.keys())
        idx = data[assets[0]].index
        for a in assets[1:]:
            idx = idx.intersection(data[a].index)
        log.info(f"Running backtest on {len(idx)} days across {len(assets)} assets...")
        orch = StrategyOrchestrator(capital=self.initial, win_rate=self.win_rate)

        for i in range(self.LOOKBACK, len(idx)):
            ts = idx[i]
            slices = {a: data[a].loc[idx[:i+1]].tail(100) for a in assets}

            # Check exits
            for asset, trade in list(self.open_trades.items()):
                candle = slices[asset].iloc[-1]
                if trade.direction=="LONG":
                    if candle["low"]  <= trade.sl: self._close(trade, trade.sl, ts, "SL"); del self.open_trades[asset]; continue
                    if candle["high"] >= trade.tp: self._close(trade, trade.tp, ts, "TP"); del self.open_trades[asset]; continue
                else:
                    if candle["high"] >= trade.sl: self._close(trade, trade.sl, ts, "SL"); del self.open_trades[asset]; continue
                    if candle["low"]  <= trade.tp: self._close(trade, trade.tp, ts, "TP"); del self.open_trades[asset]; continue

            # Get signals
            gdf = slices.get(Asset.GOLD, pd.DataFrame())
            bdf = slices.get(Asset.BTC,  pd.DataFrame())
            edf = slices.get(Asset.ETH,  pd.DataFrame())
            if gdf.empty or bdf.empty or edf.empty: continue

            try:
                signals = orch.evaluate(gdf, bdf, edf, current_portfolio_value=self.capital)
            except: continue

            for sig in signals:
                if sig.asset in self.open_trades or sig.position_size_pct <= 0: continue
                direction = "LONG" if sig.signal.value > 0 else "SHORT"
                entry = sig.entry_price * (1 + self.SLIPPAGE if direction=="LONG" else 1 - self.SLIPPAGE)
                self.tid += 1
                t = Trade(self.tid, sig.asset, direction, ts, entry,
                          sig.stop_loss, sig.take_profit, sig.position_size_pct, self.capital)
                t.regime = sig.regime; t.confidence = sig.confidence
                self.open_trades[sig.asset] = t
                self.trades.append(t)

            dd = (self.peak - self.capital) / self.peak
            self.equity.append({"date": str(ts.date()), "value": round(self.capital,0), "drawdown": round(dd*100,2)})

        # Force close remaining
        for asset, trade in self.open_trades.items():
            last_price = data[asset]["close"].iloc[-1]
            self._close(trade, last_price, idx[-1], "EOD")

        return self

    def report(self):
        closed = [t for t in self.trades if t.pnl_abs is not None]
        if not closed:
            print("No closed trades to report.")
            return

        wins     = [t for t in closed if t.pnl_abs > 0]
        losses   = [t for t in closed if t.pnl_abs <= 0]
        win_rate = len(wins) / len(closed)
        gp = sum(t.pnl_abs for t in wins)
        gl = abs(sum(t.pnl_abs for t in losses))
        pf = gp / gl if gl > 0 else float("inf")

        eq = pd.DataFrame(self.equity)
        final_val = eq["value"].iloc[-1] if not eq.empty else self.capital
        total_ret = (final_val - self.initial) / self.initial
        years = len(eq) / 365
        cagr  = (final_val / self.initial) ** (1/years) - 1 if years > 0 else 0
        max_dd = eq["drawdown"].max() / 100 if not eq.empty else 0

        rets = eq["value"].pct_change().dropna()
        rf   = 0.065 / 365
        sharpe = ((rets - rf).mean() / (rets - rf).std()) * np.sqrt(365) if rets.std() > 0 else 0
        down   = (rets - rf)[rets < rf].std()
        sortino = ((rets - rf).mean() / down) * np.sqrt(365) if down > 0 else 0
        calmar = cagr / max_dd if max_dd > 0 else 0
        avg_win  = np.mean([t.pnl_pct for t in wins]) if wins else 0
        avg_loss = np.mean([t.pnl_pct for t in losses]) if losses else 0

        DIV = "=" * 58
        print(f"\n{DIV}")
        print("  BACKTEST PERFORMANCE REPORT")
        print(DIV)
        print(f"  Period : {eq.index[0] if not eq.empty else 'N/A'} — 2 years")
        print(f"  Capital: ₹{self.initial:,.0f}  →  ₹{final_val:,.0f}")
        print(f"\n{'─'*58}")
        print("  RETURNS")
        print(f"{'─'*58}")
        print(f"  Total Return : {total_ret:+.2%}")
        print(f"  CAGR         : {cagr:+.2%}")
        print(f"\n{'─'*58}")
        print("  RISK")
        print(f"{'─'*58}")
        print(f"  Sharpe Ratio : {sharpe:.3f}   (target > 1.5)")
        print(f"  Sortino Ratio: {sortino:.3f}   (target > 2.0)")
        print(f"  Calmar Ratio : {calmar:.3f}   (target > 1.0)")
        print(f"  Max Drawdown : {max_dd:.2%}  (target < 15%)")
        print(f"\n{'─'*58}")
        print("  TRADES")
        print(f"{'─'*58}")
        print(f"  Total Trades : {len(closed)}")
        print(f"  Win Rate     : {win_rate:.2%}   (target > 45%)")
        print(f"  Profit Factor: {pf:.3f}   (target > 1.5)")
        print(f"  Avg Win      : {avg_win:+.2%}")
        print(f"  Avg Loss     : {avg_loss:+.2%}")
        print(f"\n{'─'*58}")
        print("  PER ASSET")
        print(f"{'─'*58}")
        for asset in Asset:
            at = [t for t in closed if t.asset == asset]
            if not at: continue
            aw = [t for t in at if t.pnl_abs > 0]
            print(f"  {asset.value:10s}  Trades:{len(at):3d}  "
                  f"Win:{len(aw)/len(at):.0%}  "
                  f"P&L: ₹{sum(t.pnl_abs for t in at):>10,.0f}")
        print(f"\n{'─'*58}")
        print("  GO / NO-GO CHECKLIST")
        print(f"{'─'*58}")
        checks = [
            ("Sharpe > 1.5",     sharpe > 1.5),
            ("Max DD < 15%",     max_dd < 0.15),
            ("Win Rate > 45%",   win_rate > 0.45),
            ("Profit Factor>1.5",pf > 1.5),
            ("CAGR > 15%",       cagr > 0.15),
            ("Calmar > 1.0",     calmar > 1.0),
            ("Positive Return",  total_ret > 0),
            ("Trades > 30",      len(closed) > 30),
        ]
        score = sum(1 for _, p in checks if p)
        for label, passed in checks:
            print(f"  {'✅' if passed else '❌'}  {label}")
        grade = ("🟢 EXCELLENT" if score>=7 else "🟡 GOOD" if score>=5
                 else "🟠 MARGINAL" if score>=3 else "🔴 FAIL")
        print(f"\n  Score: {score}/8   Grade: {grade}")
        print(f"\n{DIV}\n")

        eq.to_csv("equity_curve.csv", index=False)
        log.info("Saved equity_curve.csv")

if __name__ == "__main__":
    print("\n" + "="*58)
    print("  BACKTEST ENGINE — Starting")
    print("="*58 + "\n")
    data = fetch_all(years=2, interval="1d")
    if len(data) < 2:
        print("❌ Not enough data. Check internet.")
    else:
        bt = Backtester(capital=500000, win_rate=0.52)
        bt.run(data)
        bt.report()
