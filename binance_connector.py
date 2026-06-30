"""
=============================================================
  BINANCE API CONNECTOR
  Assets: BTC/USDT, ETH/USDT (Futures or Spot)
  Purpose: Live OHLCV data feed + order execution
=============================================================

SETUP STEPS:
  1. pip install python-binance pandas python-dotenv websocket-client
  2. Create Binance API key at: https://www.binance.com/en/my/settings/api-management
     ⚠️  Enable "Futures Trading" if using USDM Futures
     ⚠️  Whitelist your server IP for security
  3. Add keys to .env file (see template at bottom)

MODES:
  - SPOT: Buy/sell actual BTC or ETH
  - USDM FUTURES: Trade BTC/ETH with leverage (institutional standard)
  
For this bot, we default to USDM Futures (Binance Futures).
Set USE_FUTURES=False in .env for spot trading.

BINANCE API DOCS: https://binance-docs.github.io/apidocs/futures/en/
=============================================================
"""

import os
import time
import logging
import threading
from datetime import datetime, timezone
from typing import Optional, Callable
from dotenv import load_dotenv
import pandas as pd

try:
    from binance.client import Client
    from binance.streams import ThreadedWebsocketManager
    from binance.enums import *
    from binance.exceptions import BinanceAPIException
except ImportError:
    raise ImportError("Run: pip install python-binance")

load_dotenv()
log = logging.getLogger("BinanceConnector")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")


# ─────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────

TRADING_PAIRS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
}

# Binance Futures leverage (institutional standard: 1–3x, never 10x+)
DEFAULT_LEVERAGE = 1   # 1x = no leverage, same as spot exposure

# Kline (candle) interval map
INTERVAL_MAP = {
    "1m":  Client.KLINE_INTERVAL_1MINUTE,
    "3m":  Client.KLINE_INTERVAL_3MINUTE,
    "5m":  Client.KLINE_INTERVAL_5MINUTE,
    "15m": Client.KLINE_INTERVAL_15MINUTE,
    "30m": Client.KLINE_INTERVAL_30MINUTE,
    "1h":  Client.KLINE_INTERVAL_1HOUR,
    "4h":  Client.KLINE_INTERVAL_4HOUR,
    "1d":  Client.KLINE_INTERVAL_1DAY,
}


# ─────────────────────────────────────────────
# AUTH MANAGER
# ─────────────────────────────────────────────

class BinanceAuthManager:
    """
    Handles Binance API authentication.
    Unlike Zerodha, Binance keys don't expire — but rotate them monthly.
    
    Security rules:
    - NEVER hardcode API keys in code
    - Restrict API key to specific IP addresses in Binance settings
    - Use read-only keys for data, separate keys for trading
    """

    def __init__(self):
        self.api_key    = os.getenv("BINANCE_API_KEY")
        self.api_secret = os.getenv("BINANCE_API_SECRET")
        self.testnet    = os.getenv("BINANCE_TESTNET", "true").lower() == "true"

        if not self.api_key or not self.api_secret:
            raise EnvironmentError(
                "Missing BINANCE_API_KEY or BINANCE_API_SECRET in .env file"
            )

    def get_client(self) -> Client:
        """Returns authenticated Binance client."""
        if self.testnet:
            log.warning("🧪 TESTNET MODE — Using Binance Testnet (no real money)")
            client = Client(self.api_key, self.api_secret, testnet=True)
        else:
            log.warning("🔴 MAINNET MODE — Real Binance account connected")
            client = Client(self.api_key, self.api_secret)

        # Verify connectivity
        try:
            server_time = client.get_server_time()
            local_time  = int(time.time() * 1000)
            drift_ms    = abs(server_time["serverTime"] - local_time)
            if drift_ms > 1000:
                log.warning(f"⚠️  Clock drift: {drift_ms}ms — sync your system clock")
            else:
                log.info(f"✅ Binance connected | Server time drift: {drift_ms}ms")
        except Exception as e:
            raise ConnectionError(f"Binance connection failed: {e}")

        return client


# ─────────────────────────────────────────────
# DATA CONNECTOR
# ─────────────────────────────────────────────

