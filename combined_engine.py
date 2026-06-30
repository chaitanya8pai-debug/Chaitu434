"""
=============================================================
  COMBINED STRATEGY ENGINE
  Original Engine → finds direction (BUY/SELL)
  Smart Money Engine → confirms entry quality
  Only trades when BOTH agree
=============================================================

HOW IT WORKS:
  Step 1: Original engine scans for signal (EMA, RSI, VWAP, Volume)
  Step 2: Smart Money confirms with 6 institutional factors:
          - Order Flow (Volume Delta)
          - Market Structure (HH/HL or LH/LL)
          - Order Block proximity
          - VWAP position
          - Liquidity Sweep
          - Premium/Discount zone
  Step 3: Only trade if both agree → Higher quality entries
=============================================================
"""

import numpy as np
import pandas as pd
import warnings
warnings.filterwarnings("ignore")


# ─────────────────────────────────────────────
# STEP 1: ORIGINAL ENGINE SIGNAL
# ─────────────────────────────────────────────

def original_signal(df):
    """
    Fast multi-factor signal — same as our proven strategy engine.
    Returns: direction ("BUY"/"SELL"/"NEUTRAL"), confidence (0-1)
    """
    close = df["close"]; vol = df["volume"]

    ema9  = close.ewm(span=9).mean()
    ema21 = close.ewm(span=21).mean()
    ema50 = close.ewm(span=50).mean()

    delta = close.diff()
    gain  = delta.clip(lower=0).ewm(span=14).mean()
    loss  = (-delta.clip(upper=0)).ewm(span=14).mean()
    rsi   = 100 - (100 / (1 + gain / (loss + 1e-9)))

    tp   = (df["high"] + df["low"] + close) / 3
    vwap = (tp * vol).cumsum() / vol.cumsum()

    hl       = (df["high"] - df["low"]).replace(0, 1e-9)
    dv       = ((close - df["low"]) / hl - (df["high"] - close) / hl) * vol
    cum_delta = dv.rolling(20).sum()

    vol_trend = vol.rolling(10).mean() / vol.rolling(30).mean()

    cur = float(close.iloc[-1])
    score = (
        (2 if ema9.iloc[-1]  > ema21.iloc[-1] else -2) +
        (1 if cur            > ema50.iloc[-1] else -1) +
        (1 if rsi.iloc[-1]   > 55             else -1 if rsi.iloc[-1] < 45 else 0) +
        (1 if cur            > vwap.iloc[-1]  else -1) +
        (1 if cum_delta.iloc[-1] > 0          else -1) +
        (1 if vol_trend.iloc[-1] > 1.1        else 0)
    )

    conf = abs(score) / 7.0

    if score >= 4:    return "BUY",  round(conf, 2)
    elif score <= -4: return "SELL", round(conf, 2)
    return "NEUTRAL", 0.0


# ─────────────────────────────────────────────
# STEP 2: SMART MONEY CONFIRMATION
# ─────────────────────────────────────────────

