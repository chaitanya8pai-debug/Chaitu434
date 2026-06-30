"""
=============================================================
  ZERODHA KITE API CONNECTOR
  Asset: Gold (MCX — GOLDPETAL / GOLD)
  Purpose: Live OHLCV data feed + order execution
=============================================================

SETUP STEPS (do this once):
  1. pip install kiteconnect pandas python-dotenv
  2. Create .env file with your credentials (see bottom)
  3. Run generate_access_token() once per day to get token
  4. Token expires at midnight IST — re-auth daily

ZERODHA API DOCS: https://kite.trade/docs/connect/v3/
=============================================================
"""

import os
import time
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional, Callable
from dotenv import load_dotenv
import pandas as pd

# Kite SDK — install via: pip install kiteconnect
try:
    from kiteconnect import KiteConnect, KiteTicker
except ImportError:
    raise ImportError("Run: pip install kiteconnect")

load_dotenv()
log = logging.getLogger("ZerodhaConnector")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")


# ─────────────────────────────────────────────
# CONSTANTS — MCX Gold instrument tokens
# These are Zerodha's internal instrument IDs
# Verify latest tokens at: https://api.kite.trade/instruments/MCX
# ─────────────────────────────────────────────

MCX_GOLD_TOKENS = {
    "GOLD":      57392135,  # Standard Gold (1 kg lot)
    "GOLDM":     57392903,  # Mini Gold (100g lot)
    "GOLDPETAL": 57399047,  # Petal Gold (1g lot — best for small capital)
    "GOLDGUINEA":57395847,  # Guinea Gold (8g lot)
}

# Recommended for retail/semi-institutional: GOLDPETAL or GOLDM
DEFAULT_GOLD_SYMBOL = "GOLDPETAL"
DEFAULT_GOLD_TOKEN  = MCX_GOLD_TOKENS[DEFAULT_GOLD_SYMBOL]

MCX_TRADING_HOURS = {
    "start": "09:00",   # 9 AM IST
    "end":   "23:30",   # 11:30 PM IST
    "tz":    "Asia/Kolkata"
}


# ─────────────────────────────────────────────
# AUTH MANAGER
# ─────────────────────────────────────────────

class ZerodhaAuthManager:
    """
    Handles Zerodha OAuth flow.
    Zerodha tokens expire daily at midnight — must re-auth each day.
    
    Flow:
      1. Generate login URL
      2. User logs in, gets request_token from redirect URL
      3. Exchange request_token → access_token
      4. Use access_token for all API calls
    """

    def __init__(self):
        self.api_key    = os.getenv("ZERODHA_API_KEY")
        self.api_secret = os.getenv("ZERODHA_API_SECRET")
        self.kite       = KiteConnect(api_key=self.api_key)

        if not self.api_key or not self.api_secret:
            raise EnvironmentError(
                "Missing ZERODHA_API_KEY or ZERODHA_API_SECRET in .env file"
            )

    def get_login_url(self) -> str:
        """Step 1: Generate the URL the user must visit to log in."""
        url = self.kite.login_url()
        log.info(f"🔐 Open this URL in your browser to log in:\n\n  {url}\n")
        return url

    def generate_access_token(self, request_token: str) -> str:
        """
        Step 2: After login, Zerodha redirects to:
          https://your-redirect-url/?request_token=XXXX&action=login&status=success
        
        Copy the request_token from that URL and pass it here.
        """
        session = self.kite.generate_session(request_token, api_secret=self.api_secret)
        access_token = session["access_token"]
        self.kite.set_access_token(access_token)

        # Save token so you don't re-auth unless needed
        with open(".zerodha_token", "w") as f:
            f.write(access_token)

        log.info("✅ Zerodha access token generated and saved to .zerodha_token")
        return access_token

    def load_saved_token(self) -> Optional[str]:
        """Load previously saved token (valid until midnight IST)."""
        try:
            with open(".zerodha_token", "r") as f:
                token = f.read().strip()
            self.kite.set_access_token(token)
            # Quick validation check
            self.kite.profile()
            log.info("✅ Zerodha token loaded from file — still valid")
            return token
        except Exception:
            log.warning("⚠️  Saved token invalid or expired — re-authentication needed")
            return None


# ─────────────────────────────────────────────
# MARKET HOURS CHECK
# ─────────────────────────────────────────────

def is_mcx_market_open() -> bool:
    """Check if MCX is currently open for trading (9 AM – 11:30 PM IST)."""
    try:
        import pytz
        ist  = pytz.timezone("Asia/Kolkata")
        now  = datetime.now(ist)
        # Skip weekends
        if now.weekday() >= 5:
            return False
        start = now.replace(hour=9,  minute=0,  second=0, microsecond=0)
        end   = now.replace(hour=23, minute=30, second=0, microsecond=0)
        return start <= now <= end
    except ImportError:
        # Fallback without pytz
        now = datetime.utcnow() + timedelta(hours=5, minutes=30)
        if now.weekday() >= 5:
            return False
        return 9 <= now.hour < 23 or (now.hour == 23 and now.minute <= 30)