class BinanceDataConnector:
    """
    Fetches historical and live OHLCV data for BTC/ETH.
    Binance is available 24/7 — no market hours restriction.
    """

    def __init__(self, client: Client, use_futures: bool = True):
        self.client      = client
        self.use_futures = use_futures
        self._twm        = None          # ThreadedWebsocketManager
        self._live_dfs   = {}            # symbol → pd.DataFrame

    def get_historical(
        self,
        symbol: str,        # "BTC" or "ETH"
        interval: str = "1h",
        limit: int = 500    # number of candles (max 1500 for Binance)
    ) -> pd.DataFrame:
        """
        Fetch historical OHLCV data.
        Returns DataFrame with: open, high, low, close, volume
        """
        pair     = TRADING_PAIRS.get(symbol, symbol)
        kline_iv = INTERVAL_MAP.get(interval, Client.KLINE_INTERVAL_1HOUR)

        log.info(f"📥 Fetching {pair} historical data | {interval} | {limit} candles")

        try:
            if self.use_futures:
                klines = self.client.futures_klines(
                    symbol=pair, interval=kline_iv, limit=limit
                )
            else:
                klines = self.client.get_klines(
                    symbol=pair, interval=kline_iv, limit=limit
                )
        except BinanceAPIException as e:
            log.error(f"❌ Binance API error: {e.status_code} — {e.message}")
            raise

        df = pd.DataFrame(klines, columns=[
            "timestamp", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_buy_base", "taker_buy_quote", "ignore"
        ])

        df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("datetime", inplace=True)
        df = df[["open", "high", "low", "close", "volume"]].astype(float)
        df = df.dropna()

        log.info(f"✅ {len(df)} candles loaded for {pair}")
        return df

    def get_ltp(self, symbol: str) -> float:
        """Get Last Traded Price for BTC or ETH."""
        pair = TRADING_PAIRS.get(symbol, symbol)
        try:
            if self.use_futures:
                ticker = self.client.futures_symbol_ticker(symbol=pair)
            else:
                ticker = self.client.get_symbol_ticker(symbol=pair)
            return float(ticker["price"])
        except BinanceAPIException as e:
            log.error(f"❌ LTP fetch failed: {e}")
            return 0.0

    def get_orderbook_depth(self, symbol: str, levels: int = 10) -> dict:
        """
        Fetch order book — used for order flow analysis.
        Returns bid/ask walls (institutional trading signal).
        """
        pair = TRADING_PAIRS.get(symbol, symbol)
        try:
            if self.use_futures:
                depth = self.client.futures_order_book(symbol=pair, limit=levels)
            else:
                depth = self.client.get_order_book(symbol=pair, limit=levels)

            bids = pd.DataFrame(depth["bids"], columns=["price", "qty"]).astype(float)
            asks = pd.DataFrame(depth["asks"], columns=["price", "qty"]).astype(float)

            bid_wall = bids.nlargest(3, "qty")
            ask_wall = asks.nlargest(3, "qty")

            return {
                "bid_wall_price": bid_wall["price"].values.tolist(),
                "ask_wall_price": ask_wall["price"].values.tolist(),
                "total_bid_qty":  bids["qty"].sum(),
                "total_ask_qty":  asks["qty"].sum(),
                "buy_pressure":   bids["qty"].sum() / (bids["qty"].sum() + asks["qty"].sum()),
            }
        except Exception as e:
            log.error(f"❌ Order book fetch failed: {e}")
            return {}

    def start_live_feed(
        self,
        symbols: list[str],
        interval: str = "1m",
        on_candle: Optional[Callable] = None
    ):
        """
        Start WebSocket stream for real-time candles.
        on_candle(symbol, df) called on every closed candle.
        
        Binance streams push updates every 2 seconds for the current candle.
        We only trigger on_candle when the candle is marked as closed.
        """
        self._twm = ThreadedWebsocketManager(
            api_key    = os.getenv("BINANCE_API_KEY"),
            api_secret = os.getenv("BINANCE_API_SECRET"),
            testnet    = os.getenv("BINANCE_TESTNET", "true").lower() == "true"
        )
        self._twm.start()
        kline_iv = INTERVAL_MAP.get(interval, Client.KLINE_INTERVAL_1HOUR)

        for sym in symbols:
            pair = TRADING_PAIRS.get(sym, sym).lower()

            def make_handler(symbol_key):
                def handler(msg):
                    if msg.get("e") == "error":
                        log.error(f"❌ Stream error: {msg['m']}")
                        return

                    k = msg.get("k", {})
                    is_closed = k.get("x", False)   # True only when candle is complete

                    if not is_closed:
                        return   # Skip in-progress candles

                    candle = {
                        "datetime": pd.to_datetime(k["t"], unit="ms", utc=True),
                        "open":     float(k["o"]),
                        "high":     float(k["h"]),
                        "low":      float(k["l"]),
                        "close":    float(k["c"]),
                        "volume":   float(k["v"]),
                    }
                    log.info(
                        f"🕯️  {symbol_key} candle | "
                        f"O:{candle['open']:.2f} H:{candle['high']:.2f} "
                        f"L:{candle['low']:.2f} C:{candle['close']:.2f}"
                    )

                    # Append to live DataFrame
                    new_row = pd.DataFrame([candle]).set_index("datetime")
                    if symbol_key not in self._live_dfs:
                        self._live_dfs[symbol_key] = new_row
                    else:
                        self._live_dfs[symbol_key] = pd.concat(
                            [self._live_dfs[symbol_key], new_row]
                        ).tail(500)   # Keep last 500 candles in memory

                    if on_candle:
                        on_candle(symbol_key, self._live_dfs[symbol_key].copy())

                return handler

            stream_name = f"{pair}@kline_{kline_iv}"
            if self.use_futures:
                self._twm.start_kline_futures_socket(
                    callback=make_handler(sym),
                    symbol=pair.upper(),
                    interval=kline_iv
                )
            else:
                self._twm.start_kline_socket(
                    callback=make_handler(sym),
                    symbol=pair.upper(),
                    interval=kline_iv
                )

            log.info(f"🚀 Live stream started: {sym} | {interval}")

    def stop_live_feed(self):
        if self._twm:
            self._twm.stop()
            log.info("🛑 Binance WebSocket streams stopped")

    def get_live_df(self, symbol: str) -> Optional[pd.DataFrame]:
        """Get the current accumulated live DataFrame for a symbol."""
        return self._live_dfs.get(symbol)