def smart_money_confirm(df, direction):
    """
    Confirms the original engine's signal using 6 smart money factors.
    Returns: confirmed (bool), sm_score (0-6), reasons (list)
    """
    close = df["close"]; high = df["high"]
    low   = df["low"];   vol  = df["volume"]; op = df["open"]

    reasons = []
    score   = 0

    # ── Factor 1: Order Flow (Volume Delta) ──
    hl       = (high - low).replace(0, 1e-9)
    dv       = ((close - low) / hl - (high - close) / hl) * vol
    cum_delta = dv.rolling(20).sum()
    of_bull  = float(cum_delta.iloc[-1]) > 0

    if direction == "BUY" and of_bull:
        score += 1
        reasons.append("✅ Order Flow: Buyers dominant (bullish delta)")
    elif direction == "SELL" and not of_bull:
        score += 1
        reasons.append("✅ Order Flow: Sellers dominant (bearish delta)")
    else:
        reasons.append("❌ Order Flow: Against signal direction")

    # ── Factor 2: Market Structure (HH/HL or LH/LL) ──
    sh   = high.rolling(5, center=True).max() == high
    sl_  = low.rolling(5, center=True).min() == low
    lsh  = high.where(sh).ffill()
    lsl  = low.where(sl_).ffill()
    hh   = float(lsh.iloc[-1]) > float(lsh.shift(5).iloc[-1])
    hl_s = float(lsl.iloc[-1]) > float(lsl.shift(5).iloc[-1])

    if direction == "BUY" and hh and hl_s:
        score += 1
        reasons.append("✅ Market Structure: HH + HL (bullish)")
    elif direction == "SELL" and not hh and not hl_s:
        score += 1
        reasons.append("✅ Market Structure: LH + LL (bearish)")
    else:
        reasons.append("❌ Market Structure: Not aligned")

    # ── Factor 3: Order Block Proximity ──
    body      = abs(close - op)
    spread    = (high - low).replace(0, 1e-9)
    body_ratio = body / spread
    rvol      = vol / vol.rolling(20).mean()
    bull_ob   = (close.shift(3) > op.shift(3)) & (rvol.shift(3) > 1.5) & (body_ratio.shift(3) > 0.6)
    bear_ob   = (close.shift(3) < op.shift(3)) & (rvol.shift(3) > 1.5) & (body_ratio.shift(3) > 0.6)
    at_bull_ob = bool(bull_ob.iloc[-1]) or bool(bull_ob.iloc[-2])
    at_bear_ob = bool(bear_ob.iloc[-1]) or bool(bear_ob.iloc[-2])

    if direction == "BUY" and at_bull_ob:
        score += 1
        reasons.append("✅ Order Block: Price at institutional buy zone")
    elif direction == "SELL" and at_bear_ob:
        score += 1
        reasons.append("✅ Order Block: Price at institutional sell zone")
    else:
        reasons.append("⚪ Order Block: Not at key OB level")

    # ── Factor 4: VWAP Position ──
    tp   = (high + low + close) / 3
    vwap = (tp * vol).cumsum() / vol.cumsum()
    std  = (tp - vwap).rolling(20).std()
    cur  = float(close.iloc[-1])
    vwap_val = float(vwap.iloc[-1])
    std_val  = float(std.iloc[-1])

    if direction == "BUY" and cur < vwap_val + std_val:    # Below VWAP+1σ = good buy
        score += 1
        reasons.append(f"✅ VWAP: Price in buy zone (below VWAP+1σ)")
    elif direction == "SELL" and cur > vwap_val - std_val:  # Above VWAP-1σ = good sell
        score += 1
        reasons.append(f"✅ VWAP: Price in sell zone (above VWAP-1σ)")
    else:
        reasons.append("❌ VWAP: Price extended — not ideal entry")

    # ── Factor 5: Liquidity Sweep (Stop Hunt) ──
    recent_high = float(high.rolling(10).max().shift(1).iloc[-1])
    recent_low  = float(low.rolling(10).min().shift(1).iloc[-1])
    last_bar    = df.iloc[-1]
    sweep_low   = float(last_bar["low"]) < recent_low and cur > recent_low
    sweep_high  = float(last_bar["high"]) > recent_high and cur < recent_high

    if direction == "BUY" and sweep_low:
        score += 1
        reasons.append("✅ Liquidity Sweep: Sell-side swept — smart money bought!")
    elif direction == "SELL" and sweep_high:
        score += 1
        reasons.append("✅ Liquidity Sweep: Buy-side swept — smart money sold!")
    else:
        reasons.append("⚪ No recent liquidity sweep")

    # ── Factor 6: Premium / Discount Zone ──
    rng_high = float(high.rolling(50).max().iloc[-1])
    rng_low  = float(low.rolling(50).min().iloc[-1])
    pct      = (cur - rng_low) / (rng_high - rng_low + 1e-9) * 100
    in_discount = pct < 40
    in_premium  = pct > 60

    if direction == "BUY" and in_discount:
        score += 1
        reasons.append(f"✅ Zone: Price in DISCOUNT ({pct:.0f}%) — smart money buys here")
    elif direction == "SELL" and in_premium:
        score += 1
        reasons.append(f"✅ Zone: Price in PREMIUM ({pct:.0f}%) — smart money sells here")
    else:
        reasons.append(f"⚪ Zone: Price at {pct:.0f}% of range (neutral zone)")

    # ── Confirmation Threshold ──
    # Need 4 out of 6 factors to agree (67% confluence)
    confirmed = score >= 4

    return confirmed, score, reasons


# ─────────────────────────────────────────────
# COMBINED ENGINE — MAIN FUNCTION
# ─────────────────────────────────────────────