# ─────────────────────────────────────────────
# DATA CONNECTOR (Historical + Live)
# ─────────────────────────────────────────────

class ZerodhaDataConnector:
    """
    Fetches OHLCV data from Zerodha — both historical and live (WebSocket).
    """

    INTERVAL_MAP = {
        "1m":  "minute",
        "3m":  "3minute",
        "5m":  "5minute",
        "15m": "15minute",
        "30m": "30minute",
        "1h":  "60minute",
        "1d":  "day",
    }

    def __init__(self, kite: KiteConnect):
        self.kite = kite
        self._live_df   = pd.DataFrame()
        self._ticker    = None
        self._callbacks = []

    def get_historical(
        self,
        symbol: str = DEFAULT_GOLD_SYMBOL,
        interval: str = "1h",
        days_back: int = 90
    ) -> pd.DataFrame:
        """
        Fetch historical OHLCV data for Gold.
        Returns DataFrame with columns: open, high, low, close, volume
        """
        if not is_mcx_market_open() and interval == "1m":
            log.warning("MCX is closed — historical data still accessible, live may be stale")

        token     = MCX_GOLD_TOKENS.get(symbol, DEFAULT_GOLD_TOKEN)
        kite_int  = self.INTERVAL_MAP.get(interval, "60minute")
        from_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
        to_date   = datetime.now().strftime("%Y-%m-%d")

        log.info(f"📥 Fetching {symbol} historical data | {interval} | Last {days_back} days")

        try:
            records = self.kite.historical_data(
                instrument_token=token,
                from_date=from_date,
                to_date=to_date,
                interval=kite_int,
                continuous=False,  # Set True for continuous futures (handles expiry rollover)
                oi=False
            )
        except Exception as e:
            log.error(f"❌ Historical data fetch failed: {e}")
            raise

        df = pd.DataFrame(records)
        df.rename(columns={"date": "datetime"}, inplace=True)
        df.set_index("datetime", inplace=True)
        df.index = pd.to_datetime(df.index)
        df = df[["open", "high", "low", "close", "volume"]]
        df = df.dropna()

        log.info(f"✅ {len(df)} candles loaded for {symbol}")
        return df

    def get_ltp(self, symbol: str = DEFAULT_GOLD_SYMBOL) -> float:
        """Get Last Traded Price for Gold."""
        token = MCX_GOLD_TOKENS.get(symbol, DEFAULT_GOLD_TOKEN)
        ltp_data = self.kite.ltp(f"MCX:{symbol}")
        return ltp_data[f"MCX:{symbol}"]["last_price"]

    def start_live_feed(
        self,
        symbol: str = DEFAULT_GOLD_SYMBOL,
        on_tick: Optional[Callable] = None
    ):
        """
        Start WebSocket live feed for real-time ticks.
        on_tick(tick_df) is called whenever a new candle is ready.
        """
        if not is_mcx_market_open():
            log.warning("⚠️  MCX is currently closed. Live feed will connect but may not tick.")

        token = MCX_GOLD_TOKENS.get(symbol, DEFAULT_GOLD_TOKEN)
        api_key      = os.getenv("ZERODHA_API_KEY")
        access_token = os.getenv("ZERODHA_ACCESS_TOKEN") or open(".zerodha_token").read().strip()

        ticker = KiteTicker(api_key, access_token)
        self._candle_buffer = []
        self._last_candle_minute = None

        def on_ticks(ws, ticks):
            for tick in ticks:
                if tick["instrument_token"] != token:
                    continue
                price  = tick["last_price"]
                volume = tick.get("volume_traded", 0)
                now    = datetime.now()
                minute = now.replace(second=0, microsecond=0)

                if self._last_candle_minute is None:
                    self._last_candle_minute = minute

                if minute != self._last_candle_minute:
                    # New minute — emit completed candle
                    if self._candle_buffer:
                        prices  = [t[0] for t in self._candle_buffer]
                        volumes = [t[1] for t in self._candle_buffer]
                        candle  = {
                            "datetime": self._last_candle_minute,
                            "open":   prices[0],
                            "high":   max(prices),
                            "low":    min(prices),
                            "close":  prices[-1],
                            "volume": sum(volumes),
                        }
                        log.info(f"🕯️  New candle | O:{candle['open']} H:{candle['high']} L:{candle['low']} C:{candle['close']}")

                        # Append to live DataFrame
                        new_row = pd.DataFrame([candle]).set_index("datetime")
                        self._live_df = pd.concat([self._live_df, new_row])

                        if on_tick:
                            on_tick(self._live_df.copy())

                    self._candle_buffer = []
                    self._last_candle_minute = minute

                self._candle_buffer.append((price, volume))

        def on_connect(ws, response):
            log.info(f"✅ Zerodha WebSocket connected for {symbol}")
            ws.subscribe([token])
            ws.set_mode(ws.MODE_FULL, [token])

        def on_close(ws, code, reason):
            log.warning(f"⚠️  WebSocket closed: {code} — {reason}")

        def on_error(ws, code, reason):
            log.error(f"❌ WebSocket error: {code} — {reason}")

        ticker.on_ticks   = on_ticks
        ticker.on_connect = on_connect
        ticker.on_close   = on_close
        ticker.on_error   = on_error

        self._ticker = ticker
        thread = threading.Thread(target=ticker.connect, kwargs={"threaded": True})
        thread.daemon = True
        thread.start()
        log.info(f"🚀 Live feed started for {symbol}")

    def stop_live_feed(self):
        if self._ticker:
            self._ticker.close()
            log.info("🛑 Live feed stopped")


