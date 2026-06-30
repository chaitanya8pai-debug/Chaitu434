
# ============================================================
#  FULL ASSET UNIVERSE — All markets your bot will scan
# ============================================================

ASSETS = {

    # ── COMMODITIES ─────────────────────────────────────────
    "Brent Crude":   {"ticker": "BZ=F",  "category": "Commodity", "icon": "🛢️"},
    "WTI Crude":     {"ticker": "CL=F",  "category": "Commodity", "icon": "🛢️"},
    "Natural Gas":   {"ticker": "NG=F",  "category": "Commodity", "icon": "🔥"},
    "Gold":          {"ticker": "GC=F",  "category": "Commodity", "icon": "🪙"},
    "Silver":        {"ticker": "SI=F",  "category": "Commodity", "icon": "🥈"},
    "Copper":        {"ticker": "HG=F",  "category": "Commodity", "icon": "🔶"},
    "Platinum":      {"ticker": "PL=F",  "category": "Commodity", "icon": "⬜"},

    # ── INDIAN INDICES ───────────────────────────────────────
    "Nifty 50":      {"ticker": "^NSEI",      "category": "Index", "icon": "📈"},
    "Sensex":        {"ticker": "^BSESN",     "category": "Index", "icon": "📊"},
    "Nifty Bank":    {"ticker": "^NSEBANK",   "category": "Index", "icon": "🏦"},
    "Nifty IT":      {"ticker": "^CNXIT",     "category": "Index", "icon": "💻"},

    # ── TOP INDIAN STOCKS ────────────────────────────────────
    "Reliance":      {"ticker": "RELIANCE.NS","category": "Stock",  "icon": "🏭"},
    "TCS":           {"ticker": "TCS.NS",     "category": "Stock",  "icon": "💻"},
    "HDFC Bank":     {"ticker": "HDFCBANK.NS","category": "Stock",  "icon": "🏦"},
    "Infosys":       {"ticker": "INFY.NS",    "category": "Stock",  "icon": "💻"},
    "ICICI Bank":    {"ticker": "ICICIBANK.NS","category":"Stock",  "icon": "🏦"},
    "Wipro":         {"ticker": "WIPRO.NS",   "category": "Stock",  "icon": "💻"},
    "Adani Ports":   {"ticker": "ADANIPORTS.NS","category":"Stock", "icon": "⚓"},
    "Bajaj Finance": {"ticker": "BAJFINANCE.NS","category":"Stock", "icon": "💰"},
    "L&T":           {"ticker": "LT.NS",      "category": "Stock",  "icon": "🏗️"},
    "ONGC":          {"ticker": "ONGC.NS",    "category": "Stock",  "icon": "🛢️"},

    # ── GLOBAL INDICES ───────────────────────────────────────
    "S&P 500":       {"ticker": "^GSPC",  "category": "Index", "icon": "🇺🇸"},
    "Nasdaq":        {"ticker": "^IXIC",  "category": "Index", "icon": "💹"},
    "Dow Jones":     {"ticker": "^DJI",   "category": "Index", "icon": "📊"},

    # ── CURRENCY PAIRS ───────────────────────────────────────
    "USD/INR":       {"ticker": "USDINR=X",  "category": "Forex", "icon": "💵"},
    "EUR/USD":       {"ticker": "EURUSD=X",  "category": "Forex", "icon": "💶"},
    "GBP/USD":       {"ticker": "GBPUSD=X",  "category": "Forex", "icon": "💷"},
    "USD/JPY":       {"ticker": "USDJPY=X",  "category": "Forex", "icon": "💴"},
    "EUR/INR":       {"ticker": "EURINR=X",  "category": "Forex", "icon": "💶"},
    "GBP/INR":       {"ticker": "GBPINR=X",  "category": "Forex", "icon": "💷"},

    # ── CRYPTO ──────────────────────────────────────────────
    "Bitcoin":       {"ticker": "BTC-USD",  "category": "Crypto", "icon": "₿"},
    "Ethereum":      {"ticker": "ETH-USD",  "category": "Crypto", "icon": "⟠"},
    "Solana":        {"ticker": "SOL-USD",  "category": "Crypto", "icon": "🟣"},
    "BNB":           {"ticker": "BNB-USD",  "category": "Crypto", "icon": "🔶"},
}