def combined_signal(df, asset_name=""):
    """
    Full combined engine evaluation.
    Returns signal dict or None if no trade.
    """
    if len(df) < 60:
        return None

    # Step 1: Original engine finds direction
    direction, orig_conf = original_signal(df)

    if direction == "NEUTRAL" or orig_conf < 0.45:
        return None

    # Step 2: Smart Money confirms entry
    confirmed, sm_score, reasons = smart_money_confirm(df, direction)

    if not confirmed:
        return None

    # Step 3: Compute smart SL/TP using swing levels
    close = df["close"]; high = df["high"]; low = df["low"]
    tr    = pd.concat([high-low, (high-close.shift()).abs(), (low-close.shift()).abs()], axis=1).max(axis=1)
    atr   = float(tr.ewm(span=14).mean().iloc[-1])
    cur   = float(close.iloc[-1])

    # Find swing points for natural SL/TP
    recent = df.tail(20)
    swings_h = []; swings_l = []
    for i in range(2, len(recent)-2):
        if recent["high"].iloc[i] == recent["high"].iloc[i-2:i+3].max():
            swings_h.append(float(recent["high"].iloc[i]))
        if recent["low"].iloc[i] == recent["low"].iloc[i-2:i+3].min():
            swings_l.append(float(recent["low"].iloc[i]))

    if direction == "BUY":
        valid_lows   = [l for l in swings_l if l < cur]
        sl = (max(valid_lows) - 0.5*atr) if valid_lows else cur - 1.5*atr
        valid_highs  = [h for h in swings_h if h > cur]
        risk  = cur - sl
        tp = (min(valid_highs) if valid_highs and (min(valid_highs)-cur)/risk >= 2
              else cur + 2.5*risk)
    else:
        valid_highs  = [h for h in swings_h if h > cur]
        sl = (min(valid_highs) + 0.5*atr) if valid_highs else cur + 1.5*atr
        valid_lows   = [l for l in swings_l if l < cur]
        risk  = sl - cur
        tp = (max(valid_lows) if valid_lows and (cur-max(valid_lows))/risk >= 2
              else cur - 2.5*risk)

    risk   = abs(cur - sl)
    reward = abs(tp - cur)
    rr     = round(reward / risk, 2) if risk > 0 else 0

    if rr < 1.8:
        return None   # Skip poor R:R setups

    # Combined confidence = average of both engines
    combined_conf = round((orig_conf + sm_score/6) / 2, 2)

    return {
        "direction":   direction,
        "confidence":  combined_conf,
        "orig_conf":   orig_conf,
        "sm_score":    sm_score,
        "sm_reasons":  reasons,
        "entry":       round(cur, 4),
        "stop_loss":   round(sl, 4),
        "take_profit": round(tp, 4),
        "atr":         round(atr, 4),
        "rr":          rr,
        "asset":       asset_name,
    }


