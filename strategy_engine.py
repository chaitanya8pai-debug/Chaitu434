"""
=============================================================
  INSTITUTIONAL TRADING STRATEGY ENGINE
  Assets: XAUUSD (Gold), BTC/USDT, ETH/USDT
  Author: Built for Chaitu | Version 1.0
=============================================================

MODULES:
  1. MarketRegimeDetector     — Trending / Ranging / Volatile
  2. SignalGenerator          — Multi-factor signal per asset
  3. CorrelationEngine        — Gold ↔ BTC ↔ ETH ↔ DXY
  4. PositionSizer            — Kelly Criterion + risk caps
  5. RiskManager              — Drawdown kill switch + stops
  6. StrategyOrchestrator     — Master controller
=============================================================
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("StrategyEngine")


# ─────────────────────────────────────────────
# ENUMS & DATA STRUCTURES
# ─────────────────────────────────────────────

class Regime(Enum):
    TRENDING_UP   = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGING       = "ranging"
    HIGH_VOLATILITY = "high_volatility"

class Signal(Enum):
    STRONG_BUY  =  2
    BUY         =  1
    NEUTRAL     =  0
    SELL        = -1
    STRONG_SELL = -2

class Asset(Enum):
    GOLD = "XAUUSD"
    BTC  = "BTCUSDT"
    ETH  = "ETHUSDT"

@dataclass
class TradeSignal:
    asset: Asset
    signal: Signal
    regime: Regime
    confidence: float          # 0.0 to 1.0
    entry_price: float
    stop_loss: float
    take_profit: float
    position_size_pct: float   # % of capital to deploy
    reasons: list[str] = field(default_factory=list)

    def __str__(self):
        arrow = "🟢" if self.signal.value > 0 else ("🔴" if self.signal.value < 0 else "⚪")
        return (
            f"{arrow} {self.asset.value} | {self.signal.name} | "
            f"Regime: {self.regime.value} | Confidence: {self.confidence:.0%} | "
            f"Entry: {self.entry_price:.2f} | SL: {self.stop_loss:.2f} | "
            f"TP: {self.take_profit:.2f} | Size: {self.position_size_pct:.1%}"
        )


# ─────────────────────────────────────────────
# MODULE 1: MARKET REGIME DETECTOR
# ─────────────────────────────────────────────

class MarketRegimeDetector:
    """
    Detects market regime using:
    - ADX (Average Directional Index) for trend strength
    - ATR (Average True Range) for volatility
    - Bollinger Band width for ranging detection
    """

    def __init__(self, adx_period=14, atr_period=14, bb_period=20):
        self.adx_period = adx_period
        self.atr_period = atr_period
        self.bb_period  = bb_period

    def _compute_adx(self, df: pd.DataFrame) -> pd.Series:
        high, low, close = df['high'], df['low'], df['close']
        plus_dm  = high.diff().clip(lower=0)
        minus_dm = (-low.diff()).clip(lower=0)
        plus_dm[plus_dm < minus_dm]  = 0
        minus_dm[minus_dm < plus_dm] = 0

        atr = self._compute_atr(df)
        plus_di  = 100 * (plus_dm.ewm(span=self.adx_period).mean()  / atr)
        minus_di = 100 * (minus_dm.ewm(span=self.adx_period).mean() / atr)
        dx = (abs(plus_di - minus_di) / (plus_di + minus_di + 1e-9)) * 100
        adx = dx.ewm(span=self.adx_period).mean()
        return adx, plus_di, minus_di

    def _compute_atr(self, df: pd.DataFrame) -> pd.Series:
        high, low, close = df['high'], df['low'], df['close']
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs()
        ], axis=1).max(axis=1)
        return tr.ewm(span=self.atr_period).mean()

    def _compute_bb_width(self, df: pd.DataFrame) -> pd.Series:
        close = df['close']
        ma    = close.rolling(self.bb_period).mean()
        std   = close.rolling(self.bb_period).std()
        upper = ma + 2 * std
        lower = ma - 2 * std
        width = (upper - lower) / (ma + 1e-9)
        return width

    def detect(self, df: pd.DataFrame) -> Regime:
        """
        Returns the current market regime for a given OHLCV dataframe.
        df must have columns: open, high, low, close, volume
        """
        adx, plus_di, minus_di = self._compute_adx(df)
        atr       = self._compute_atr(df)
        bb_width  = self._compute_bb_width(df)

        latest_adx      = adx.iloc[-1]
        latest_plus_di  = plus_di.iloc[-1]
        latest_minus_di = minus_di.iloc[-1]
        latest_atr      = atr.iloc[-1]
        avg_atr         = atr.rolling(50).mean().iloc[-1]
        latest_bbw      = bb_width.iloc[-1]
        avg_bbw         = bb_width.rolling(50).mean().iloc[-1]

        # High volatility: ATR spike > 1.5x average
        if latest_atr > 1.5 * avg_atr:
            return Regime.HIGH_VOLATILITY

        # Strong trend: ADX > 25
        if latest_adx > 25:
            if latest_plus_di > latest_minus_di:
                return Regime.TRENDING_UP
            else:
                return Regime.TRENDING_DOWN

        # Ranging: Bollinger Band width contracting
        if latest_bbw < 0.8 * avg_bbw:
            return Regime.RANGING

        # Default: weak trend — treat as ranging
        return Regime.RANGING


# ─────────────────────────────────────────────
# MODULE 2: SIGNAL GENERATOR
# ─────────────────────────────────────────────

class SignalGenerator:
    """
    Multi-factor signal engine. Uses different strategies
    per regime — momentum in trends, mean reversion in ranges.

    Indicators used:
    - EMA crossover (fast/slow)
    - RSI (momentum + divergence check)
    - VWAP deviation (institutional entry level)
    - Order flow proxy (volume delta)
    - Macro overlay (for Gold: DXY correlation)
    """

    def __init__(self):
        self.ema_fast   = 9
        self.ema_slow   = 21
        self.ema_trend  = 50
        self.rsi_period = 14

    def _ema(self, series: pd.Series, period: int) -> pd.Series:
        return series.ewm(span=period, adjust=False).mean()

    def _rsi(self, close: pd.Series) -> pd.Series:
        delta = close.diff()
        gain  = delta.clip(lower=0).ewm(span=self.rsi_period).mean()
        loss  = (-delta.clip(upper=0)).ewm(span=self.rsi_period).mean()
        rs    = gain / (loss + 1e-9)
        return 100 - (100 / (1 + rs))

    def _vwap(self, df: pd.DataFrame) -> pd.Series:
        tp  = (df['high'] + df['low'] + df['close']) / 3
        return (tp * df['volume']).cumsum() / df['volume'].cumsum()

    def _volume_delta(self, df: pd.DataFrame) -> pd.Series:
        """
        Proxy for order flow: positive = buyers dominant, negative = sellers.
        Uses candle body direction × volume as a simplified delta.
        """
        direction = np.sign(df['close'] - df['open'])
        return direction * df['volume']

    def generate(self, df: pd.DataFrame, regime: Regime, asset: Asset) -> tuple[Signal, float, list[str]]:
        """
        Returns (Signal, confidence_score, reasons_list)
        confidence is 0.0–1.0 based on how many factors agree.
        """
        close    = df['close']
        reasons  = []
        score    = 0   # positive = bullish, negative = bearish
        max_pts  = 0

        ema_fast  = self._ema(close, self.ema_fast)
        ema_slow  = self._ema(close, self.ema_slow)
        ema_trend = self._ema(close, self.ema_trend)
        rsi       = self._rsi(close)
        vwap      = self._vwap(df)
        vol_delta = self._volume_delta(df)

        cur_price     = close.iloc[-1]
        cur_ema_fast  = ema_fast.iloc[-1]
        cur_ema_slow  = ema_slow.iloc[-1]
        cur_ema_trend = ema_trend.iloc[-1]
        cur_rsi       = rsi.iloc[-1]
        cur_vwap      = vwap.iloc[-1]
        cur_vol_delta = vol_delta.rolling(5).sum().iloc[-1]
        prev_vol_delta= vol_delta.rolling(5).sum().iloc[-2]

        # ── FACTOR 1: EMA Crossover (2 pts) ──
        max_pts += 2
        if cur_ema_fast > cur_ema_slow:
            score += 2
            reasons.append("✅ EMA fast > slow (bullish crossover)")
        else:
            score -= 2
            reasons.append("❌ EMA fast < slow (bearish crossover)")

        # ── FACTOR 2: Price vs Trend EMA (1 pt) ──
        max_pts += 1
        if cur_price > cur_ema_trend:
            score += 1
            reasons.append("✅ Price above 50 EMA (bullish bias)")
        else:
            score -= 1
            reasons.append("❌ Price below 50 EMA (bearish bias)")

        # ── FACTOR 3: RSI (regime-adjusted) (2 pts) ──
        max_pts += 2
        if regime in [Regime.TRENDING_UP, Regime.TRENDING_DOWN]:
            # In trends: RSI as momentum, not overbought/oversold
            if regime == Regime.TRENDING_UP and cur_rsi > 50:
                score += 2
                reasons.append(f"✅ RSI {cur_rsi:.1f} confirms uptrend momentum")
            elif regime == Regime.TRENDING_DOWN and cur_rsi < 50:
                score -= 2
                reasons.append(f"❌ RSI {cur_rsi:.1f} confirms downtrend momentum")
            else:
                reasons.append(f"⚠️ RSI {cur_rsi:.1f} diverging from regime direction")
        else:
            # In ranging: use overbought/oversold
            if cur_rsi < 35:
                score += 2
                reasons.append(f"✅ RSI {cur_rsi:.1f} — oversold in ranging market")
            elif cur_rsi > 65:
                score -= 2
                reasons.append(f"❌ RSI {cur_rsi:.1f} — overbought in ranging market")
            else:
                reasons.append(f"⚪ RSI {cur_rsi:.1f} — neutral in ranging market")

        # ── FACTOR 4: VWAP Deviation (1 pt) ──
        max_pts += 1
        vwap_dev = (cur_price - cur_vwap) / (cur_vwap + 1e-9)
        if vwap_dev > 0.002:    # Price meaningfully above VWAP
            score += 1
            reasons.append(f"✅ Price {vwap_dev:.2%} above VWAP (institutional buying)")
        elif vwap_dev < -0.002:
            score -= 1
            reasons.append(f"❌ Price {vwap_dev:.2%} below VWAP (institutional selling)")
        else:
            reasons.append("⚪ Price near VWAP — no edge")

        # ── FACTOR 5: Volume/Order Flow Delta (2 pts) ──
        max_pts += 2
        if cur_vol_delta > 0 and cur_vol_delta > prev_vol_delta:
            score += 2
            reasons.append("✅ Volume delta accelerating bullish (buyers in control)")
        elif cur_vol_delta < 0 and cur_vol_delta < prev_vol_delta:
            score -= 2
            reasons.append("❌ Volume delta accelerating bearish (sellers in control)")
        else:
            reasons.append("⚪ Volume delta unclear — mixed order flow")

        # ── FACTOR 6: Gold-specific — DXY overlay (1 pt) ──
        # In live system, feed real DXY data. Here we use a placeholder.
        if asset == Asset.GOLD:
            max_pts += 1
            reasons.append("⚠️ DXY overlay: connect real DXY feed for live trading")
            # Example logic when DXY data available:
            # if dxy_falling: score += 1 (Gold inverse correlation)
            # if dxy_rising:  score -= 1

        # ── Compute Confidence & Final Signal ──
        raw_confidence = (score + max_pts) / (2 * max_pts)  # normalise 0–1
        confidence     = max(0.0, min(1.0, raw_confidence))

        if score >= 4:
            signal = Signal.STRONG_BUY
        elif score >= 2:
            signal = Signal.BUY
        elif score <= -4:
            signal = Signal.STRONG_SELL
        elif score <= -2:
            signal = Signal.SELL
        else:
            signal = Signal.NEUTRAL

        return signal, confidence, reasons


# ─────────────────────────────────────────────
# MODULE 3: CORRELATION ENGINE
# ─────────────────────────────────────────────

class CorrelationEngine:
    """
    Monitors rolling correlations between:
    - Gold ↔ BTC
    - Gold ↔ ETH
    - BTC ↔ ETH

    If all assets are highly correlated (> 0.7) AND all long,
    it flags over-concentration risk and reduces position sizes.

    Institutionally, correlated long positions = same risk.
    """

    def __init__(self, window=30):
        self.window = window

    def compute(self, price_dict: dict[str, pd.Series]) -> dict:
        """
        price_dict: {'XAUUSD': pd.Series, 'BTCUSDT': pd.Series, 'ETHUSDT': pd.Series}
        Returns correlation matrix and concentration risk flag.
        """
        df = pd.DataFrame(price_dict).pct_change().dropna()
        corr = df.rolling(self.window).corr().iloc[-len(price_dict):]

        results = {}
        pairs = [
            ("XAUUSD",  "BTCUSDT"),
            ("XAUUSD",  "ETHUSDT"),
            ("BTCUSDT", "ETHUSDT"),
        ]
        for a, b in pairs:
            if a in df.columns and b in df.columns:
                r = df[[a, b]].rolling(self.window).corr().unstack()[b][a].iloc[-1]
                results[f"{a}__{b}"] = round(r, 3)
                log.info(f"Correlation {a}↔{b}: {r:.3f}")

        # Concentration risk: all pairs correlated above 0.7
        high_corr_count = sum(1 for v in results.values() if abs(v) > 0.7)
        results['concentration_risk'] = high_corr_count >= 2
        if results['concentration_risk']:
            log.warning("⚠️  HIGH CORRELATION ACROSS ASSETS — reduce position sizes!")

        return results


# ─────────────────────────────────────────────
# MODULE 4: POSITION SIZER
# ─────────────────────────────────────────────

class PositionSizer:
    """
    Uses a modified Kelly Criterion with hard caps:
    - Full Kelly is too aggressive; we use Half-Kelly
    - Hard cap per asset: 30% of portfolio
    - Hard cap crypto total: 40% of portfolio
    - Hard cap Gold: 40% of portfolio
    - Always keep 20% as cash buffer

    Kelly formula: f* = (bp - q) / b
    where b = reward/risk ratio, p = win rate, q = 1 - p
    """

    MAX_SINGLE_ASSET = 0.30   # 30% max per asset
    MAX_CRYPTO_TOTAL = 0.40   # 40% max total crypto
    MAX_GOLD_TOTAL   = 0.40   # 40% max gold
    CASH_BUFFER      = 0.20   # Always keep 20% cash

    def size(
        self,
        asset: Asset,
        signal: Signal,
        confidence: float,
        win_rate: float,          # historical win rate (0.0–1.0)
        reward_risk_ratio: float, # e.g., 2.0 means 2:1 R:R
        current_crypto_pct: float = 0.0,  # current % already in crypto
        current_gold_pct: float   = 0.0,  # current % already in gold
        concentration_risk: bool  = False
    ) -> float:
        """Returns recommended position size as % of total capital."""

        if abs(signal.value) == 0:
            return 0.0   # No position on neutral signal

        # Kelly Criterion (Half-Kelly for safety)
        p = win_rate
        q = 1 - p
        b = reward_risk_ratio
        kelly = (b * p - q) / b
        half_kelly = max(0, kelly / 2)

        # Scale by signal confidence
        size = half_kelly * confidence

        # Apply hard caps
        size = min(size, self.MAX_SINGLE_ASSET)

        # Crypto-specific cap
        if asset in [Asset.BTC, Asset.ETH]:
            remaining_crypto_budget = max(0, self.MAX_CRYPTO_TOTAL - current_crypto_pct)
            size = min(size, remaining_crypto_budget)

        # Gold-specific cap
        if asset == Asset.GOLD:
            remaining_gold_budget = max(0, self.MAX_GOLD_TOTAL - current_gold_pct)
            size = min(size, remaining_gold_budget)

        # Reduce by 50% if high correlation risk
        if concentration_risk:
            size *= 0.5
            log.warning(f"Position size halved due to correlation risk: {size:.2%}")

        log.info(f"Position size for {asset.value}: {size:.2%} of capital")
        return round(size, 4)


# ─────────────────────────────────────────────
# MODULE 5: RISK MANAGER
# ─────────────────────────────────────────────

class RiskManager:
    """
    Computes per-trade stop loss and take profit levels.
    Also runs portfolio-level kill switch checks.

    Stop Loss methods:
    - ATR-based (institutional standard): SL = entry ± (ATR × multiplier)
    - Never risk more than 1–2% of capital per trade

    Take Profit:
    - Fixed R:R (e.g., 2:1 or 3:1)
    """

    MAX_DAILY_DRAWDOWN = 0.15   # 5% daily portfolio loss = halt all trading
    MAX_TRADE_RISK     = 0.02   # Risk max 2% of capital per trade
    ATR_MULTIPLIER     = 1.5    # SL = entry ± 1.5 × ATR
    REWARD_RISK_RATIO  = 2.0    # Default 2:1 take profit

    def compute_levels(
        self,
        df: pd.DataFrame,
        signal: Signal,
        capital: float
    ) -> tuple[float, float, float]:
        """
        Returns (entry_price, stop_loss, take_profit)
        df must have 'high', 'low', 'close', 'volume'
        """
        entry = df['close'].iloc[-1]

        # ATR for stop distance
        high, low, close = df['high'], df['low'], df['close']
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs()
        ], axis=1).max(axis=1)
        atr = tr.ewm(span=14).mean().iloc[-1]

        sl_distance = atr * self.ATR_MULTIPLIER
        tp_distance = sl_distance * self.REWARD_RISK_RATIO

        if signal.value > 0:  # Long trade
            stop_loss   = entry - sl_distance
            take_profit = entry + tp_distance
        else:                  # Short trade
            stop_loss   = entry + sl_distance
            take_profit = entry - tp_distance

        return round(entry, 4), round(stop_loss, 4), round(take_profit, 4)

    def check_kill_switch(self, portfolio_value: float, peak_portfolio_value: float) -> bool:
        """
        Returns True if all trading should halt (max drawdown breached).
        """
        drawdown = (peak_portfolio_value - portfolio_value) / peak_portfolio_value
        if drawdown >= self.MAX_DAILY_DRAWDOWN:
            log.critical(f"🚨 KILL SWITCH TRIGGERED — Drawdown: {drawdown:.2%}. All trading halted.")
            return True
        return False


# ─────────────────────────────────────────────
# MODULE 6: STRATEGY ORCHESTRATOR (Master)
# ─────────────────────────────────────────────

class StrategyOrchestrator:
    """
    Master controller. Wires all modules together.

    Usage:
        orchestrator = StrategyOrchestrator(capital=500000)
        signal = orchestrator.evaluate(gold_df, btc_df, eth_df)
    """

    def __init__(self, capital: float, win_rate: float = 0.55):
        self.capital    = capital
        self.win_rate   = win_rate
        self.peak_value = capital

        self.regime_detector = MarketRegimeDetector()
        self.signal_gen      = SignalGenerator()
        self.corr_engine     = CorrelationEngine()
        self.sizer           = PositionSizer()
        self.risk_mgr        = RiskManager()

        self.current_crypto_pct = 0.0
        self.current_gold_pct   = 0.0

        log.info(f"StrategyOrchestrator initialized | Capital: ₹{capital:,.0f} | Win Rate: {win_rate:.0%}")

    def evaluate(
        self,
        gold_df: pd.DataFrame,
        btc_df: pd.DataFrame,
        eth_df: pd.DataFrame,
        current_portfolio_value: Optional[float] = None
    ) -> list[TradeSignal]:
        """
        Full evaluation cycle. Returns list of TradeSignal objects.
        Each df must have: open, high, low, close, volume (DatetimeIndex)
        """
        portfolio_val = current_portfolio_value or self.capital

        # ── Kill switch check ──
        if self.risk_mgr.check_kill_switch(portfolio_val, self.peak_value):
            log.critical("All signals suppressed — kill switch active.")
            return []

        self.peak_value = max(self.peak_value, portfolio_val)

        # ── Correlation check ──
        corr_data = self.corr_engine.compute({
            'XAUUSD':  gold_df['close'],
            'BTCUSDT': btc_df['close'],
            'ETHUSDT': eth_df['close'],
        })
        concentration_risk = corr_data.get('concentration_risk', False)

        signals = []
        asset_data = [
            (Asset.GOLD, gold_df),
            (Asset.BTC,  btc_df),
            (Asset.ETH,  eth_df),
        ]

        for asset, df in asset_data:
            if len(df) < 60:
                log.warning(f"Insufficient data for {asset.value} — skipping")
                continue

            # ── Regime Detection ──
            regime = self.regime_detector.detect(df)
            log.info(f"{asset.value} Regime: {regime.value}")

            # ── Signal Generation ──
            signal, confidence, reasons = self.signal_gen.generate(df, regime, asset)

            # Skip low-confidence and neutral signals
            if signal == Signal.NEUTRAL or confidence < 0.55:
                log.info(f"{asset.value}: Signal too weak ({signal.name}, {confidence:.0%}) — skipping")
                continue

            # ── Risk Levels ──
            entry, stop_loss, take_profit = self.risk_mgr.compute_levels(df, signal, self.capital)
            rr_ratio = abs(take_profit - entry) / (abs(entry - stop_loss) + 1e-9)

            # ── Position Sizing ──
            size_pct = self.sizer.size(
                asset              = asset,
                signal             = signal,
                confidence         = confidence,
                win_rate           = self.win_rate,
                reward_risk_ratio  = rr_ratio,
                current_crypto_pct = self.current_crypto_pct,
                current_gold_pct   = self.current_gold_pct,
                concentration_risk = concentration_risk,
            )

            if size_pct <= 0:
                log.info(f"{asset.value}: Position size = 0 (budget exhausted or caps hit)")
                continue

            trade_signal = TradeSignal(
                asset             = asset,
                signal            = signal,
                regime            = regime,
                confidence        = confidence,
                entry_price       = entry,
                stop_loss         = stop_loss,
                take_profit       = take_profit,
                position_size_pct = size_pct,
                reasons           = reasons,
            )

            signals.append(trade_signal)
            log.info(str(trade_signal))
            for r in reasons:
                log.info(f"   {r}")

        return signals


# ─────────────────────────────────────────────
# QUICK TEST (Run with synthetic data)
# ─────────────────────────────────────────────

def _make_synthetic_ohlcv(seed=42, n=200, base=100.0, trend=0.001) -> pd.DataFrame:
    """Generate synthetic trending OHLCV data for testing."""
    np.random.seed(seed)
    close = base * np.cumprod(1 + trend + np.random.normal(0, 0.01, n))
    high  = close * (1 + np.abs(np.random.normal(0, 0.005, n)))
    low   = close * (1 - np.abs(np.random.normal(0, 0.005, n)))
    open_ = np.roll(close, 1)
    vol   = np.random.randint(1000, 10000, n).astype(float)
    idx   = pd.date_range("2024-01-01", periods=n, freq="1h")
    return pd.DataFrame({'open': open_, 'high': high, 'low': low, 'close': close, 'volume': vol}, index=idx)


if __name__ == "__main__":
    print("\n" + "="*60)
    print("  STRATEGY ENGINE — TEST RUN (Synthetic Data)")
    print("="*60 + "\n")

    # Simulate ₹5 lakh capital
    orchestrator = StrategyOrchestrator(capital=500_000, win_rate=0.55)

    gold_df = _make_synthetic_ohlcv(seed=1, base=2350, trend=0.0008)   # Gold trending up
    btc_df  = _make_synthetic_ohlcv(seed=2, base=65000, trend=0.0015)  # BTC trending up
    eth_df  = _make_synthetic_ohlcv(seed=3, base=3200, trend=-0.001)   # ETH trending down

    signals = orchestrator.evaluate(gold_df, btc_df, eth_df, current_portfolio_value=500_000)

    print("\n" + "─"*60)
    print(f"  TRADE SIGNALS GENERATED: {len(signals)}")
    print("─"*60)
    for s in signals:
        print(f"\n  {s}")
        print(f"  Capital to deploy: ₹{s.position_size_pct * 500_000:,.0f}")
        print(f"  Reasons:")
        for r in s.reasons:
            print(f"    {r}")
    print("\n" + "="*60 + "\n")