# ─────────────────────────────────────────────
# ORDER EXECUTOR
# ─────────────────────────────────────────────

class ZerodhaOrderExecutor:
    """
    Places, modifies, and cancels MCX Gold orders via Kite API.

    IMPORTANT:
    - Always use LIMIT orders (never market orders) for better fills
    - MCX Gold lot sizes: GOLD=1kg, GOLDM=100g, GOLDPETAL=1g
    - Verify margin requirements before placing orders
    """

    LOT_SIZES = {
        "GOLD":       1000,   # grams (1 kg)
        "GOLDM":       100,   # grams (100g)
        "GOLDPETAL":     1,   # gram  (1g)
        "GOLDGUINEA":    8,   # grams (8g)
    }

    def __init__(self, kite: KiteConnect, paper_trade: bool = True):
        self.kite        = kite
        self.paper_trade = paper_trade   # ← Set False ONLY for live trading
        self._orders     = []

        if paper_trade:
            log.warning("🧪 PAPER TRADE MODE — No real orders will be placed")
        else:
            log.warning("🔴 LIVE TRADE MODE — Real orders WILL be placed. Be careful.")

    def _compute_lots(self, capital_to_deploy: float, price: float, symbol: str) -> int:
        """
        Compute how many lots to buy given capital and current price.
        MCX Gold is priced per 10 grams — adjust accordingly.
        """
        lot_gram  = self.LOT_SIZES.get(symbol, 1)
        price_per_gram = price / 10   # Zerodha quotes per 10g
        cost_per_lot   = price_per_gram * lot_gram
        lots = max(1, int(capital_to_deploy / cost_per_lot))
        return lots

    def place_order(
        self,
        symbol: str,
        direction: str,          # "BUY" or "SELL"
        capital_to_deploy: float,
        limit_price: float,
        stop_loss: float,
        take_profit: float,
    ) -> Optional[str]:
        """
        Places a bracket/cover order on MCX Gold.
        Returns order_id if successful.
        """
        lots = self._compute_lots(capital_to_deploy, limit_price, symbol)
        log.info(
            f"📋 Order | {direction} {lots} lots {symbol} @ ₹{limit_price:.2f} | "
            f"SL: ₹{stop_loss:.2f} | TP: ₹{take_profit:.2f} | "
            f"Capital: ₹{capital_to_deploy:,.0f}"
        )

        if self.paper_trade:
            fake_id = f"PAPER_{int(time.time())}"
            self._orders.append({
                "order_id": fake_id,
                "symbol": symbol,
                "direction": direction,
                "lots": lots,
                "entry": limit_price,
                "sl": stop_loss,
                "tp": take_profit,
                "status": "OPEN",
                "timestamp": datetime.now().isoformat(),
            })
            log.info(f"📝 Paper order placed: {fake_id}")
            return fake_id

        # ── Live order ──
        try:
            sl_distance = abs(limit_price - stop_loss)
            order_id = self.kite.place_order(
                tradingsymbol  = symbol,
                exchange       = self.kite.EXCHANGE_MCX,
                transaction_type = (
                    self.kite.TRANSACTION_TYPE_BUY if direction == "BUY"
                    else self.kite.TRANSACTION_TYPE_SELL
                ),
                quantity       = lots,
                order_type     = self.kite.ORDER_TYPE_LIMIT,
                product        = self.kite.PRODUCT_NRML,   # NRML for overnight, MIS for intraday
                price          = limit_price,
                stoploss       = sl_distance,              # For SL-M orders
                validity       = self.kite.VALIDITY_DAY,
                variety        = self.kite.VARIETY_REGULAR,
            )
            log.info(f"✅ Live order placed: {order_id}")
            return order_id

        except Exception as e:
            log.error(f"❌ Order placement failed: {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        if self.paper_trade:
            for o in self._orders:
                if o["order_id"] == order_id:
                    o["status"] = "CANCELLED"
            log.info(f"📝 Paper order {order_id} cancelled")
            return True
        try:
            self.kite.cancel_order(variety=self.kite.VARIETY_REGULAR, order_id=order_id)
            log.info(f"✅ Order {order_id} cancelled")
            return True
        except Exception as e:
            log.error(f"❌ Cancel failed: {e}")
            return False

    def get_open_positions(self) -> pd.DataFrame:
        """Get all open positions."""
        if self.paper_trade:
            return pd.DataFrame([o for o in self._orders if o["status"] == "OPEN"])
        try:
            pos = self.kite.positions()
            df  = pd.DataFrame(pos["net"])
            return df[df["quantity"] != 0]
        except Exception as e:
            log.error(f"❌ Positions fetch failed: {e}")
            return pd.DataFrame()

    def get_portfolio_value(self) -> float:
        """Get total portfolio value including margins."""
        if self.paper_trade:
            log.info("Paper trade mode — returning estimated portfolio value")
            return 500_000.0   # placeholder
        try:
            margins = self.kite.margins(segment="commodity")
            return margins["net"]
        except Exception as e:
            log.error(f"❌ Portfolio value fetch failed: {e}")
            return 0.0


# ─────────────────────────────────────────────
# MAIN ZERODHA CONNECTOR (combines everything)
# ─────────────────────────────────────────────

class ZerodhaConnector:
    """
    Top-level connector — import this in your main bot.
    
    Usage:
        from zerodha_connector import ZerodhaConnector
        
        zc = ZerodhaConnector(paper_trade=True)
        zc.authenticate()
        
        gold_df = zc.data.get_historical("GOLDPETAL", interval="1h", days_back=90)
        zc.data.start_live_feed("GOLDPETAL", on_tick=my_callback)
        
        order_id = zc.orders.place_order(
            symbol="GOLDPETAL", direction="BUY",
            capital_to_deploy=50000,
            limit_price=gold_df['close'].iloc[-1],
            stop_loss=..., take_profit=...
        )
    """

    def __init__(self, paper_trade: bool = True):
        self.paper_trade = paper_trade
        self.auth        = ZerodhaAuthManager()
        self._kite       = self.auth.kite
        self.data        = None
        self.orders      = None

    def authenticate(self, request_token: Optional[str] = None):
        """
        Authenticate with Zerodha.
        - First run: call get_login_url(), log in, then pass request_token here.
        - Subsequent runs: loads saved token automatically.
        """
        if request_token:
            self.auth.generate_access_token(request_token)
        else:
            saved = self.auth.load_saved_token()
            if not saved:
                url = self.auth.get_login_url()
                raise RuntimeError(
                    f"No valid token. Log in at:\n{url}\n"
                    "Then call authenticate(request_token='your_token_here')"
                )

        self.data   = ZerodhaDataConnector(self._kite)
        self.orders = ZerodhaOrderExecutor(self._kite, paper_trade=self.paper_trade)
        log.info("✅ ZerodhaConnector ready")

    def market_status(self) -> dict:
        return {
            "mcx_open": is_mcx_market_open(),
            "timestamp": datetime.now().isoformat(),
            "note": "MCX trades 9 AM – 11:30 PM IST, Mon–Fri"
        }


# ─────────────────────────────────────────────
# .env FILE TEMPLATE (create this in your project root)
# ─────────────────────────────────────────────
"""
Save this as .env in your project folder:

ZERODHA_API_KEY=your_api_key_here
ZERODHA_API_SECRET=your_api_secret_here
ZERODHA_ACCESS_TOKEN=   # Leave blank — auto-populated after login
ZERODHA_REDIRECT_URL=https://127.0.0.1:5000/callback

Get API key at: https://developers.kite.trade/
"""

if __name__ == "__main__":
    # ── Quick connection test ──
    print("\n" + "="*55)
    print("  ZERODHA CONNECTOR — Connection Test")
    print("="*55)

    zc = ZerodhaConnector(paper_trade=True)

    print("\n📋 MCX Market Status:")
    print(f"   Open: {is_mcx_market_open()}")
    print(f"   Hours: 9:00 AM – 11:30 PM IST, Mon–Fri")

    print("\n⚙️  Next steps:")
    print("   1. Add credentials to .env file")
    print("   2. Call zc.authenticate()")
    print("   3. gold_df = zc.data.get_historical('GOLDPETAL', '1h', 90)")
    print("   4. Feed gold_df into StrategyOrchestrator")
    print("\n" + "="*55 + "\n")