# ─────────────────────────────────────────────
# BACKTEST
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import yfinance as yf
    from datetime import datetime, timedelta

    def fetch(ticker, years=2):
        end   = datetime.now()
        start = end - timedelta(days=365*years)
        df = yf.download(ticker, start=start.strftime("%Y-%m-%d"),
                         end=end.strftime("%Y-%m-%d"), auto_adjust=True, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]
        df.index   = pd.to_datetime(df.index, utc=True)
        return df[["open","high","low","close","volume"]].dropna()

    print("\n" + "="*62)
    print("  COMBINED ENGINE BACKTEST")
    print("  Original Engine (Direction) + Smart Money (Confirmation)")
    print("="*62)
    print("\nFetching 2 years of data...")

    gold = fetch("GC=F"); btc = fetch("BTC-USD"); eth = fetch("ETH-USD")
    print(f"Gold:{len(gold)} | BTC:{len(btc)} | ETH:{len(eth)} days\n")
    print("Running Combined Engine on every single day...")
    print("(Takes ~60 seconds)\n")

    CAPITAL    = 500000
    capital    = CAPITAL
    peak       = CAPITAL
    all_trades = []
    LOOKBACK   = 60

    for name, df in [("XAUUSD", gold), ("BTCUSDT", btc), ("ETHUSDT", eth)]:
        in_trade = False
        asset_trades = []

        for i in range(LOOKBACK, len(df)):
            window = df.iloc[:i+1].tail(100)

            if not in_trade:
                sig = combined_signal(window, name)
                if sig:
                    entry     = sig["entry"] * (1.001 if sig["direction"]=="BUY" else 0.999)
                    sl        = sig["stop_loss"]
                    tp        = sig["take_profit"]
                    size      = 0.10 * capital
                    direction = sig["direction"]
                    in_trade  = True

            else:
                bar    = df.iloc[i]
                reason = None
                if direction == "BUY":
                    if float(bar["low"])  <= sl: reason = "SL"
                    elif float(bar["high"]) >= tp: reason = "TP"
                else:
                    if float(bar["high"]) >= sl: reason = "SL"
                    elif float(bar["low"])  <= tp: reason = "TP"

                if reason or i == len(df)-1:
                    ep      = sl if reason=="SL" else tp if reason else float(bar["close"])
                    reason  = reason or "EOD"
                    pnl_pct = (ep-entry)/entry if direction=="BUY" else (entry-ep)/entry
                    pnl_pct -= 0.001
                    pnl_abs  = pnl_pct * size
                    capital  += pnl_abs
                    peak      = max(peak, capital)
                    t = {"asset":name,"dir":direction,"reason":reason,
                         "pnl_pct":pnl_pct,"pnl_abs":pnl_abs}
                    all_trades.append(t)
                    asset_trades.append(t)
                    in_trade = False

        wins = [t for t in asset_trades if t["pnl_abs"]>0]
        pnl  = sum(t["pnl_abs"] for t in asset_trades)
        wr   = len(wins)/len(asset_trades)*100 if asset_trades else 0
        print(f"  {name}: {len(asset_trades)} trades | Win:{wr:.0f}% | PnL:Rs{pnl:+,.0f}")

    # Metrics
    wins      = [t for t in all_trades if t["pnl_abs"] > 0]
    losses    = [t for t in all_trades if t["pnl_abs"] <= 0]
    total_ret = (capital - CAPITAL) / CAPITAL
    cagr      = (capital / CAPITAL) ** 0.5 - 1
    wr        = len(wins) / len(all_trades) if all_trades else 0
    gp        = sum(t["pnl_abs"] for t in wins)
    gl        = abs(sum(t["pnl_abs"] for t in losses))
    pf        = gp / gl if gl > 0 else 999
    max_dd    = (peak - capital) / peak

    D = "="*62
    print(f"\n{D}")
    print("  COMBINED ENGINE — RESULTS")
    print(D)
    print(f"  Capital:       Rs{CAPITAL:,.0f}  ->  Rs{capital:,.0f}")
    print(f"\n  RETURNS")
    print(f"  Total Return:  {total_ret:+.2%}")
    print(f"  CAGR:          {cagr:+.2%}")
    print(f"\n  RISK")
    print(f"  Max Drawdown:  {max_dd:.2%}   (target < 15%)")
    print(f"\n  TRADES")
    print(f"  Total Trades:  {len(all_trades)}")
    print(f"  Win Rate:      {wr:.2%}    (target > 45%)")
    print(f"  Profit Factor: {pf:.3f}    (target > 1.5)")
    if wins:   print(f"  Avg Win:       +{sum(t['pnl_pct'] for t in wins)/len(wins)*100:.2f}%")
    if losses: print(f"  Avg Loss:      {sum(t['pnl_pct'] for t in losses)/len(losses)*100:.2f}%")
    print(f"\n  PER ASSET")
    for a in ["XAUUSD","BTCUSDT","ETHUSDT"]:
        at = [t for t in all_trades if t["asset"]==a]
        if not at: continue
        aw = [t for t in at if t["pnl_abs"]>0]
        print(f"  {a:10s} Trades:{len(at):3d}  Win:{len(aw)/len(at):.0%}  PnL:Rs{sum(t['pnl_abs'] for t in at):>10,.0f}")
    print(f"\n  EXIT BREAKDOWN")
    for r in ["TP","SL","EOD"]:
        rt = [t for t in all_trades if t["reason"]==r]
        if rt:
            rw = [t for t in rt if t["pnl_abs"]>0]
            print(f"  {r:6s} {len(rt):3d} trades | Win:{len(rw)/len(rt):.0%} | PnL:Rs{sum(t['pnl_abs'] for t in rt):>10,.0f}")

    checks = [
        ("Max DD < 15%",    max_dd < 0.15),
        ("Win Rate > 45%",  wr > 0.45),
        ("PF > 1.5",        pf > 1.5),
        ("Positive Return", total_ret > 0),
        ("CAGR > 15%",      cagr > 0.15),
        ("Trades > 20",     len(all_trades) > 20),
    ]
    score = sum(1 for _,p in checks if p)
    print(f"\n  COMPARISON")
    print(f"  Original Engine:    +11.09% | 48.19% win | GOOD")
    print(f"  Smart Money:         +2.50% | 40.48% win | MARGINAL")
    print(f"  Combined Engine:    {total_ret:+.2%} | {wr:.2%} win | ?")
    print(f"\n  GO / NO-GO")
    for l,p in checks: print(f"  {'pass' if p else 'FAIL'}  {l}")
    grade = "EXCELLENT" if score>=5 else "GOOD" if score>=4 else "MARGINAL" if score>=2 else "FAIL"
    print(f"\n  Score:{score}/6  Grade: {grade}")
    print(f"\n{D}\n")
