"""
=============================================================
  SMART MONEY STRATEGY ENGINE v2.0
  Indicators used by institutional / smart money traders
=============================================================

MODULES:
  1. OrderFlowAnalyzer        — Volume Delta, Cumulative Delta,
                                Buy/Sell Pressure, Absorption
  2. SmartMoneyConceptsEngine — Break of Structure (BOS),
                                Order Blocks, Fair Value Gaps,
                                Liquidity Sweeps, CHoCH
  3. VolumeProfileAnalyzer    — Point of Control, Value Area,
                                High Volume Nodes
  4. MarketStructureAnalyzer  — HH/HL/LH/LL, Swing detection,
                                Wyckoff phases
  5. InstitutionalLevels      — VWAP bands, Pivot Points,
                                Weekly/Monthly levels
  6. SmartMoneyOrchestrator   — Master signal combiner
=============================================================
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import logging
import warnings
warnings.filterwarnings("ignore")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("SmartMoneyEngine")


# ─────────────────────────────────────────────
# ENUMS
# ─────────────────────────────────────────────

class MarketBias(Enum):
    STRONG_BULLISH = "strong_bullish"
    BULLISH        = "bullish"
    NEUTRAL        = "neutral"
    BEARISH        = "bearish"
    STRONG_BEARISH = "strong_bearish"

class StructureType(Enum):
    HH = "Higher High"
    HL = "Higher Low"
    LH = "Lower High"
    LL = "Lower Low"

class WyckoffPhase(Enum):
    ACCUMULATION  = "Accumulation"    # Smart money buying quietly
    MARKUP        = "Markup"          # Price rising
    DISTRIBUTION  = "Distribution"   # Smart money selling quietly
    MARKDOWN      = "Markdown"        # Price falling
    UNKNOWN       = "Unknown"


# ─────────────────────────────────────────────
# DATA CLASS FOR FINAL SIGNAL
# ─────────────────────────────────────────────

@dataclass
class SmartMoneySignal:
    asset:            str
    bias:             MarketBias
    confidence:       float          # 0.0 – 1.0
    entry_price:      float
    stop_loss:        float
    take_profit:      float
    position_size_pct:float

    # Smart money insights
    wyckoff_phase:    WyckoffPhase   = WyckoffPhase.UNKNOWN
    structure:        str            = ""
    order_block:      Optional[float]= None
    fvg_present:      bool           = False
    liquidity_sweep:  bool           = False
    bos_detected:     bool           = False
    cumulative_delta: float          = 0.0
    volume_poc:       float          = 0.0
    vwap_position:    str            = ""
    reasons:          list           = field(default_factory=list)

    def summary(self):
        d = "🟢 BUY" if self.bias in [MarketBias.BULLISH, MarketBias.STRONG_BULLISH] else \
            "🔴 SELL" if self.bias in [MarketBias.BEARISH, MarketBias.STRONG_BEARISH] else "⚪ NEUTRAL"
        return (
            f"{d} {self.asset} | {self.bias.value} | "
            f"Confidence: {self.confidence:.0%} | "
            f"Entry: {self.entry_price:.2f} | "
            f"SL: {self.stop_loss:.2f} | TP: {self.take_profit:.2f} | "
            f"Size: {self.position_size_pct:.1%}"
        )


# ─────────────────────────────────────────────
# MODULE 1: ORDER FLOW ANALYZER
# ─────────────────────────────────────────────

class OrderFlowAnalyzer:
    """
    Derives order flow from OHLCV data.

    Institutional traders watch:
    - Volume Delta: who is in control (buyers or sellers)
    - Cumulative Delta: trend of buying/selling pressure
    - Absorption: large volume with small price move = smart money absorbing
    - Imbalance candles: huge move on huge volume = institutional entry

    Since we use OHLCV (not tick data), we use proven proxies.
    """

    def volume_delta(self, df: pd.DataFrame) -> pd.Series:
        """
        Estimate buy vs sell volume per candle.
        Formula: Buy vol = (close-low)/(high-low) * volume
                 Sell vol = (high-close)/(high-low) * volume
                 Delta = Buy - Sell
        """
        hl = df['high'] - df['low']
        hl = hl.replace(0, np.nan)
        buy_vol  = ((df['close'] - df['low'])  / hl) * df['volume']
        sell_vol = ((df['high']  - df['close']) / hl) * df['volume']
        return buy_vol - sell_vol

    def cumulative_delta(self, df: pd.DataFrame, window: int = 20) -> pd.Series:
        """Rolling cumulative delta — shows sustained buy/sell pressure."""
        delta = self.volume_delta(df)
        return delta.rolling(window).sum()

    def relative_volume(self, df: pd.DataFrame, window: int = 20) -> pd.Series:
        """RVOL: current volume vs average. >2.0 = institutional activity."""
        avg_vol = df['volume'].rolling(window).mean()
        return df['volume'] / avg_vol

    def detect_absorption(self, df: pd.DataFrame) -> pd.Series:
        """
        Absorption: large volume bar with small body = smart money absorbing supply/demand.
        Signals potential reversal.
        """
        body   = abs(df['close'] - df['open'])
        spread = df['high'] - df['low']
        spread = spread.replace(0, np.nan)
        body_ratio = body / spread   # Small ratio = absorption candle
        rvol       = self.relative_volume(df)
        # Absorption = high volume + small body
        return (rvol > 1.5) & (body_ratio < 0.3)

    def detect_imbalance(self, df: pd.DataFrame) -> pd.Series:
        """
        Imbalance candle: large body + high volume = strong institutional intent.
        """
        body   = abs(df['close'] - df['open'])
        spread = df['high'] - df['low']
        spread = spread.replace(0, np.nan)
        body_ratio = body / spread
        rvol       = self.relative_volume(df)
        return (rvol > 2.0) & (body_ratio > 0.7)

    def buy_sell_pressure(self, df: pd.DataFrame, window: int = 14) -> dict:
        """
        Summarise current order flow conditions.
        Returns dict with actionable insights.
        """
        delta     = self.volume_delta(df)
        cum_delta = self.cumulative_delta(df, window)
        rvol      = self.relative_volume(df)
        absorption= self.detect_absorption(df)
        imbalance = self.detect_imbalance(df)

        latest_delta     = delta.iloc[-1]
        latest_cum       = cum_delta.iloc[-1]
        latest_rvol      = rvol.iloc[-1]
        recent_absorb    = absorption.iloc[-5:].any()
        recent_imbalance = imbalance.iloc[-3:].any()
        bullish_imbalance= (imbalance & (df['close'] > df['open'])).iloc[-3:].any()
        bearish_imbalance= (imbalance & (df['close'] < df['open'])).iloc[-3:].any()

        # Delta trend: is buying or selling accelerating?
        delta_trend = "accelerating_bullish" if (delta.iloc[-3:] > 0).all() else \
                      "accelerating_bearish" if (delta.iloc[-3:] < 0).all() else "mixed"

        return {
            "latest_delta":      round(float(latest_delta), 0),
            "cumulative_delta":  round(float(latest_cum), 0),
            "rvol":              round(float(latest_rvol), 2),
            "absorption":        bool(recent_absorb),
            "imbalance":         bool(recent_imbalance),
            "bullish_imbalance": bool(bullish_imbalance),
            "bearish_imbalance": bool(bearish_imbalance),
            "delta_trend":       delta_trend,
            "score": (
                (2 if latest_cum > 0 else -2) +
                (1 if delta_trend == "accelerating_bullish" else -1 if delta_trend == "accelerating_bearish" else 0) +
                (1 if bullish_imbalance else -1 if bearish_imbalance else 0) +
                (-1 if recent_absorb else 0)   # Absorption near top = bearish warning
            )
        }


# ─────────────────────────────────────────────
# MODULE 2: SMART MONEY CONCEPTS ENGINE
# ─────────────────────────────────────────────

class SmartMoneyConceptsEngine:
    """
    Smart Money Concepts (SMC) — the framework used by ICT,
    institutional desks, and prop traders.

    Key concepts:
    - Break of Structure (BOS): trend confirmation
    - Change of Character (CHoCH): trend reversal warning
    - Order Blocks (OB): institutional supply/demand zones
    - Fair Value Gaps (FVG): price imbalances smart money fills
    - Liquidity Sweeps: stop hunts above/below key levels
    - Premium/Discount: where to buy/sell relative to range
    """

    def detect_swing_points(self, df: pd.DataFrame, strength: int = 3) -> dict:
        """
        Find significant swing highs and lows.
        strength: how many candles each side to confirm swing.
        """
        highs = []
        lows  = []
        n     = len(df)

        for i in range(strength, n - strength):
            if df['high'].iloc[i] == df['high'].iloc[i-strength:i+strength+1].max():
                highs.append({"idx": i, "price": df['high'].iloc[i],
                              "time": df.index[i]})
            if df['low'].iloc[i] == df['low'].iloc[i-strength:i+strength+1].min():
                lows.append({"idx": i, "price": df['low'].iloc[i],
                             "time": df.index[i]})
        return {"highs": highs[-10:], "lows": lows[-10:]}

    def detect_market_structure(self, df: pd.DataFrame) -> dict:
        """
        Determine if market is making HH/HL (bullish) or LH/LL (bearish).
        """
        swings = self.detect_swing_points(df)
        highs  = [s['price'] for s in swings['highs']]
        lows   = [s['price'] for s in swings['lows']]

        if len(highs) < 2 or len(lows) < 2:
            return {"bias": "neutral", "structure": "insufficient data",
                    "bos": False, "choch": False}

        last_hh = highs[-1] > highs[-2]   # Higher High
        last_hl = lows[-1]  > lows[-2]    # Higher Low
        last_lh = highs[-1] < highs[-2]   # Lower High
        last_ll = lows[-1]  < lows[-2]    # Lower Low

        if last_hh and last_hl:
            bias = "bullish"
            structure = f"HH ({highs[-1]:.2f}) + HL ({lows[-1]:.2f})"
        elif last_lh and last_ll:
            bias = "bearish"
            structure = f"LH ({highs[-1]:.2f}) + LL ({lows[-1]:.2f})"
        elif last_hh and last_ll:
            bias = "neutral"
            structure = "Expanding range"
        else:
            bias = "neutral"
            structure = "Mixed structure"

        # Break of Structure: price closes beyond last swing point
        last_close = df['close'].iloc[-1]
        bos_bullish = last_close > max(highs[-3:]) if len(highs) >= 3 else False
        bos_bearish = last_close < min(lows[-3:])  if len(lows)  >= 3 else False
        bos = bos_bullish or bos_bearish

        # Change of Character: after uptrend, first LL = CHoCH
        choch = (last_hl is False and bias == "bullish") or \
                (last_hh is False and bias == "bearish")

        return {
            "bias": bias, "structure": structure,
            "bos": bos, "bos_direction": "bullish" if bos_bullish else "bearish" if bos_bearish else "none",
            "choch": choch,
            "last_swing_high": highs[-1] if highs else 0,
            "last_swing_low":  lows[-1]  if lows  else 0,
        }

    def find_order_blocks(self, df: pd.DataFrame, lookback: int = 20) -> dict:
        """
        Order Block: the last opposing candle before a strong impulse move.
        Bullish OB: last bearish candle before a strong bullish move (institutional buy zone)
        Bearish OB: last bullish candle before a strong bearish move (institutional sell zone)

        Price often returns to OB — that's where smart money re-enters.
        """
        recent = df.tail(lookback).copy()
        close  = recent['close'].values
        open_  = recent['open'].values
        high   = recent['high'].values
        low    = recent['low'].values
        vol    = recent['volume'].values
        avg_vol= np.mean(vol)

        bullish_ob = None
        bearish_ob = None

        # Fast vectorized order block detection
        c = pd.Series(close); o = pd.Series(open_)
        h = pd.Series(high);  l = pd.Series(low)
        v = pd.Series(vol)
        bull_impulse = (c > o) & (c.shift(-1) > o.shift(-1)) & (v > avg_vol * 1.3)
        bear_impulse = (c < o) & (c.shift(-1) < o.shift(-1)) & (v > avg_vol * 1.3)
        bear_candles = c < o
        bull_candles = c > o
        for i in bull_impulse[bull_impulse].index:
            for j in range(i-1, max(0, i-5), -1):
                if j < len(bear_candles) and bear_candles.iloc[j]:
                    bullish_ob = {"high": float(h.iloc[j]), "low": float(l.iloc[j]),
                                  "mid": (float(h.iloc[j])+float(l.iloc[j]))/2, "idx": j}
                    break
        for i in bear_impulse[bear_impulse].index:
            for j in range(i-1, max(0, i-5), -1):
                if j < len(bull_candles) and bull_candles.iloc[j]:
                    bearish_ob = {"high": float(h.iloc[j]), "low": float(l.iloc[j]),
                                  "mid": (float(h.iloc[j])+float(l.iloc[j]))/2, "idx": j}
                    break

        cur_price = df['close'].iloc[-1]

        # Is price currently AT an order block? (within 0.5%)
        at_bullish_ob = (bullish_ob and
                         bullish_ob['low'] <= cur_price <= bullish_ob['high'] * 1.005)
        at_bearish_ob = (bearish_ob and
                         bearish_ob['low'] * 0.995 <= cur_price <= bearish_ob['high'])

        return {
            "bullish_ob": bullish_ob,
            "bearish_ob": bearish_ob,
            "at_bullish_ob": bool(at_bullish_ob),
            "at_bearish_ob": bool(at_bearish_ob),
            "score": (2 if at_bullish_ob else -2 if at_bearish_ob else 0)
        }

    def find_fair_value_gaps(self, df: pd.DataFrame, lookback: int = 15) -> dict:
        """
        Fair Value Gap (FVG): 3-candle pattern where candle 1 high < candle 3 low (bullish)
        or candle 1 low > candle 3 high (bearish).
        Represents an imbalance — price often returns to fill it.
        """
        recent = df.tail(lookback + 2)
        fvgs   = {"bullish": [], "bearish": []}

        for i in range(1, len(recent) - 1):
            c1_high = recent['high'].iloc[i-1]
            c1_low  = recent['low'].iloc[i-1]
            c3_high = recent['high'].iloc[i+1]
            c3_low  = recent['low'].iloc[i+1]

            # Bullish FVG: gap up — space between candle 1 high and candle 3 low
            if c1_high < c3_low:
                fvgs['bullish'].append({"top": c3_low, "bottom": c1_high,
                                        "mid": (c3_low + c1_high) / 2})
            # Bearish FVG: gap down
            if c1_low > c3_high:
                fvgs['bearish'].append({"top": c1_low, "bottom": c3_high,
                                        "mid": (c1_low + c3_high) / 2})

        cur = df['close'].iloc[-1]
        # Is price in an unfilled FVG?
        in_bullish_fvg = any(f['bottom'] <= cur <= f['top'] for f in fvgs['bullish'])
        in_bearish_fvg = any(f['bottom'] <= cur <= f['top'] for f in fvgs['bearish'])

        return {
            "bullish_fvgs": len(fvgs['bullish']),
            "bearish_fvgs": len(fvgs['bearish']),
            "in_bullish_fvg": in_bullish_fvg,
            "in_bearish_fvg": in_bearish_fvg,
            "latest_fvg": fvgs['bullish'][-1] if fvgs['bullish'] else fvgs['bearish'][-1] if fvgs['bearish'] else None,
            "score": (1 if in_bullish_fvg else -1 if in_bearish_fvg else 0)
        }

    def detect_liquidity_sweep(self, df: pd.DataFrame, lookback: int = 30) -> dict:
        """
        Liquidity Sweep / Stop Hunt:
        Smart money drives price above a swing high (to trigger retail buy stops,
        take the other side, then reverse) or below a swing low.

        Signs: price wicks above recent high/low then closes back inside.
        """
        recent   = df.tail(lookback)
        cur      = df['close'].iloc[-1]
        last_bar = df.iloc[-1]

        swings   = self.detect_swing_points(recent, strength=3)
        highs    = [s['price'] for s in swings['highs']]
        lows     = [s['price'] for s in swings['lows']]

        sweep_high = False
        sweep_low  = False

        if highs:
            key_high = sorted(highs)[-1]
            # Wick above key high but closed below = sweep of buy-side liquidity (bearish)
            if last_bar['high'] > key_high and last_bar['close'] < key_high:
                sweep_high = True

        if lows:
            key_low = sorted(lows)[0]
            # Wick below key low but closed above = sweep of sell-side liquidity (bullish)
            if last_bar['low'] < key_low and last_bar['close'] > key_low:
                sweep_low = True

        return {
            "sweep_high": sweep_high,   # Bearish signal — smart money sold into the sweep
            "sweep_low":  sweep_low,    # Bullish signal — smart money bought into the sweep
            "score": (2 if sweep_low else -2 if sweep_high else 0)
        }

    def premium_discount(self, df: pd.DataFrame, lookback: int = 50) -> dict:
        """
        Premium/Discount analysis:
        - Below 50% of recent range = Discount (smart money buys here)
        - Above 50% of recent range = Premium (smart money sells here)
        - Optimal Buy Zone: 62–79% Fibonacci retracement
        """
        recent   = df.tail(lookback)
        rng_high = recent['high'].max()
        rng_low  = recent['low'].min()
        rng      = rng_high - rng_low
        cur      = df['close'].iloc[-1]

        if rng == 0:
            return {"zone": "unknown", "pct": 50, "score": 0}

        pct_in_range = (cur - rng_low) / rng * 100

        # Fibonacci levels
        fib_618 = rng_high - 0.618 * rng   # OTE buy zone top
        fib_79  = rng_high - 0.79  * rng   # OTE buy zone bottom

        in_ote_buy  = fib_79 <= cur <= fib_618
        in_ote_sell = (rng_low + 0.618*rng) <= cur <= (rng_low + 0.79*rng)

        zone = "discount" if pct_in_range < 50 else "premium"

        return {
            "zone":        zone,
            "pct":         round(pct_in_range, 1),
            "range_high":  round(rng_high, 2),
            "range_low":   round(rng_low, 2),
            "in_ote_buy":  in_ote_buy,
            "in_ote_sell": in_ote_sell,
            "score": (
                1 if zone == "discount" else -1 +
                (1 if in_ote_buy else -1 if in_ote_sell else 0)
            )
        }


# ─────────────────────────────────────────────
# MODULE 3: VOLUME PROFILE ANALYZER
# ─────────────────────────────────────────────

class VolumeProfileAnalyzer:
    """
    Volume Profile: shows where the most trading activity happened.
    Institutions use this to identify:
    - Point of Control (POC): highest volume price = magnet
    - Value Area High (VAH): top of 70% volume zone
    - Value Area Low (VAL): bottom of 70% volume zone
    - High Volume Nodes (HVN): strong support/resistance
    - Low Volume Nodes (LVN): price moves quickly through these
    """

    def compute(self, df: pd.DataFrame, bins: int = 30, lookback: int = 60) -> dict:
        recent = df.tail(lookback)
        price_min = recent['low'].min()
        price_max = recent['high'].max()

        if price_max == price_min:
            return {"poc": df['close'].iloc[-1], "vah": price_max, "val": price_min,
                    "score": 0, "current_zone": "unknown"}

        # Distribute volume across price bins
        bin_edges = np.linspace(price_min, price_max, bins + 1)
        vol_profile = np.zeros(bins)

        # Fast vectorized version using numpy histogram
        mid_prices = (recent['high'] + recent['low']) / 2
        vol_profile, _ = np.histogram(mid_prices, bins=bin_edges, weights=recent['volume'])

        poc_idx   = np.argmax(vol_profile)
        poc_price = (bin_edges[poc_idx] + bin_edges[poc_idx+1]) / 2

        # Value Area: 70% of total volume around POC
        total_vol = vol_profile.sum()
        target    = total_vol * 0.70
        va_vol    = vol_profile[poc_idx]
        lo, hi    = poc_idx, poc_idx

        while va_vol < target and (lo > 0 or hi < bins - 1):
            add_lo = vol_profile[lo-1] if lo > 0 else 0
            add_hi = vol_profile[hi+1] if hi < bins-1 else 0
            if add_hi >= add_lo:
                hi += 1; va_vol += add_hi
            else:
                lo -= 1; va_vol += add_lo

        vah = (bin_edges[hi] + bin_edges[hi+1]) / 2
        val = (bin_edges[lo] + bin_edges[lo+1]) / 2

        cur = df['close'].iloc[-1]
        in_value_area = val <= cur <= vah
        above_poc     = cur > poc_price
        zone          = "above_value_area" if cur > vah else \
                        "below_value_area" if cur < val else \
                        "in_value_area"

        return {
            "poc":           round(poc_price, 2),
            "vah":           round(vah, 2),
            "val":           round(val, 2),
            "current_zone":  zone,
            "in_value_area": in_value_area,
            "above_poc":     above_poc,
            "score": (
                1 if (in_value_area and above_poc)  else
               -1 if (in_value_area and not above_poc) else
                0
            )
        }


# ─────────────────────────────────────────────
# MODULE 4: MARKET STRUCTURE + WYCKOFF
# ─────────────────────────────────────────────

class WyckoffAnalyzer:
    """
    Wyckoff Method: Richard Wyckoff's framework for understanding
    how smart money (the "Composite Man") accumulates and distributes.

    Phases:
    - Accumulation: price range-bound, volume drying up → smart money buying
    - Markup: breakout upward, rising volume
    - Distribution: price range-bound at top, volume erratic → smart money selling
    - Markdown: price falls on volume
    """

    def detect_phase(self, df: pd.DataFrame, lookback: int = 60) -> dict:
        recent  = df.tail(lookback)
        close   = recent['close']
        volume  = recent['volume']
        high    = recent['high']
        low     = recent['low']

        # Price range compression (ranging market)
        price_range    = (high.max() - low.min()) / close.mean()
        recent_range   = (high.tail(10).max() - low.tail(10).min()) / close.mean()
        range_ratio    = recent_range / price_range

        # Volume trend
        vol_early = volume.head(lookback//2).mean()
        vol_late  = volume.tail(lookback//2).mean()
        vol_trend = vol_late / vol_early

        # Price trend
        price_change = (close.iloc[-1] - close.iloc[0]) / close.iloc[0]

        # Effort vs Result: high volume but small price move = absorption
        effort   = volume.tail(10).mean() / volume.mean()
        result   = abs(close.tail(10).iloc[-1] - close.tail(10).iloc[0]) / close.mean()
        EvR      = result / (effort + 1e-9)

        # Phase detection logic
        if range_ratio < 0.4 and vol_trend < 0.8 and abs(price_change) < 0.03:
            if close.iloc[-1] < close.mean():
                phase = WyckoffPhase.ACCUMULATION
                desc  = "Price range-bound at lows, volume drying up — smart money accumulating"
            else:
                phase = WyckoffPhase.DISTRIBUTION
                desc  = "Price range-bound at highs, volume erratic — smart money distributing"
        elif price_change > 0.03 and vol_trend > 1.0:
            phase = WyckoffPhase.MARKUP
            desc  = "Rising price with rising volume — markup phase"
        elif price_change < -0.03 and vol_trend > 1.0:
            phase = WyckoffPhase.MARKDOWN
            desc  = "Falling price with rising volume — markdown phase"
        else:
            phase = WyckoffPhase.UNKNOWN
            desc  = "No clear Wyckoff phase"

        bullish_phases = [WyckoffPhase.ACCUMULATION, WyckoffPhase.MARKUP]
        bearish_phases = [WyckoffPhase.DISTRIBUTION, WyckoffPhase.MARKDOWN]

        return {
            "phase":       phase,
            "description": desc,
            "effort_vs_result": round(EvR, 3),
            "vol_trend":   round(vol_trend, 2),
            "score": (
                2 if phase == WyckoffPhase.ACCUMULATION else
                1 if phase == WyckoffPhase.MARKUP       else
               -1 if phase == WyckoffPhase.DISTRIBUTION else
               -2 if phase == WyckoffPhase.MARKDOWN     else 0
            )
        }


# ─────────────────────────────────────────────
# MODULE 5: INSTITUTIONAL LEVELS
# ─────────────────────────────────────────────

class InstitutionalLevels:
    """
    Key price levels that institutions watch:
    - VWAP + Standard Deviation bands (institutional benchmark)
    - Weekly / Monthly Open (institutional reference levels)
    - Pivot Points (used by market makers)
    - Previous Day High/Low (important for intraday)
    """

    def vwap_analysis(self, df: pd.DataFrame) -> dict:
        """VWAP with ±1σ and ±2σ bands."""
        tp   = (df['high'] + df['low'] + df['close']) / 3
        vwap = (tp * df['volume']).cumsum() / df['volume'].cumsum()
        std  = (tp - vwap).rolling(20).std()

        cur       = df['close'].iloc[-1]
        vwap_cur  = vwap.iloc[-1]
        std_cur   = std.iloc[-1]
        upper1    = vwap_cur + std_cur
        lower1    = vwap_cur - std_cur
        upper2    = vwap_cur + 2 * std_cur
        lower2    = vwap_cur - 2 * std_cur

        deviation = (cur - vwap_cur) / (std_cur + 1e-9)

        if cur > upper2:    zone = "extreme_premium"
        elif cur > upper1:  zone = "premium"
        elif cur > vwap_cur:zone = "above_vwap"
        elif cur > lower1:  zone = "below_vwap"
        elif cur > lower2:  zone = "discount"
        else:               zone = "extreme_discount"

        return {
            "vwap":      round(float(vwap_cur), 2),
            "upper1":    round(float(upper1), 2),
            "lower1":    round(float(lower1), 2),
            "upper2":    round(float(upper2), 2),
            "lower2":    round(float(lower2), 2),
            "zone":      zone,
            "deviation": round(float(deviation), 2),
            "score": (
               -2 if zone == "extreme_premium"  else
               -1 if zone == "premium"          else
                0 if zone in ["above_vwap","below_vwap"] else
                1 if zone == "discount"          else
                2 if zone == "extreme_discount"  else 0
            )
        }

    def pivot_points(self, df: pd.DataFrame) -> dict:
        """Standard pivot points — used by market makers and prop desks."""
        yesterday = df.iloc[-2]
        H, L, C  = yesterday['high'], yesterday['low'], yesterday['close']
        pivot    = (H + L + C) / 3
        r1       = 2 * pivot - L
        r2       = pivot + (H - L)
        r3       = H + 2 * (pivot - L)
        s1       = 2 * pivot - H
        s2       = pivot - (H - L)
        s3       = L - 2 * (H - pivot)
        cur      = df['close'].iloc[-1]

        # Is price near a key pivot level? (within 0.3%)
        levels   = {"P": pivot, "R1": r1, "R2": r2, "S1": s1, "S2": s2}
        near_level = None
        for name, level in levels.items():
            if abs(cur - level) / level < 0.003:
                near_level = name
                break

        return {
            "pivot": round(pivot, 2),
            "r1": round(r1, 2), "r2": round(r2, 2), "r3": round(r3, 2),
            "s1": round(s1, 2), "s2": round(s2, 2), "s3": round(s3, 2),
            "near_level": near_level,
            "above_pivot": cur > pivot,
            "score": (1 if cur > pivot else -1)
        }

    def weekly_levels(self, df: pd.DataFrame) -> dict:
        """Weekly open and range — institutions trade off weekly levels."""
        weekly = df.resample('W', on=df.index.to_series()).agg(
            {'open':'first','high':'max','low':'min','close':'last'}
        ) if hasattr(df.index, 'freq') else None

        # Fallback: approximate from last 5 days
        week_data  = df.tail(5)
        week_open  = week_data['open'].iloc[0]
        week_high  = week_data['high'].max()
        week_low   = week_data['low'].min()
        cur        = df['close'].iloc[-1]
        above_wk_open = cur > week_open

        return {
            "week_open":      round(week_open, 2),
            "week_high":      round(week_high, 2),
            "week_low":       round(week_low, 2),
            "above_week_open": above_wk_open,
            "score": (1 if above_wk_open else -1)
        }


# ─────────────────────────────────────────────
# MODULE 6: POSITION SIZER (Kelly + Smart Money)
# ─────────────────────────────────────────────

class SmartMoneyPositionSizer:
    """
    Position sizing considering:
    - Half-Kelly with confidence scaling
    - Hard risk limits
    - Order block proximity bonus (higher confidence = larger size)
    """

    MAX_RISK_PER_TRADE = 0.02   # Risk max 2% of capital per trade
    MAX_POSITION       = 0.30   # Max 30% in one asset
    ATR_MULTIPLIER     = 1.5    # SL distance

    def compute_atr(self, df: pd.DataFrame, period: int = 14) -> float:
        high, low, close = df['high'], df['low'], df['close']
        tr = pd.concat([high-low, (high-close.shift()).abs(), (low-close.shift()).abs()], axis=1).max(axis=1)
        return float(tr.ewm(span=period).mean().iloc[-1])

    def size(self, capital: float, confidence: float, entry: float,
             stop_loss: float, win_rate: float = 0.55, at_key_level: bool = False) -> dict:
        sl_distance  = abs(entry - stop_loss)
        rr_ratio     = 2.0   # Default 2:1
        p, q         = win_rate, 1 - win_rate
        b            = rr_ratio
        kelly        = max(0, (b*p - q) / b)
        half_kelly   = kelly / 2
        size         = half_kelly * confidence
        if at_key_level:
            size *= 1.2   # Slight bonus when at confirmed OB or FVG

        size = min(size, self.MAX_POSITION)
        capital_to_deploy = size * capital
        units_risk = capital * self.MAX_RISK_PER_TRADE / sl_distance if sl_distance > 0 else 0

        return {
            "size_pct":  round(size, 4),
            "capital":   round(capital_to_deploy, 0),
            "sl_distance": round(sl_distance, 2),
        }


# ─────────────────────────────────────────────
# MASTER ORCHESTRATOR
# ─────────────────────────────────────────────

class SmartMoneyOrchestrator:
    """
    Combines ALL smart money signals into one final decision.

    Scoring system (max ±20 points):
    ─────────────────────────────────
    Order Flow         : ±4 pts
    Market Structure   : ±3 pts (BOS, CHoCH)
    Order Block        : ±2 pts
    Fair Value Gap     : ±1 pt
    Liquidity Sweep    : ±2 pts
    Premium/Discount   : ±2 pts
    Volume Profile POC : ±1 pt
    VWAP position      : ±2 pts
    Wyckoff phase      : ±2 pts
    Pivot Points       : ±1 pt
    ─────────────────────────────────
    Total              : ±20 pts

    Signal fires only when confidence > 55%
    """

    def __init__(self, capital: float = 500000, win_rate: float = 0.55):
        self.capital     = capital
        self.win_rate    = win_rate
        self.order_flow  = OrderFlowAnalyzer()
        self.smc         = SmartMoneyConceptsEngine()
        self.vol_profile = VolumeProfileAnalyzer()
        self.wyckoff     = WyckoffAnalyzer()
        self.inst_levels = InstitutionalLevels()
        self.sizer       = SmartMoneyPositionSizer()

    def _compute_levels(self, df: pd.DataFrame, bias: MarketBias) -> tuple:
        """Compute entry, SL, TP using ATR and smart money levels."""
        atr   = self.sizer.compute_atr(df)
        entry = float(df['close'].iloc[-1])
        sl_dist = atr * self.sizer.ATR_MULTIPLIER
        tp_dist = sl_dist * 2.0

        if bias in [MarketBias.BULLISH, MarketBias.STRONG_BULLISH]:
            sl = entry - sl_dist
            tp = entry + tp_dist
        else:
            sl = entry + sl_dist
            tp = entry - tp_dist

        return round(entry, 4), round(sl, 4), round(tp, 4)

    def evaluate(self, df: pd.DataFrame, asset: str) -> Optional[SmartMoneySignal]:
        """Full smart money evaluation for one asset."""
        if len(df) < 60:
            log.warning(f"{asset}: Need at least 60 bars")
            return None

        reasons = []
        score   = 0
        max_pts = 20

        # ── 1. ORDER FLOW (±4 pts) ──────────────────────────
        of = self.order_flow.buy_sell_pressure(df)
        score += of['score']
        reasons.append(f"{'✅' if of['score']>0 else '❌' if of['score']<0 else '⚪'} "
                       f"Order Flow: Delta={of['cumulative_delta']:+,.0f} | "
                       f"RVOL={of['rvol']:.2f}x | Trend={of['delta_trend']}")
        if of['bullish_imbalance']:
            reasons.append("  ⚡ Institutional BUY imbalance candle detected")
        if of['bearish_imbalance']:
            reasons.append("  ⚡ Institutional SELL imbalance candle detected")
        if of['absorption']:
            reasons.append("  🔴 Absorption detected — potential reversal")

        # ── 2. MARKET STRUCTURE (±3 pts) ───────────────────
        ms = self.smc.detect_market_structure(df)
        struct_score = (2 if ms['bias']=='bullish' else -2 if ms['bias']=='bearish' else 0)
        struct_score += (1 if ms['bos'] and ms['bos_direction']=='bullish' else
                        -1 if ms['bos'] and ms['bos_direction']=='bearish' else 0)
        score += struct_score
        reasons.append(f"{'✅' if struct_score>0 else '❌' if struct_score<0 else '⚪'} "
                       f"Market Structure: {ms['structure']}"
                       + (" | ⚡ BREAK OF STRUCTURE!" if ms['bos'] else "")
                       + (" | ⚠️ CHoCH — trend changing!" if ms['choch'] else ""))

        # ── 3. ORDER BLOCKS (±2 pts) ────────────────────────
        ob = self.smc.find_order_blocks(df)
        score += ob['score']
        if ob['at_bullish_ob']:
            reasons.append(f"✅ Price AT Bullish Order Block {ob['bullish_ob']['low']:.2f}–{ob['bullish_ob']['high']:.2f} — institutional buy zone")
        elif ob['at_bearish_ob']:
            reasons.append(f"❌ Price AT Bearish Order Block {ob['bearish_ob']['low']:.2f}–{ob['bearish_ob']['high']:.2f} — institutional sell zone")
        elif ob['bullish_ob']:
            reasons.append(f"⚪ Bullish OB below: {ob['bullish_ob']['mid']:.2f}")
        elif ob['bearish_ob']:
            reasons.append(f"⚪ Bearish OB above: {ob['bearish_ob']['mid']:.2f}")

        # ── 4. FAIR VALUE GAPS (±1 pt) ──────────────────────
        fvg = self.smc.find_fair_value_gaps(df)
        score += fvg['score']
        if fvg['in_bullish_fvg']:
            reasons.append("✅ Price inside Bullish Fair Value Gap — imbalance zone (buy)")
        elif fvg['in_bearish_fvg']:
            reasons.append("❌ Price inside Bearish Fair Value Gap — imbalance zone (sell)")
        else:
            reasons.append(f"⚪ FVGs: {fvg['bullish_fvgs']} bullish, {fvg['bearish_fvgs']} bearish nearby")

        # ── 5. LIQUIDITY SWEEP (±2 pts) ─────────────────────
        liq = self.smc.detect_liquidity_sweep(df)
        score += liq['score']
        if liq['sweep_low']:
            reasons.append("✅ Sell-side liquidity SWEPT — smart money bought stops (bullish reversal)")
        elif liq['sweep_high']:
            reasons.append("❌ Buy-side liquidity SWEPT — smart money sold stops (bearish reversal)")

        # ── 6. PREMIUM / DISCOUNT (±2 pts) ──────────────────
        pd_data = self.smc.premium_discount(df)
        score  += pd_data['score']
        ote_str = " | 🎯 IN OTE ZONE" if pd_data['in_ote_buy'] or pd_data['in_ote_sell'] else ""
        reasons.append(f"{'✅' if pd_data['zone']=='discount' else '❌'} "
                       f"Price in {pd_data['zone'].upper()} zone ({pd_data['pct']:.1f}% of range){ote_str}")

        # ── 7. VOLUME PROFILE (±1 pt) ───────────────────────
        vp = self.vol_profile.compute(df)
        score += vp['score']
        reasons.append(f"{'✅' if vp['score']>0 else '❌' if vp['score']<0 else '⚪'} "
                       f"Volume Profile: POC={vp['poc']:.2f} | VAH={vp['vah']:.2f} | VAL={vp['val']:.2f} | Zone={vp['current_zone']}")

        # ── 8. VWAP (±2 pts) ────────────────────────────────
        vwap = self.inst_levels.vwap_analysis(df)
        score += vwap['score']
        reasons.append(f"{'✅' if vwap['score']>0 else '❌' if vwap['score']<0 else '⚪'} "
                       f"VWAP: {vwap['vwap']:.2f} | Zone={vwap['zone']} | Deviation={vwap['deviation']:+.2f}σ")

        # ── 9. WYCKOFF (±2 pts) ─────────────────────────────
        wy = self.wyckoff.detect_phase(df)
        score += wy['score']
        reasons.append(f"{'✅' if wy['score']>0 else '❌' if wy['score']<0 else '⚪'} "
                       f"Wyckoff: {wy['phase'].value} | {wy['description']}")

        # ── 10. PIVOT POINTS (±1 pt) ────────────────────────
        piv = self.inst_levels.pivot_points(df)
        score += piv['score']
        near_str = f" | Near {piv['near_level']}" if piv['near_level'] else ""
        reasons.append(f"{'✅' if piv['score']>0 else '❌'} "
                       f"Pivot: P={piv['pivot']:.2f} | {'Above' if piv['above_pivot'] else 'Below'} pivot{near_str}")

        # ── FINAL SIGNAL ────────────────────────────────────
        confidence = (score + max_pts) / (2 * max_pts)
        confidence = max(0.0, min(1.0, confidence))

        if score >= 8:      bias = MarketBias.STRONG_BULLISH
        elif score >= 4:    bias = MarketBias.BULLISH
        elif score <= -8:   bias = MarketBias.STRONG_BEARISH
        elif score <= -4:   bias = MarketBias.BEARISH
        else:               bias = MarketBias.NEUTRAL

        # Skip neutral or low confidence
        if bias == MarketBias.NEUTRAL or confidence < 0.52:
            log.info(f"{asset}: No signal (score={score}, conf={confidence:.0%})")
            return None

        entry, sl, tp = self._compute_levels(df, bias)
        at_key = ob['at_bullish_ob'] or ob['at_bearish_ob'] or fvg['in_bullish_fvg'] or fvg['in_bearish_fvg']
        sizing = self.sizer.size(self.capital, confidence, entry, sl, self.win_rate, at_key)

        sig = SmartMoneySignal(
            asset             = asset,
            bias              = bias,
            confidence        = confidence,
            entry_price       = entry,
            stop_loss         = sl,
            take_profit       = tp,
            position_size_pct = sizing['size_pct'],
            wyckoff_phase     = wy['phase'],
            structure         = ms['structure'],
            order_block       = ob['bullish_ob']['mid'] if ob['at_bullish_ob'] else (ob['bearish_ob']['mid'] if ob['at_bearish_ob'] else None),
            fvg_present       = fvg['in_bullish_fvg'] or fvg['in_bearish_fvg'],
            liquidity_sweep   = liq['sweep_low'] or liq['sweep_high'],
            bos_detected      = ms['bos'],
            cumulative_delta  = of['cumulative_delta'],
            volume_poc        = vp['poc'],
            vwap_position     = vwap['zone'],
            reasons           = reasons,
        )

        log.info(f"\n{sig.summary()}")
        for r in reasons:
            log.info(f"   {r}")

        return sig

    def evaluate_all(self, gold_df: pd.DataFrame, btc_df: pd.DataFrame,
                     eth_df: pd.DataFrame) -> list:
        """Evaluate all three assets."""
        tickers = {"XAUUSD": gold_df, "BTCUSDT": btc_df, "ETHUSDT": eth_df}
        signals = []
        for asset, df in tickers.items():
            try:
                sig = self.evaluate(df, asset)
                if sig:
                    signals.append(sig)
            except Exception as e:
                log.error(f"{asset} evaluation failed: {e}")
        return signals


# ─────────────────────────────────────────────
# TEST RUN
# ─────────────────────────────────────────────

def _make_df(seed=42, n=200, base=100.0, trend=0.001):
    np.random.seed(seed)
    close = base * np.cumprod(1 + trend + np.random.normal(0, 0.012, n))
    high  = close * (1 + np.abs(np.random.normal(0, 0.006, n)))
    low   = close * (1 - np.abs(np.random.normal(0, 0.006, n)))
    open_ = np.roll(close, 1)
    vol   = np.random.randint(5000, 50000, n).astype(float)
    # Add some volume spikes for realism
    vol[50], vol[100], vol[150] = vol[50]*3, vol[100]*2.5, vol[150]*2
    idx = pd.date_range("2024-01-01", periods=n, freq="1D")
    return pd.DataFrame({'open':open_,'high':high,'low':low,'close':close,'volume':vol}, index=idx)

if __name__ == "__main__":
    print("\n" + "="*65)
    print("  SMART MONEY ENGINE v2.0 — TEST RUN")
    print("="*65 + "\n")

    orch = SmartMoneyOrchestrator(capital=500000, win_rate=0.55)
    gold = _make_df(seed=1, base=2350, trend=0.0006)
    btc  = _make_df(seed=2, base=65000, trend=0.0012)
    eth  = _make_df(seed=3, base=3200, trend=-0.0008)

    signals = orch.evaluate_all(gold, btc, eth)

    print(f"\n{'─'*65}")
    print(f"  SIGNALS FOUND: {len(signals)}")
    print(f"{'─'*65}")
    for s in signals:
        print(f"\n  {s.summary()}")
        print(f"  Wyckoff: {s.wyckoff_phase.value}")
        print(f"  Structure: {s.structure}")
        print(f"  BOS: {s.bos_detected} | FVG: {s.fvg_present} | Liq Sweep: {s.liquidity_sweep}")
        print(f"  VWAP Zone: {s.vwap_position} | Vol POC: {s.volume_poc}")
        capital = s.position_size_pct * 500000
        print(f"  Capital: ₹{capital:,.0f}")
        print(f"\n  Reasons:")
        for r in s.reasons:
            print(f"    {r}")
    print("\n" + "="*65 + "\n")