# ─────────────────────────────────────────────
# ORDER EXECUTOR
# ─────────────────────────────────────────────

class BinanceOrderExecutor:
    """
    Places, manages, and cancels orders on Binance Futures.
    
    Order types used:
    - LIMIT: Primary entry (better fills than market)
    - STOP_MARKET: Stop loss (guaranteed execution near SL price)
    - TAKE_PROFIT_MARKET: Take profit
    
    IMPORTANT: Each trade opens 3 orders:
    1. Entry (LIMIT)
    2. Stop Loss (STOP_MARKET)
    3. Take Profit (TAKE_PROFIT_MARKET)
    
    Always cancel SL/TP if entry is not filled.
    """

    MIN_NOTIONAL = {
        "BTCUSDT": 5.0,    # $5 minimum order
        "ETHUSDT": 5.0,
    }

    def __init__(self, client: Client, paper_trade: bool = True, use_futures: bool = True):
        self.client      = client
        self.paper_trade = paper_trade
        self.use_futures = use_futures
        self._orders     = []

        if paper_trade:
            log.warning("🧪 PAPER TRADE MODE — No real Binance orders will be placed")
        else:
            log.warning("🔴 LIVE MODE — Real Binance orders will be placed")

        # Set leverage for futures
        if not paper_trade and use_futures:
            for sym in ["BTCUSDT", "ETHUSDT"]:
                try:
                    client.futures_change_leverage(symbol=sym, leverage=DEFAULT_LEVERAGE)
                    client.futures_change_margin_type(symbol=sym, marginType="ISOLATED")
                    log.info(f"✅ {sym} leverage set to {DEFAULT_LEVERAGE}x (Isolated margin)")
                except Exception as e:
                    log.warning(f"Leverage/margin setup: {e}")

    def _compute_quantity(self, symbol: str, capital_usdt: float, price: float) -> float:
        """Compute order quantity in base asset (BTC or ETH)."""
        raw_qty = capital_usdt / price
        # Round to Binance precision requirements
        precision = {"BTCUSDT": 3, "ETHUSDT": 3}.get(symbol, 3)
        qty = round(raw_qty, precision)
        return max(qty, 0.001)   # Minimum quantity

    def place_order(
        self,
        symbol: str,              # "BTC" or "ETH"
        direction: str,           # "BUY" or "SELL"
        capital_usdt: float,
        limit_price: float,
        stop_loss: float,
        take_profit: float,
    ) -> dict:
        """
        Places a full trade bracket: Entry + SL + TP.
        Returns dict with all 3 order IDs.
        """
        pair = TRADING_PAIRS.get(symbol, symbol)
        qty  = self._compute_quantity(pair, capital_usdt, limit_price)

        log.info(
            f"📋 Order | {direction} {qty} {symbol} @ ${limit_price:,.2f} | "
            f"SL: ${stop_loss:,.2f} | TP: ${take_profit:,.2f} | "
            f"Capital: ${capital_usdt:,.2f}"
        )

        if self.paper_trade:
            order_group = {
                "entry_id": f"PAPER_ENTRY_{int(time.time())}",
                "sl_id":    f"PAPER_SL_{int(time.time())}",
                "tp_id":    f"PAPER_TP_{int(time.time())}",
                "symbol":   symbol,
                "direction": direction,
                "qty":      qty,
                "entry":    limit_price,
                "sl":       stop_loss,
                "tp":       take_profit,
                "status":   "OPEN",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            self._orders.append(order_group)
            log.info(f"📝 Paper order group placed: {order_group['entry_id']}")
            return order_group

        # ── Live orders ──
        try:
            opposite = "SELL" if direction == "BUY" else "BUY"

            # 1. Entry order (Limit)
            entry_order = self.client.futures_create_order(
                symbol        = pair,
                side          = direction,
                type          = ORDER_TYPE_LIMIT,
                timeInForce   = TIME_IN_FORCE_GTC,
                quantity      = qty,
                price         = limit_price,
                reduceOnly    = False,
            )

            # 2. Stop Loss (Stop Market — triggers at SL price)
            sl_order = self.client.futures_create_order(
                symbol        = pair,
                side          = opposite,
                type          = FUTURE_ORDER_TYPE_STOP_MARKET,
                stopPrice     = stop_loss,
                closePosition = True,
            )

            # 3. Take Profit (Take Profit Market)
            tp_order = self.client.futures_create_order(
                symbol        = pair,
                side          = opposite,
                type          = FUTURE_ORDER_TYPE_TAKE_PROFIT_MARKET,
                stopPrice     = take_profit,
                closePosition = True,
            )

            order_group = {
                "entry_id": entry_order["orderId"],
                "sl_id":    sl_order["orderId"],
                "tp_id":    tp_order["orderId"],
                "symbol":   symbol,
                "direction": direction,
                "qty":      qty,
                "status":   "OPEN",
            }
            self._orders.append(order_group)
            log.info(f"✅ Live order group placed | Entry: {order_group['entry_id']}")
            return order_group

        except BinanceAPIException as e:
            log.error(f"❌ Binance order failed: {e.status_code} — {e.message}")
            return {}

    def cancel_all_orders(self, symbol: str):
        """Emergency cancel all open orders for a symbol."""
        pair = TRADING_PAIRS.get(symbol, symbol)
        if self.paper_trade:
            for o in self._orders:
                if o["symbol"] == symbol:
                    o["status"] = "CANCELLED"
            log.info(f"📝 All paper orders for {symbol} cancelled")
            return
        try:
            self.client.futures_cancel_all_open_orders(symbol=pair)
            log.info(f"✅ All open orders cancelled for {pair}")
        except Exception as e:
            log.error(f"❌ Cancel all failed: {e}")

    def get_open_positions(self) -> pd.DataFrame:
        """Get all open futures positions."""
        if self.paper_trade:
            return pd.DataFrame([o for o in self._orders if o["status"] == "OPEN"])
        try:
            positions = self.client.futures_position_information()
            df = pd.DataFrame(positions)
            df["positionAmt"] = df["positionAmt"].astype(float)
            return df[df["positionAmt"] != 0]
        except Exception as e:
            log.error(f"❌ Positions fetch failed: {e}")
            return pd.DataFrame()

    def get_portfolio_value_usdt(self) -> float:
        """Get total USDT balance in futures wallet."""
        if self.paper_trade:
            return 10_000.0   # Placeholder $10k
        try:
            balance = self.client.futures_account_balance()
            for asset in balance:
                if asset["asset"] == "USDT":
                    return float(asset["balance"])
            return 0.0
        except Exception as e:
            log.error(f"❌ Balance fetch failed: {e}")
            return 0.0


# ─────────────────────────────────────────────
# MAIN BINANCE CONNECTOR (combines everything)
# ─────────────────────────────────────────────

class BinanceConnector:
    """
    Top-level connector — import this in your main bot.
    
    Usage:
        from binance_connector import BinanceConnector

        bc = BinanceConnector(paper_trade=True, use_futures=True)
        bc.connect()

        btc_df = bc.data.get_historical("BTC", interval="1h", limit=200)
        eth_df = bc.data.get_historical("ETH", interval="1h", limit=200)
        
        bc.data.start_live_feed(["BTC", "ETH"], interval="1h", on_candle=my_callback)
        
        order = bc.orders.place_order(
            symbol="BTC", direction="BUY",
            capital_usdt=1000,
            limit_price=btc_df['close'].iloc[-1],
            stop_loss=..., take_profit=...
        )
    """

    def __init__(self, paper_trade: bool = True, use_futures: bool = True):
        self.paper_trade = paper_trade
        self.use_futures = use_futures
        self.auth        = BinanceAuthManager()
        self._client     = None
        self.data        = None
        self.orders      = None

    def connect(self):
        """Connect to Binance and initialize data + order modules."""
        self._client = self.auth.get_client()
        self.data    = BinanceDataConnector(self._client, use_futures=self.use_futures)
        self.orders  = BinanceOrderExecutor(
            self._client,
            paper_trade=self.paper_trade,
            use_futures=self.use_futures
        )
        log.info(
            f"✅ BinanceConnector ready | "
            f"Mode: {'Futures' if self.use_futures else 'Spot'} | "
            f"{'Testnet' if self.auth.testnet else 'MAINNET'}"
        )

    def get_market_snapshot(self) -> dict:
        """Quick snapshot of BTC and ETH prices."""
        return {
            "BTC_USDT": self.data.get_ltp("BTC"),
            "ETH_USDT": self.data.get_ltp("ETH"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "market": "24/7 (crypto never sleeps)",
        }


# ─────────────────────────────────────────────
# .env FILE TEMPLATE
# ─────────────────────────────────────────────
"""
Save this as .env in your project folder:

BINANCE_API_KEY=your_api_key_here
BINANCE_API_SECRET=your_api_secret_here
BINANCE_TESTNET=true           # Set false for live trading
USE_FUTURES=true               # Set false for spot trading

Get testnet keys at: https://testnet.binancefuture.com/
Get live keys at:    https://www.binance.com/en/my/settings/api-management
"""

if __name__ == "__main__":
    print("\n" + "="*55)
    print("  BINANCE CONNECTOR — Connection Test")
    print("="*55)
    print("\n⚙️  Next steps:")
    print("   1. Add credentials to .env file")
    print("   2. Set BINANCE_TESTNET=true for safe testing")
    print("   3. bc = BinanceConnector(paper_trade=True)")
    print("   4. bc.connect()")
    print("   5. btc_df = bc.data.get_historical('BTC', '1h', 200)")
    print("   6. Feed btc_df + eth_df into StrategyOrchestrator")
    print("\n" + "="*55 + "\n")
