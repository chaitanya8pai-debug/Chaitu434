"""
=============================================================
  MASTER BOT — Main Entry Point
  Wires: Zerodha (Gold) + Binance (BTC/ETH) + Strategy Engine
=============================================================

Run this file to start the bot.
  python main_bot.py

MODES (set in .env):
  BOT_MODE=paper      → Paper trading (default, safe)
  BOT_MODE=live       → Real money (only after 30 days paper)

LOOP:
  Every SCAN_INTERVAL seconds:
    1. Fetch latest OHLCV from Zerodha (Gold) + Binance (BTC/ETH)
    2. Run StrategyOrchestrator.evaluate()
    3. If signal → compute size → place order
    4. Check kill switch
    5. Send Telegram alert
    6. Sleep → repeat
=============================================================
"""

import os
import time
import logging
import traceback
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

from strategy_engine   import StrategyOrchestrator, Asset, Signal
from zerodha_connector import ZerodhaConnector
from binance_connector import BinanceConnector

load_dotenv()
log = logging.getLogger("MasterBot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")


# ─────────────────────────────────────────────
# CONFIG (all from .env)
# ─────────────────────────────────────────────

BOT_MODE       = os.getenv("BOT_MODE", "paper")           # "paper" or "live"
CAPITAL_INR    = float(os.getenv("CAPITAL_INR", 500000))  # Total capital in INR
SCAN_INTERVAL  = int(os.getenv("SCAN_INTERVAL", 3600))    # Seconds between scans (3600 = 1hr)
CANDLE_INTERVAL= os.getenv("CANDLE_INTERVAL", "1h")       # "1h", "4h", "1d"
CANDLES_BACK   = int(os.getenv("CANDLES_BACK", 200))      # Historical candles to load
WIN_RATE       = float(os.getenv("WIN_RATE", 0.55))       # Historical win rate estimate
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "")

PAPER_TRADE = BOT_MODE != "live"

if not PAPER_TRADE:
    log.critical("🔴 LIVE MODE ACTIVE — Real capital at risk!")
else:
    log.info("🧪 PAPER TRADE MODE — Safe to run")


# ─────────────────────────────────────────────
# TELEGRAM ALERTS
# ─────────────────────────────────────────────

def send_telegram(message: str):
    """Send alert to your Telegram bot."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        log.info(f"[No Telegram] {message}")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT,
            "text":    message,
            "parse_mode": "HTML"
        }, timeout=5)
    except Exception as e:
        log.warning(f"Telegram alert failed: {e}")


def format_alert(trade_signal) -> str:
    """Format a TradeSignal into a Telegram message."""
    emoji = {"STRONG_BUY": "🚀", "BUY": "🟢", "SELL": "🔴", "STRONG_SELL": "💀"}.get(
        trade_signal.signal.name, "⚪"
    )
    return (
        f"{emoji} <b>{trade_signal.asset.value}</b> — {trade_signal.signal.name}\n"
        f"📊 Regime: {trade_signal.regime.value}\n"
        f"💯 Confidence: {trade_signal.confidence:.0%}\n"
        f"💰 Entry: {trade_signal.entry_price:,.2f}\n"
        f"🛑 Stop Loss: {trade_signal.stop_loss:,.2f}\n"
        f"🎯 Take Profit: {trade_signal.take_profit:,.2f}\n"
        f"📦 Size: {trade_signal.position_size_pct:.1%} of capital\n"
        f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M IST')}"
    )


# ─────────────────────────────────────────────
# MASTER BOT CLASS
# ─────────────────────────────────────────────

class MasterBot:

    def __init__(self):
        log.info("="*55)
        log.info("  MASTER BOT INITIALIZING")
        log.info(f"  Mode: {'PAPER' if PAPER_TRADE else 'LIVE'}")
        log.info(f"  Capital: ₹{CAPITAL_INR:,.0f}")
        log.info(f"  Scan interval: {SCAN_INTERVAL}s ({SCAN_INTERVAL//60} min)")
        log.info("="*55)

        # Strategy brain
        self.orchestrator = StrategyOrchestrator(
            capital=CAPITAL_INR, win_rate=WIN_RATE
        )

        # Exchange connectors
        self.zerodha = ZerodhaConnector(paper_trade=PAPER_TRADE)
        self.binance  = BinanceConnector(paper_trade=PAPER_TRADE, use_futures=True)

        self._running    = False
        self._scan_count = 0

    def setup(self, zerodha_request_token: str = None):
        """
        One-time setup. Call this before start().
        
        zerodha_request_token: only needed on first run or after midnight.
        """
        log.info("🔌 Connecting to exchanges...")

        # Zerodha
        try:
            self.zerodha.authenticate(request_token=zerodha_request_token)
            log.info("✅ Zerodha connected")
        except RuntimeError as e:
            log.error(f"❌ Zerodha auth failed: {e}")
            raise

        # Binance
        try:
            self.binance.connect()
            snap = self.binance.get_market_snapshot()
            log.info(f"✅ Binance connected | BTC: ${snap['BTC_USDT']:,.0f} | ETH: ${snap['ETH_USDT']:,.0f}")
        except Exception as e:
            log.error(f"❌ Binance connection failed: {e}")
            raise

        send_telegram(
            f"🤖 <b>Bot started</b>\n"
            f"Mode: {'PAPER' if PAPER_TRADE else '🔴 LIVE'}\n"
            f"Capital: ₹{CAPITAL_INR:,.0f}\n"
            f"Assets: Gold (MCX) + BTC + ETH"
        )
        log.info("✅ Setup complete. Call start() to begin scanning.")

    def _fetch_data(self) -> tuple:
        """Fetch latest OHLCV for all three assets."""
        log.info(f"📡 Scan #{self._scan_count} — Fetching market data...")

        gold_df = self.zerodha.data.get_historical(
            symbol="GOLDPETAL", interval=CANDLE_INTERVAL, days_back=CANDLES_BACK
        )
        btc_df = self.binance.data.get_historical(
            symbol="BTC", interval=CANDLE_INTERVAL, limit=CANDLES_BACK
        )
        eth_df = self.binance.data.get_historical(
            symbol="ETH", interval=CANDLE_INTERVAL, limit=CANDLES_BACK
        )

        log.info(
            f"📊 Data loaded | "
            f"Gold: {len(gold_df)} candles (latest ₹{gold_df['close'].iloc[-1]:,.2f}) | "
            f"BTC: {len(btc_df)} candles (${btc_df['close'].iloc[-1]:,.0f}) | "
            f"ETH: {len(eth_df)} candles (${eth_df['close'].iloc[-1]:,.0f})"
        )
        return gold_df, btc_df, eth_df

    def _get_current_portfolio_value(self) -> float:
        """Estimate total portfolio value in INR (simplified)."""
        gold_val  = self.zerodha.orders.get_portfolio_value()
        usdt_val  = self.binance.orders.get_portfolio_value_usdt()
        # Rough USDT → INR conversion (use live rate in production)
        inr_rate  = float(os.getenv("USDT_TO_INR", 84))
        crypto_inr = usdt_val * inr_rate
        return gold_val + crypto_inr

    def _execute_signal(self, trade_signal):
        """Execute a trade signal on the appropriate exchange."""
        capital_to_deploy = trade_signal.position_size_pct * CAPITAL_INR
        direction = "BUY" if trade_signal.signal.value > 0 else "SELL"

        if trade_signal.asset == Asset.GOLD:
            # Zerodha MCX
            order_id = self.zerodha.orders.place_order(
                symbol          = "GOLDPETAL",
                direction       = direction,
                capital_to_deploy = capital_to_deploy,
                limit_price     = trade_signal.entry_price,
                stop_loss       = trade_signal.stop_loss,
                take_profit     = trade_signal.take_profit,
            )
        elif trade_signal.asset == Asset.BTC:
            # Binance BTC
            usdt_rate = float(os.getenv("USDT_TO_INR", 84))
            capital_usdt = capital_to_deploy / usdt_rate
            order_id = self.binance.orders.place_order(
                symbol       = "BTC",
                direction    = direction,
                capital_usdt = capital_usdt,
                limit_price  = trade_signal.entry_price,
                stop_loss    = trade_signal.stop_loss,
                take_profit  = trade_signal.take_profit,
            )
        elif trade_signal.asset == Asset.ETH:
            # Binance ETH
            usdt_rate = float(os.getenv("USDT_TO_INR", 84))
            capital_usdt = capital_to_deploy / usdt_rate
            order_id = self.binance.orders.place_order(
                symbol       = "ETH",
                direction    = direction,
                capital_usdt = capital_usdt,
                limit_price  = trade_signal.entry_price,
                stop_loss    = trade_signal.stop_loss,
                take_profit  = trade_signal.take_profit,
            )
        else:
            order_id = None

        return order_id

    def _scan(self):
        """One full scan cycle."""
        self._scan_count += 1

        try:
            # 1. Fetch data
            gold_df, btc_df, eth_df = self._fetch_data()

            # 2. Portfolio value for kill switch
            portfolio_value = self._get_current_portfolio_value()

            # 3. Run strategy
            signals = self.orchestrator.evaluate(
                gold_df, btc_df, eth_df,
                current_portfolio_value=portfolio_value
            )

            if not signals:
                log.info("⚪ No actionable signals this scan — waiting for next cycle")
                send_telegram(f"⚪ Scan #{self._scan_count} — No signals. Markets watched.")
                return

            # 4. Execute signals
            for sig in signals:
                log.info(f"⚡ Executing: {sig}")
                order_id = self._execute_signal(sig)

                if order_id:
                    alert = format_alert(sig)
                    capital_deployed = sig.position_size_pct * CAPITAL_INR
                    alert += f"\n📋 Order ID: <code>{order_id}</code>"
                    alert += f"\n💸 Capital deployed: ₹{capital_deployed:,.0f}"
                    send_telegram(alert)
                    log.info(f"✅ Order placed: {order_id}")
                else:
                    log.warning(f"⚠️  Signal generated but order failed for {sig.asset.value}")

        except Exception as e:
            error_msg = f"❌ Scan #{self._scan_count} error: {str(e)}"
            log.error(error_msg)
            log.error(traceback.format_exc())
            send_telegram(f"⚠️ <b>Bot Error</b>\n{error_msg}")

    def start(self):
        """Start the main bot loop."""
        self._running = True
        log.info(f"🚀 Bot started | Scanning every {SCAN_INTERVAL}s")
        send_telegram("🚀 Bot loop started")

        while self._running:
            start_time = time.time()
            self._scan()
            elapsed = time.time() - start_time
            sleep_time = max(0, SCAN_INTERVAL - elapsed)
            log.info(f"💤 Sleeping {sleep_time:.0f}s until next scan...")
            time.sleep(sleep_time)

    def stop(self):
        """Gracefully stop the bot."""
        self._running = False
        self.zerodha.data.stop_live_feed()
        self.binance.data.stop_live_feed()
        send_telegram("🛑 Bot stopped gracefully")
        log.info("🛑 Bot stopped")


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    bot = MasterBot()

    # ── FIRST RUN: Uncomment to generate Zerodha login URL ──
    # from zerodha_connector import ZerodhaAuthManager
    # auth = ZerodhaAuthManager()
    # auth.get_login_url()    # Open URL in browser
    # Then run: bot.setup(zerodha_request_token="paste_token_here")

    # ── SUBSEQUENT RUNS (token saved) ──
    bot.setup()   # Uses saved .zerodha_token
    bot.start()   # Begins infinite scan loop
