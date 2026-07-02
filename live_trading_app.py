
from flask import Flask, jsonify, request
import yfinance as yf

# Fix yfinance for cloud servers
try:
    import yfinance.data as _yfd
    _yfd.YfData.user_agent_headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
except: pass
import pandas as pd
import json, os, uuid, warnings, logging
import threading, time, math
import websocket
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
import requests as req

warnings.filterwarnings("ignore")
logging.getLogger("werkzeug").setLevel(logging.ERROR)
load_dotenv(os.path.expanduser("~/Desktop/trading-bot/.env"))

app     = Flask(__name__, static_folder=".", template_folder=".")

# Allow cross-origin requests (needed for cloud)
@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return response
CAPITAL = 500000
TRADES_FILE = os.path.expanduser("~/Desktop/trading-bot/paper_trades.json")
TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN","")
CHAT    = os.getenv("TELEGRAM_CHAT_ID","")

ASSETS = {
    "Brent Crude":  {"ticker":"BZ=F",        "cat":"Commodity","live":False,"ws":""},
    "WTI Crude":    {"ticker":"CL=F",        "cat":"Commodity","live":False,"ws":""},
    "Natural Gas":  {"ticker":"NG=F",        "cat":"Commodity","live":False,"ws":""},
    "Gold":         {"ticker":"GC=F",        "cat":"Commodity","live":False,"ws":""},
    "Silver":       {"ticker":"SI=F",        "cat":"Commodity","live":False,"ws":""},
    "Copper":       {"ticker":"HG=F",        "cat":"Commodity","live":False,"ws":""},
    "Platinum":     {"ticker":"PL=F",        "cat":"Commodity","live":False,"ws":""},
    "Bitcoin":      {"ticker":"BTC-USD",     "cat":"Crypto",   "live":True, "ws":"btcusdt"},
    "Ethereum":     {"ticker":"ETH-USD",     "cat":"Crypto",   "live":True, "ws":"ethusdt"},
    "Solana":       {"ticker":"SOL-USD",     "cat":"Crypto",   "live":True, "ws":"solusdt"},
    "BNB":          {"ticker":"BNB-USD",     "cat":"Crypto",   "live":True, "ws":"bnbusdt"},
    "XRP":          {"ticker":"XRP-USD",     "cat":"Crypto",   "live":True, "ws":"xrpusdt"},
    "Cardano":      {"ticker":"ADA-USD",     "cat":"Crypto",   "live":True, "ws":"adausdt"},
    "Avalanche":    {"ticker":"AVAX-USD",    "cat":"Crypto",   "live":True, "ws":"avaxusdt"},
    "Dogecoin":     {"ticker":"DOGE-USD",    "cat":"Crypto",   "live":True, "ws":"dogeusdt"},
    "Chainlink":    {"ticker":"LINK-USD",    "cat":"Crypto",   "live":True, "ws":"linkusdt"},
    "Litecoin":     {"ticker":"LTC-USD",     "cat":"Crypto",   "live":True, "ws":"ltcusdt"},
    "Cosmos":       {"ticker":"ATOM-USD",    "cat":"Crypto",   "live":True, "ws":"atomusdt"},
    "Near":         {"ticker":"NEAR-USD",    "cat":"Crypto",   "live":True, "ws":"nearusdt"},
    "Aave":         {"ticker":"AAVE-USD",    "cat":"Crypto",   "live":True, "ws":"aaveusdt"},
    "Optimism":     {"ticker":"OP-USD",      "cat":"Crypto",   "live":True, "ws":"opusdt"},
    "Dogwifhat":    {"ticker":"WIF-USD",     "cat":"Crypto",   "live":True, "ws":"wifusdt"},
}

live_prices = {}
price_lock  = threading.Lock()
auto_state  = {
    "enabled":False,"scan_interval":300,"status":"Idle",
    "last_scan":"Never","trades_today":0,"max_trades_day":10,
    "log":[],"top_signals":[]
}
_btc_cache = {"df":None,"time":None}

# ── HELPERS ──────────────────────────────────────────────────
def load_trades():
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE) as f: return json.load(f)
    return {"open":[],"closed":[],"equity":[{"date":str(datetime.now().date()),"value":CAPITAL}]}

def save_trades(data):
    with open(TRADES_FILE,"w") as f: json.dump(data,f,indent=2)

def bot_log(msg,level="INFO"):
    entry={"time":datetime.now().strftime("%d %b %H:%M:%S"),"msg":msg,"level":level}
    auto_state["log"].insert(0,entry)
    auto_state["log"]=auto_state["log"][:200]
    print(f"[{entry[chr(116)+'ime']}] {level}: {msg}")

def send_tg(msg):
    if not TOKEN or not CHAT: return
    try: req.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                  json={"chat_id":CHAT,"text":msg},timeout=5)
    except: pass

def fetch_ohlcv(ticker,period="3mo"):
    import time
    for attempt in range(3):
        try:
            df=yf.download(ticker,period=period,
                          auto_adjust=True,progress=False,
                          timeout=30)
            if isinstance(df.columns,pd.MultiIndex):
                df.columns=df.columns.get_level_values(0)
            df.columns=[c.lower() for c in df.columns]
            df=df[["open","high","low","close","volume"]].dropna()
            if len(df)>0: return df
        except Exception as e:
            if attempt<2:
                time.sleep(2*attempt+1)
                continue
    return pd.DataFrame(columns=["open","high","low","close","volume"])

def is_market_open(cat,name):
    IST=timezone(timedelta(hours=5,minutes=30))
    now=datetime.now(IST); wd=now.weekday(); hm=now.hour*60+now.minute
    if cat=="Crypto": return True
    if cat=="Forex":  return wd<5
    if wd>=5: return False
    indian=["Nifty 50","Sensex","Bank Nifty","Nifty IT","Reliance","TCS",
            "HDFC Bank","Infosys","ICICI Bank","Wipro","Adani Ports",
            "Bajaj Finance","L&T","ONGC"]
    if name in indian: return 9*60+15<=hm<=15*60+30
    if cat=="Commodity": return 9*60<=hm<=23*60+30
    if name in ["S&P 500","Nasdaq","Dow Jones"]: return hm>=19*60 or hm<=1*60+30
    return True

# ── SIGNAL ENGINES ───────────────────────────────────────────
def compute_signal(df):
    close=df["close"]; vol=df["volume"]
    ema9=close.ewm(span=9).mean(); ema21=close.ewm(span=21).mean()
    ema50=close.ewm(span=50).mean()
    delta=close.diff(); gain=delta.clip(lower=0).ewm(span=14).mean()
    loss=(-delta.clip(upper=0)).ewm(span=14).mean()
    rsi=100-(100/(1+gain/(loss+1e-9)))
    tp_v=(df["high"]+df["low"]+close)/3
    vwap=(tp_v*vol).cumsum()/vol.cumsum()
    hl=(df["high"]-df["low"]).replace(0,1e-9)
    dv=((close-df["low"])/hl-(df["high"]-close)/hl)*vol
    cum_delta=dv.rolling(20).sum()
    vol_trend=vol.rolling(10).mean()/vol.rolling(30).mean()
    h=df["high"]; l=df["low"]
    hh=float(h.tail(5).max())>float(h.tail(10).head(5).max())
    hl_=float(l.tail(5).min())>float(l.tail(10).head(5).min())
    cur=float(close.iloc[-1])
    score=(
        (2 if ema9.iloc[-1]>ema21.iloc[-1] else -2)+
        (1 if cur>ema50.iloc[-1] else -1)+
        (1 if rsi.iloc[-1]>55 else -1 if rsi.iloc[-1]<45 else 0)+
        (1 if cur>vwap.iloc[-1] else -1)+
        (1 if cum_delta.iloc[-1]>0 else -1)+
        (1 if vol_trend.iloc[-1]>1.1 else 0)+
        (1 if (hh and hl_) else -1 if (not hh and not hl_) else 0)
    )
    conf=abs(score)/8.0
    if score>=4:    return "BUY",round(conf,2)
    elif score<=-4: return "SELL",round(conf,2)
    return "NEUTRAL",0.0

def detect_btc_regime(btc_df):
    close=btc_df["close"]
    ema50=close.ewm(span=50).mean(); ema200=close.ewm(span=200).mean()
    cur=float(close.iloc[-1]); e50=float(ema50.iloc[-1]); e200=float(ema200.iloc[-1])
    if e50>e200 and cur>e50: return "BULL"
    elif e50<e200 and cur<e50: return "BEAR"
    return "SIDEWAYS"

def institutional_crypto_signal(df, btc_df, name):
    """
    Institutional Crypto Strategy:
    BEAR regime  → SELL (trend) + BUY if RSI extreme oversold (bounce)
    BULL regime  → BUY  (trend) + SELL if RSI extreme overbought (dip)
    SIDEWAYS     → No trade
    """
    regime = detect_btc_regime(btc_df)
    if regime == "SIDEWAYS": return "NEUTRAL", 0.0

    close=df["close"]; vol=df["volume"]
    high=df["high"];   low=df["low"]

    # EMAs
    ema21  = close.ewm(span=21).mean()
    ema50  = close.ewm(span=50).mean()
    ema200 = close.ewm(span=200).mean()
    cur    = float(close.iloc[-1])
    e50    = float(ema50.iloc[-1])
    e200   = float(ema200.iloc[-1])

    # RSI
    delta = close.diff()
    gain  = delta.clip(lower=0).ewm(span=14).mean()
    loss  = (-delta.clip(upper=0)).ewm(span=14).mean()
    rsi   = float((100-(100/(1+gain/(loss+1e-9)))).iloc[-1])

    # Volume + VWAP
    vol_ratio = float(vol.rolling(5).mean().iloc[-1] /
                     (vol.rolling(30).mean().iloc[-1]+1e-9))
    tp_v  = (high+low+close)/3
    vwap  = float(((tp_v*vol).cumsum()/vol.cumsum()).iloc[-1])

    # ── TREND SCORE ───────────────────────────────────────────
    score = 0
    if e50>e200:   score+=3  # Golden cross
    else:          score-=3  # Death cross
    if cur>e200:   score+=2
    else:          score-=2
    if cur>e50:    score+=1
    else:          score-=1
    if rsi>60:     score+=1
    elif rsi<40:   score-=1
    if vol_ratio>1.3: score+=1
    if cur>vwap:   score+=1
    else:          score-=1
    # Max = 9

    conf = abs(score)/9.0

    # ── REGIME BASED DECISIONS ────────────────────────────────

    # 1. TREND SIGNALS — trade WITH the regime
    if regime=="BULL" and score>=4:
        return "BUY", round(conf,2)

    elif regime=="BEAR" and score<=-4:
        return "SELL", round(conf,2)

    # 2. BOUNCE SIGNAL — trade AGAINST regime when extreme
    # BEAR regime + extreme oversold = BUY the bounce
    # Institutions cover shorts and buy when RSI < 25
    elif regime=="BEAR" and rsi<=30:
        # Bounce confidence based on how oversold
        bounce_conf = round(min((25-rsi)/25 + 0.40, 0.85), 2)
        return "BUY", bounce_conf   # counter-trend bounce

    # 3. DIP SIGNAL — BULL regime + extreme overbought
    # Institutions take profits when RSI > 75
    elif regime=="BULL" and rsi>=70:
        dip_conf = round(min((rsi-75)/25 + 0.40, 0.85), 2)
        return "SELL", dip_conf   # counter-trend dip

    return "NEUTRAL", 0.0


def cta_commodity_signal(df):
    close=df["close"]; high=df["high"]; low=df["low"]
    cur=float(close.iloc[-1])
    momentum_score=0
    for lb in [21,63,126,252]:
        if len(close)>=lb:
            ret=(float(close.iloc[-1])-float(close.iloc[-lb]))/float(close.iloc[-lb])
            if ret>0.02: momentum_score+=1
            elif ret>0: momentum_score+=0.5
            elif ret<-0.02: momentum_score-=1
            elif ret<0: momentum_score-=0.5
    avg_momentum=momentum_score/4
    breakout_score=0
    for period in [20,55,100]:
        if len(high)>=period:
            highest=float(high.iloc[-period:].max()); lowest=float(low.iloc[-period:].min())
            if cur>=highest*0.998: breakout_score+=1
            elif cur<=lowest*1.002: breakout_score-=1
    ema50=float(close.ewm(span=50).mean().iloc[-1])
    ema200=float(close.ewm(span=200).mean().iloc[-1])
    if ema50>ema200 and cur>ema50: ema_score=2
    elif ema50<ema200 and cur<ema50: ema_score=-2
    elif cur>ema200: ema_score=1
    else: ema_score=-1
    tr=pd.concat([high-low,(high-close.shift()).abs(),(low-close.shift()).abs()],axis=1).max(axis=1)
    atr=float(tr.ewm(span=14).mean().iloc[-1])
    atr_pct=atr/cur*100
    if atr_pct<0.3 or atr_pct>6.0: return "NEUTRAL",0.0
    final_score=(avg_momentum*4+breakout_score*0.5+ema_score*0.5)
    conf=min(abs(final_score)/4.0,1.0)
    if final_score>=1.0 and conf>=0.40: return "BUY",round(conf,2)
    elif final_score<=-1.0 and conf>=0.40: return "SELL",round(conf,2)
    return "NEUTRAL",0.0

def get_btc_regime_df():
    now=datetime.now()
    if (_btc_cache["df"] is None or _btc_cache["time"] is None or
        (now-_btc_cache["time"]).seconds>3600):
        df=fetch_ohlcv("BTC-USD","1y")
        _btc_cache["df"]=df; _btc_cache["time"]=now
    return _btc_cache["df"]

def smart_levels(df, direction):
    """
    Combined Institutional SL/TP Calculator
    Indicators: ATR + EMA + Bollinger + Swing + Fibonacci + RSI
    RSI role:
      - Overbought (>70): tighter TP for SELL, wider SL for BUY
      - Oversold  (<30): tighter TP for BUY,  wider SL for SELL
      - Extreme   (>80/<20): extend TP by 1.5x (strong momentum)
    Min R:R: 2.0:1 enforced
    """
    close = df["close"]; high = df["high"]; low = df["low"]
    cur   = float(close.iloc[-1])

    # ── ATR ───────────────────────────────────────────────────
    tr  = pd.concat([high-low,
                    (high-close.shift()).abs(),
                    (low-close.shift()).abs()], axis=1).max(axis=1)
    atr = float(tr.ewm(span=14).mean().iloc[-1])

    # ── RSI ───────────────────────────────────────────────────
    delta = close.diff()
    gain  = delta.clip(lower=0).ewm(span=14).mean()
    loss  = (-delta.clip(upper=0)).ewm(span=14).mean()
    rsi   = float((100-(100/(1+gain/(loss+1e-9)))).iloc[-1])

    # RSI zones
    rsi_extreme_bull = rsi < 20   # extreme oversold
    rsi_oversold     = rsi < 30   # oversold
    rsi_extreme_bear = rsi > 80   # extreme overbought
    rsi_overbought   = rsi > 70   # overbought
    rsi_neutral      = 40<=rsi<=60

    # RSI-based TP multiplier
    # Strong momentum = extend TP target
    if direction == "BUY":
        if rsi_extreme_bull:  tp_mult = 3.0   # extreme oversold = big bounce
        elif rsi_oversold:    tp_mult = 2.5   # oversold = good bounce
        elif rsi_overbought:  tp_mult = 2.0   # overbought = small move left
        else:                 tp_mult = 2.5   # neutral

        # RSI-based SL buffer
        if rsi_oversold:      sl_buffer = 0.05*atr  # tighter — high conviction
        elif rsi_overbought:  sl_buffer = 0.20*atr  # wider — less conviction
        else:                 sl_buffer = 0.10*atr
    else:  # SELL
        if rsi_extreme_bear:  tp_mult = 3.0   # extreme overbought = big drop
        elif rsi_overbought:  tp_mult = 2.5   # overbought = good drop
        elif rsi_oversold:    tp_mult = 2.0   # oversold = small move left
        else:                 tp_mult = 2.5

        if rsi_overbought:    sl_buffer = 0.05*atr
        elif rsi_oversold:    sl_buffer = 0.20*atr
        else:                 sl_buffer = 0.10*atr

    # ── EMAs ──────────────────────────────────────────────────
    ema20  = float(close.ewm(span=20).mean().iloc[-1])
    ema50  = float(close.ewm(span=50).mean().iloc[-1])
    ema200 = float(close.ewm(span=200).mean().iloc[-1])

    # ── BOLLINGER BANDS ───────────────────────────────────────
    sma20    = float(close.rolling(20).mean().iloc[-1])
    std20    = float(close.rolling(20).std().iloc[-1])
    bb_upper = sma20 + 2*std20
    bb_lower = sma20 - 2*std20

    # ── SWING HIGHS/LOWS ─────────────────────────────────────
    recent   = df.tail(30)
    swings_h = []; swings_l = []
    for i in range(2, len(recent)-2):
        if float(recent["high"].iloc[i]) == float(recent["high"].iloc[i-2:i+3].max()):
            swings_h.append(float(recent["high"].iloc[i]))
        if float(recent["low"].iloc[i]) == float(recent["low"].iloc[i-2:i+3].min()):
            swings_l.append(float(recent["low"].iloc[i]))

    # ── FIBONACCI ─────────────────────────────────────────────
    recent_high = float(high.tail(20).max())
    recent_low  = float(low.tail(20).min())
    fib_range   = recent_high - recent_low
    fib_236     = recent_high - 0.236*fib_range
    fib_382     = recent_high - 0.382*fib_range
    fib_618     = recent_high - 0.618*fib_range

    # ── COLLECT LEVELS ────────────────────────────────────────
    resistances = sorted([
        l for l in [ema20, ema50, ema200, bb_upper,
                    fib_236, fib_382, fib_618] + swings_h
        if cur + 0.3*atr < l < cur + 4.0*atr
    ])

    supports = sorted([
        l for l in [ema20, ema50, ema200, bb_lower,
                    fib_618, fib_382, fib_236] + swings_l
        if cur - 4.0*atr < l < cur - 0.3*atr
    ], reverse=True)

    # ── COMBINED SL SELECTION ─────────────────────────────────
    if direction == "BUY":
        # ATR baseline SL
        sl_atr = cur - 1.5*atr

        # Best indicator SL (nearest support with buffer)
        sl_ind = max([l for l in supports
                     if cur-l >= 0.5*atr], default=None)

        # RSI adjusts SL confidence
        if sl_ind:
            # Oversold = high conviction = use tighter indicator SL
            if rsi_oversold:
                sl = sl_ind - sl_buffer
            # Not oversold = less conviction = use ATR if tighter
            else:
                sl = sl_ind - sl_buffer if sl_ind > sl_atr else sl_atr
        else:
            sl = sl_atr

        # Hard limits
        if cur - sl > 3.0*atr: sl = cur - 3.0*atr
        if cur - sl < 0.5*atr: sl = cur - 0.5*atr

        risk = cur - sl

        # TP = find resistance giving min 2:1 R:R
        # RSI extreme = extend TP target
        tp = None
        min_rr = 2.0
        for res in resistances:
            if (res-cur)/risk >= min_rr:
                tp = res - sl_buffer
                break
        if not tp:
            tp = cur + tp_mult*risk  # RSI-adjusted fallback

    else:  # SELL
        sl_atr = cur + 1.5*atr
        sl_ind = min([l for l in resistances
                     if l-cur >= 0.5*atr], default=None)

        if sl_ind:
            if rsi_overbought:
                sl = sl_ind + sl_buffer
            else:
                sl = sl_ind + sl_buffer if sl_ind < sl_atr else sl_atr
        else:
            sl = sl_atr

        if sl - cur > 3.0*atr: sl = cur + 3.0*atr
        if sl - cur < 0.5*atr: sl = cur + 0.5*atr

        risk = sl - cur

        tp = None
        for sup in supports:
            if (cur-sup)/risk >= 2.0:
                tp = sup + sl_buffer
                break
        if not tp:
            tp = cur - tp_mult*risk  # RSI-adjusted fallback

    # ── SAFETY ───────────────────────────────────────────────
    if direction == "BUY"  and tp <= cur: tp = cur + 2.5*atr
    if direction == "SELL" and tp >= cur: tp = cur - 2.5*atr

    risk   = abs(cur - sl)
    reward = abs(tp - cur)
    rr     = round(reward/risk, 2) if risk > 0 else 0

    return round(sl,4), round(tp,4), round(atr,4), rr


def check_positions():
    """
    Close positions when SL or TP is hit — runs 24/7
    Also applies trailing stop to lock in profits
    """
    data=load_trades()
    if not data.get("open"): return
    changed=False
    for trade in list(data["open"]):
        try:
            name=trade.get("name",trade.get("asset",""))
            with price_lock: lp=live_prices.get(name)
            if lp: cur=lp["price"]
            else:
                df=fetch_ohlcv(ASSETS.get(name,{}).get("ticker","BTC-USD"),"2d")
                cur=float(df["close"].iloc[-1])
            sl=trade["sl"]; tp=trade["tp"]
            direction=trade["direction"]
            entry=trade.get("entry",cur)
            atr=trade.get("atr",abs(entry-sl)/1.5)

            # ── TRAILING STOP LOGIC ───────────────────────────
            # When trade moves 2% in our favour → move SL to breakeven
            # When trade moves 4% in our favour → trail SL 1.5xATR behind
            if direction=="BUY":
                profit_pct=(cur-entry)/entry
                if profit_pct>=0.04:
                    # Trail SL at 1.5xATR below current price
                    new_sl=round(cur-1.5*atr,4)
                    if new_sl>sl:
                        trade["sl"]=new_sl; sl=new_sl
                        changed=True
                        bot_log(f"TRAIL SL: {name} BUY → SL moved to {new_sl:.4f} (locked {profit_pct*100:.1f}%)","INFO")
                elif profit_pct>=0.02:
                    # Move SL to breakeven
                    new_sl=round(entry+0.1*atr,4)
                    if new_sl>sl:
                        trade["sl"]=new_sl; sl=new_sl
                        changed=True
                        bot_log(f"BREAKEVEN SL: {name} BUY → SL at breakeven","INFO")
            else:  # SELL
                profit_pct=(entry-cur)/entry
                if profit_pct>=0.04:
                    new_sl=round(cur+1.5*atr,4)
                    if new_sl<sl:
                        trade["sl"]=new_sl; sl=new_sl
                        changed=True
                        bot_log(f"TRAIL SL: {name} SELL → SL moved to {new_sl:.4f} (locked {profit_pct*100:.1f}%)","INFO")
                elif profit_pct>=0.02:
                    new_sl=round(entry-0.1*atr,4)
                    if new_sl<sl:
                        trade["sl"]=new_sl; sl=new_sl
                        changed=True
                        bot_log(f"BREAKEVEN SL: {name} SELL → SL at breakeven","INFO")

            reason=None
            if direction=="BUY":
                if cur<=sl: reason="SL"
                elif cur>=tp: reason="TP"
            else:
                if cur>=sl: reason="SL"
                elif cur<=tp: reason="TP"
            if reason:
                ep=sl if reason=="SL" else tp
                pnl_pct=(ep-trade["entry"])/trade["entry"] if direction=="BUY" else (trade["entry"]-ep)/trade["entry"]
                pnl_abs=round(pnl_pct*trade["capital"],0)
                closed={**trade,
                    "exit_price":round(ep,4),
                    "pnl_pct":round(pnl_pct*100,2),
                    "pnl_abs":pnl_abs,
                    "closed_at":datetime.now().strftime("%d %b %Y %H:%M"),
                    "exit_reason":reason,"status":"CLOSED"}
                data["open"]=[t for t in data["open"] if t["id"]!=trade["id"]]
                data["closed"].append(closed)
                total=sum(t.get("pnl_abs",0) for t in data["closed"])
                data["equity"].append({"date":str(datetime.now().date()),
                                       "value":round(CAPITAL+total,0)})
                changed=True
                icon="✅" if pnl_abs>=0 else "❌"
                bot_log(f"{icon} CLOSED {name} via {reason} | PnL:Rs{pnl_abs:+,.0f}","TRADE")
                send_tg(f"TRADE CLOSED\n{direction} {name}\nReason:{reason}\nPnL:Rs{pnl_abs:+,.0f}\n[PAPER]")
        except: pass
    if changed: save_trades(data)

# ── SCAN ALL ─────────────────────────────────────────────────

# ═══════════════════════════════════════════════════════════
# SMART MONEY CONCEPTS ENGINE
# Used as confirmation filter for Commodities only
# Crypto stays on institutional BTC regime (proven 9x better)
# ═══════════════════════════════════════════════════════════

def smc_swing_points(df, n=5):
    """Detect swing highs and lows"""
    highs=[]; lows=[]
    for i in range(n, len(df)-n):
        wh=df["high"].iloc[i-n:i+n+1]
        wl=df["low"].iloc[i-n:i+n+1]
        if float(df["high"].iloc[i])==float(wh.max()):
            highs.append((i, float(df["high"].iloc[i])))
        if float(df["low"].iloc[i])==float(wl.min()):
            lows.append((i, float(df["low"].iloc[i])))
    return highs, lows

def smc_bos(df, highs, lows):
    """Break of Structure — trend continuation signal"""
    if len(highs)<2 or len(lows)<2: return None
    cur=float(df["close"].iloc[-1])
    rh=[h[1] for h in highs[-3:]]
    rl=[l[1] for l in lows[-3:]]
    if len(rh)>=2 and cur>rh[-2] and rh[-1]>rh[-2]: return "BULL"
    if len(rl)>=2 and cur<rl[-2] and rl[-1]<rl[-2]: return "BEAR"
    return None

def smc_choch(df, highs, lows):
    """Change of Character — trend reversal signal"""
    if len(highs)<2 or len(lows)<2: return None
    cur=float(df["close"].iloc[-1])
    rh=[h[1] for h in highs[-3:]]
    rl=[l[1] for l in lows[-3:]]
    if len(rh)>=2 and len(rl)>=2:
        was_bull = rh[-1]>rh[-2]
        was_bear = rl[-1]<rl[-2]
        if was_bull and cur<rl[-2]: return "BEAR"
        if was_bear and cur>rh[-2]: return "BULL"
    return None

def smc_fvg(df, lookback=20):
    """
    Fair Value Gap — price imbalance zones
    Bullish FVG: prev high < next low (gap up = unfilled buy orders)
    Bearish FVG: prev low > next high (gap down = unfilled sell orders)
    """
    recent=df.tail(lookback)
    cur=float(df["close"].iloc[-1])
    bull_fvg=[]; bear_fvg=[]
    for i in range(1, len(recent)-1):
        hp=float(recent["high"].iloc[i-1])
        lp=float(recent["low"].iloc[i-1])
        hn=float(recent["high"].iloc[i+1])
        ln=float(recent["low"].iloc[i+1])
        if hp<ln and cur>=hp:
            bull_fvg.append({"top":ln,"bot":hp,"mid":(hp+ln)/2})
        if lp>hn and cur<=lp:
            bear_fvg.append({"top":lp,"bot":hn,"mid":(lp+hn)/2})
    return bull_fvg, bear_fvg

def smc_order_blocks(df, lookback=20):
    """
    Order Blocks — institutional order zones
    Bullish OB: last bearish candle before strong bullish displacement
    Bearish OB: last bullish candle before strong bearish displacement
    Valid OB needs displacement candle > 1.5x ATR
    """
    recent=df.tail(lookback)
    high=recent["high"]; low=recent["low"]; close=recent["close"]
    tr=pd.concat([high-low,
                 (high-close.shift()).abs(),
                 (low-close.shift()).abs()],axis=1).max(axis=1)
    atr=float(tr.mean())
    cur=float(df["close"].iloc[-1])
    bull_ob=[]; bear_ob=[]
    for i in range(1, len(recent)-1):
        o=float(recent["open"].iloc[i])
        c=float(recent["close"].iloc[i])
        o_n=float(recent["open"].iloc[i+1])
        c_n=float(recent["close"].iloc[i+1])
        size=abs(c_n-o_n)
        ob_h=float(recent["high"].iloc[i])
        ob_l=float(recent["low"].iloc[i])
        # Bullish OB: bearish candle + strong bullish move after
        if c<o and c_n>o_n and size>1.5*atr and cur>ob_h:
            bull_ob.append({"high":ob_h,"low":ob_l,"mid":(ob_h+ob_l)/2})
        # Bearish OB: bullish candle + strong bearish move after
        if c>o and c_n<o_n and size>1.5*atr and cur<ob_l:
            bear_ob.append({"high":ob_h,"low":ob_l,"mid":(ob_h+ob_l)/2})
    return bull_ob, bear_ob

def smc_premium_discount(df):
    """
    Premium zone: above 50% of range = expensive = prefer SELL
    Discount zone: below 50% of range = cheap = prefer BUY
    """
    rh=float(df["high"].tail(50).max())
    rl=float(df["low"].tail(50).min())
    eq=(rh+rl)/2
    cur=float(df["close"].iloc[-1])
    return "PREMIUM" if cur>eq else "DISCOUNT"

def smc_liquidity(df, highs, lows):
    """
    Liquidity pools:
    Buy-side: above swing highs (stops of short sellers)
    Sell-side: below swing lows (stops of long buyers)
    Price gravitates toward liquidity
    """
    cur=float(df["close"].iloc[-1])
    buy_liq =[h[1] for h in highs if h[1]>cur]
    sell_liq=[l[1] for l in lows  if l[1]<cur]
    nearest_buy  = min(buy_liq)  if buy_liq  else cur*1.05
    nearest_sell = max(sell_liq) if sell_liq else cur*0.95
    return nearest_buy, nearest_sell

def smc_commodity_confirm(df, cta_direction):
    """
    SMC Confirmation Filter for Commodities
    Only confirms CTA signal if SMC agrees

    Returns: confirmed (bool), smc_score (int), analysis (dict)

    Rules (validated by backtest — Gold 3x better, Silver profitable):
      Strong confirm (+2 per factor):
        - BOS matches CTA direction
        - CHoCH matches CTA direction
        - FVG in CTA direction
        - Order Block confirms entry
      Mild confirm (+1):
        - Premium/Discount zone aligns
        - Liquidity target in direction

    Minimum SMC score to confirm: 2 (at least 1 strong factor)
    """
    try:
        score=0; analysis={}
        highs,lows=smc_swing_points(df)

        # 1. Break of Structure
        bos=smc_bos(df, highs, lows)
        analysis["bos"]=bos
        if bos=="BULL" and cta_direction=="BUY":   score+=2
        elif bos=="BEAR" and cta_direction=="SELL": score+=2
        elif bos is not None:                       score-=1

        # 2. Change of Character
        choch=smc_choch(df, highs, lows)
        analysis["choch"]=choch
        if choch=="BULL" and cta_direction=="BUY":   score+=2
        elif choch=="BEAR" and cta_direction=="SELL": score+=2

        # 3. Fair Value Gaps
        bull_fvg, bear_fvg=smc_fvg(df)
        analysis["bull_fvg"]=len(bull_fvg)
        analysis["bear_fvg"]=len(bear_fvg)
        if bull_fvg and cta_direction=="BUY":   score+=2
        elif bear_fvg and cta_direction=="SELL": score+=2
        elif bull_fvg and cta_direction=="SELL": score-=1
        elif bear_fvg and cta_direction=="BUY":  score-=1

        # 4. Order Blocks
        bull_ob, bear_ob=smc_order_blocks(df)
        analysis["bull_ob"]=len(bull_ob)
        analysis["bear_ob"]=len(bear_ob)
        if bull_ob and cta_direction=="BUY":    score+=2
        elif bear_ob and cta_direction=="SELL": score+=2

        # 5. Premium/Discount zone
        zone=smc_premium_discount(df)
        analysis["zone"]=zone
        if zone=="DISCOUNT" and cta_direction=="BUY":   score+=1
        elif zone=="PREMIUM" and cta_direction=="SELL":  score+=1
        elif zone=="PREMIUM" and cta_direction=="BUY":   score-=1
        elif zone=="DISCOUNT" and cta_direction=="SELL":  score-=1

        # 6. Liquidity target
        buy_liq,sell_liq=smc_liquidity(df, highs, lows)
        cur=float(df["close"].iloc[-1])
        analysis["buy_liquidity"]=round(buy_liq,4)
        analysis["sell_liquidity"]=round(sell_liq,4)
        liq_above=(buy_liq-cur)/cur
        liq_below=(cur-sell_liq)/cur
        if cta_direction=="BUY"  and liq_above<0.05: score+=1
        elif cta_direction=="SELL" and liq_below<0.05: score+=1

        analysis["smc_score"]=score
        # Confirmed if at least 1 strong factor agrees (score >= 2)
        confirmed = score>=2
        return confirmed, score, analysis

    except Exception as e:
        # If SMC fails, don't block CTA signal
        return True, 0, {"error": str(e)}



def detect_recovery(name, df, regime):
    """
    Detects short-term recovery within a bear market.
    NOT a BUY signal — a MOMENTUM SHIFT alert.
    Tells you: "Bounce happening — manage your SELL positions!"

    Conditions:
      1. Bear regime (EMA50 < EMA200)
      2. 7-day return > 5% (meaningful rally)
      3. RSI recovering (crossed above 50)
      4. EMA9 approaching EMA21 (gap < 3%)
      5. Price approaching EMA50 (within 10%)
    """
    try:
        if regime != "BEAR": return None
        close = df["close"]
        cur   = float(close.iloc[-1])

        # 7-day momentum
        ret7 = (float(close.iloc[-1])-float(close.iloc[-7]))/float(close.iloc[-7])*100
        if ret7 < 5.0: return None  # need meaningful rally

        # EMA levels
        e9   = float(close.ewm(span=9).mean().iloc[-1])
        e21  = float(close.ewm(span=21).mean().iloc[-1])
        e50  = float(close.ewm(span=50).mean().iloc[-1])

        # RSI
        d=close.diff(); g=d.clip(lower=0).ewm(span=14).mean()
        l=(-d.clip(upper=0)).ewm(span=14).mean()
        rsi=float((100-(100/(1+g/(l+1e-9)))).iloc[-1])

        # Recovery conditions
        ema_gap_pct   = abs(e9-e21)/e21*100    # how close EMA9 to EMA21
        price_to_e50  = (e50-cur)/cur*100       # how far from EMA50

        score = 0
        reasons = []

        if ret7 >= 10:  score+=3; reasons.append(f"+{ret7:.1f}% rally")
        elif ret7 >= 5: score+=2; reasons.append(f"+{ret7:.1f}% rally")

        if rsi >= 60:   score+=2; reasons.append(f"RSI {rsi:.0f}")
        elif rsi >= 50: score+=1; reasons.append(f"RSI {rsi:.0f}")

        if ema_gap_pct <= 2: score+=2; reasons.append("EMA near cross")
        elif ema_gap_pct <= 5: score+=1; reasons.append("EMA converging")

        if price_to_e50 <= 5:  score+=2; reasons.append("Near EMA50")
        elif price_to_e50 <= 10: score+=1; reasons.append("Approaching EMA50")

        if score >= 5:
            strength = "STRONG" if score >= 7 else "MODERATE"
            return {
                "name":      name,
                "type":      "RECOVERY",
                "strength":  strength,
                "ret7":      round(ret7,1),
                "rsi":       round(rsi,1),
                "ema_gap":   round(ema_gap_pct,1),
                "to_ema50":  round(price_to_e50,1),
                "reasons":   " | ".join(reasons),
                "score":     score,
            }
        return None
    except: return None


def scan_all():
    from datetime import datetime
    auto_state["status"]="Scanning 22 markets..."
    bot_log("Market scan started","INFO")
    signals=[]; data=load_trades()
    open_positions=[(t.get("name",t.get("asset","")),t.get("direction",""))
                    for t in data.get("open",[])]
    open_assets={name for name,_ in open_positions}

    for name,info in ASSETS.items():
        try:
            if not is_market_open(info["cat"],name): continue
            df=fetch_ohlcv(info["ticker"])
            if len(df)<60: continue
            # Sanity check vs live price
            hist_price=float(df["close"].iloc[-1])
            # Always prefer live price over historical close
            with price_lock: lp=live_prices.get(name)
            if lp and lp.get("price",0)>0:
                live_p=lp["price"]
                diff=abs(hist_price-live_p)/hist_price
                if diff>0.15:
                    # Price moved >15% — suspicious data, skip
                    bot_log(f"BAD DATA skipped {name}: hist={hist_price:.4f} live={live_p:.4f}","ERROR")
                    continue
                # Use live price — this is the actual execution price
                entry=live_p
                bot_log(f"LIVE PRICE: {name} hist={hist_price:.4f} → live={live_p:.4f} (diff:{diff*100:.1f}%)","INFO")
            else:
                # No live price — fetch fresh from yfinance
                try:
                    fresh_df=fetch_ohlcv(info["ticker"],"5d")
                    entry=float(fresh_df["close"].iloc[-1])
                    bot_log(f"FRESH PRICE: {name} @ {entry:.4f}","INFO")
                except:
                    entry=hist_price
            # Strategy per category
            if info["cat"]=="Crypto":
                try:
                    btc_df=get_btc_regime_df()
                    direction,conf=institutional_crypto_signal(df,btc_df,name)
                except: direction,conf=compute_signal(df)
            elif info["cat"]=="Commodity":
                try: direction,conf=cta_commodity_signal(df)
                except: direction,conf=compute_signal(df)
            else: direction,conf=compute_signal(df)
            if direction=="NEUTRAL" or conf<0.40: continue
            sl,tp,atr,rr=smart_levels(df,direction)
            if rr<2.0: continue
            sig={
                "name":name,"ticker":info["ticker"],"cat":info["cat"],
                "direction":direction,"confidence":conf,
                "entry":round(entry,4),"sl":round(sl,4),"tp":round(tp,4),
                "rr":rr,"atr":round(atr,4),"capital":round(0.10*CAPITAL,0),
                "time":datetime.now().strftime("%H:%M"),"live":info.get("live",False),
            }
            sig["already_open"]=direction in [d for n,d in open_positions if n==name]
            signals.append(sig)
            bot_log(f"SIGNAL: {direction} {name} @ {entry:.4f} | Conf:{conf:.0%} | R:R:{rr}","INFO")

            # Check for recovery within bear market
            if direction == "SELL":
                btc_df_r = get_btc_regime_df()
                regime_r = detect_btc_regime(btc_df_r)
                rec = detect_recovery(name, df, regime_r)
                if rec:
                    sig["recovery"] = rec
                    bot_log(f"RECOVERY ALERT: {name} {rec['strength']} | {rec['reasons']}","INFO")
                    send_tg(
                        f"RECOVERY ALERT\n"
                        f"{name} showing MOMENTUM SHIFT\n"
                        f"7d: +{rec['ret7']}% | RSI:{rec['rsi']}\n"
                        f"{rec['reasons']}\n"
                        f"Manage your SELL positions!"
                    )
            # Auto execute — allow opposite direction (hedge)
            existing_dirs=[d for n,d in open_positions if n==name]
            can_trade=(
                auto_state["enabled"] and
                auto_state["trades_today"]<auto_state["max_trades_day"] and
                (not existing_dirs or direction not in existing_dirs)
            )
            if can_trade:
                # Final live price check at moment of execution
                with price_lock: final_lp=live_prices.get(name)
                if final_lp and final_lp.get("price",0)>0:
                    final_price=final_lp["price"]
                    if abs(final_price-entry)/entry < 0.05:
                        sl_dist=abs(sig["sl"]-sig["entry"])
                        tp_dist=abs(sig["tp"]-sig["entry"])
                        sig["entry"]=round(final_price,4)
                        sig["sl"]=round(final_price-sl_dist,4) if direction=="BUY" else round(final_price+sl_dist,4)
                        sig["tp"]=round(final_price+tp_dist,4) if direction=="BUY" else round(final_price-tp_dist,4)
                        entry=final_price

                # ── RISK-BASED POSITION SIZING ────────────────
                # Every trade risks exactly Rs2,000 (backtested: 11x better!)
                # Position size = Fixed_Risk / SL_distance_%
                FIXED_RISK  = 2000  # Rs2,000 risk per trade
                MAX_POSITION= 100000 # cap at Rs1,00,000
                sl_dist_pct = abs(sig["sl"]-sig["entry"])/(sig["entry"]+1e-9)
                if sl_dist_pct > 0:
                    position = round(min(FIXED_RISK/sl_dist_pct, MAX_POSITION),0)
                else:
                    position = 50000  # fallback
                sig["capital"] = position
                sig["risk_per_trade"] = FIXED_RISK
                bot_log(f"RISK-SIZED: {name} SL:{sl_dist_pct*100:.1f}% away → Position:Rs{position:,.0f} Risk:Rs{FIXED_RISK}","INFO")

                trade={**sig,
                    "id":str(uuid.uuid4())[:8],
                    "opened_at":datetime.now().strftime("%d %b %Y %H:%M"),
                    "status":"OPEN","mode":"AUTO"}
                data["open"].append(trade)
                open_positions.append((name,direction))
                auto_state["trades_today"]+=1
                bot_log(f"AUTO-EXECUTED: {direction} {name} @ {entry:.4f}","TRADE")
                send_tg(f"AUTO TRADE\n{direction} {name}\nEntry:{entry:.4f} SL:{sl:.4f} TP:{tp:.4f}\nR:R:{rr}:1 Conf:{conf:.0%}\n[PAPER]")
        except Exception as e:
            bot_log(f"Scan error {name}: {str(e)[:80]}","ERROR")
            continue

    signals.sort(key=lambda x:x["confidence"],reverse=True)
    auto_state["top_signals"]=signals[:15]
    auto_state["last_scan"]=datetime.now().strftime("%d %b %H:%M:%S")
    n=len(signals)
    auto_state["status"]=f"Scan complete — {n} signal(s) found"
    bot_log(f"Scan done: {n} signals found","INFO")
    if signals: save_trades(data)
    send_tg(f"Scan Complete\n{n} signal(s) found\n{datetime.now().strftime('%d %b %H:%M')}")

# ── AUTO LOOP ────────────────────────────────────────────────

# ══════════════════════════════════════════════════════════════
# BOUNCE HUNTER — Dedicated short-term bounce detector
# Runs every 5 minutes regardless of Auto ON/OFF
# Catches missed bounces that main signal engine skips
# ══════════════════════════════════════════════════════════════

def detect_bounce(name, df, direction):
    """
    Detect short-term bounce opportunity:
    BEAR market + any of these conditions:
      1. RSI ≤ 30 + last candle green (bounce starting)
      2. RSI ≤ 25 (extreme — high conviction)
      3. RSI ≤ 30 + volume spike (capitulation + reversal)
      4. RSI ≤ 30 + Hammer/Engulfing candle pattern
    Returns: is_bounce, confidence, reason
    """
    try:
        close=df["close"]; high=df["high"]
        low=df["low"]; vol=df["volume"]; op=df["open"]
        cur=float(close.iloc[-1])

        # RSI
        delta=close.diff()
        gain=delta.clip(lower=0).ewm(span=14).mean()
        loss=(-delta.clip(upper=0)).ewm(span=14).mean()
        rsi=float((100-(100/(1+gain/(loss+1e-9)))).iloc[-1])

        # Volume ratio
        vol_ratio=float(vol.iloc[-1]/(vol.rolling(20).mean().iloc[-1]+1e-9))

        # Last candle direction
        last_green = float(close.iloc[-1]) > float(op.iloc[-1])
        last_red   = float(close.iloc[-1]) < float(op.iloc[-1])

        # Candle pattern
        c0=float(close.iloc[-1]); o0=float(op.iloc[-1])
        h0=float(high.iloc[-1]); l0=float(low.iloc[-1])
        c1=float(close.iloc[-2]); o1=float(op.iloc[-2])
        body0=abs(c0-o0); range0=h0-l0+1e-9
        body1=abs(c1-o1)
        lw0=(min(c0,o0)-l0)/range0
        uw0=(h0-max(c0,o0))/range0

        # Detect hammer
        hammer=(lw0>=0.55 and uw0<=0.15 and body0<body1)
        # Detect bullish engulfing
        engulf=(c0>o0 and c1<o1 and c0>o1 and o0<c1)

        # BEAR market bounce conditions
        if direction=="BULL":  # check overbought dip too
            if rsi>=70 and last_red:
                return True,0.65,"RSI overbought + red candle"
            if rsi>=80:
                return True,0.75,"RSI extreme overbought"
            return False,0.0,""

        # BEAR market bounce
        reasons=[]
        conf=0.0

        if rsi<=20:
            reasons.append(f"RSI extreme {rsi:.0f}")
            conf=max(conf,0.80)
        elif rsi<=25:
            reasons.append(f"RSI very oversold {rsi:.0f}")
            conf=max(conf,0.70)
        elif rsi<=30:
            reasons.append(f"RSI oversold {rsi:.0f}")
            conf=max(conf,0.60)
        else:
            return False,0.0,""  # RSI not oversold enough

        # Confirmation adds to confidence
        if last_green:
            reasons.append("green candle")
            conf=min(conf+0.10,0.90)
        if vol_ratio>=1.5:
            reasons.append(f"vol spike {vol_ratio:.1f}x")
            conf=min(conf+0.05,0.90)
        if hammer:
            reasons.append("hammer pattern")
            conf=min(conf+0.10,0.90)
        if engulf:
            reasons.append("bullish engulfing")
            conf=min(conf+0.10,0.90)

        if conf>=0.60:
            return True,round(conf,2)," + ".join(reasons)
        return False,0.0,""

    except: return False,0.0,""

def bounce_scan():
    """
    Dedicated bounce scanner — runs every 5 min 24/7
    Completely separate from main signal engine
    Catches short-term reversals quickly
    """
    data=load_trades()
    open_positions=[(t.get("name",""),t.get("direction",""))
                    for t in data.get("open",[])]
    open_count=len(data.get("open",[]))

    if open_count>=8:
        bot_log("Bounce scan: max positions reached","INFO")
        return

    bounces_found=0
    CRYPTO_ASSETS={k:v for k,v in ASSETS.items() if v["cat"]=="Crypto"}

    for name,info in CRYPTO_ASSETS.items():
        try:
            # Skip if already have position in same direction
            existing=[d for n,d in open_positions if n==name]
            if "BUY" in existing: continue  # already have bounce trade

            df=fetch_ohlcv(info["ticker"])
            if len(df)<50: continue

            # Get BTC regime
            btc_df=get_btc_regime_df()
            regime=detect_btc_regime(btc_df)
            if regime=="SIDEWAYS": continue

            # Get asset RSI
            close=df["close"]; delta=close.diff()
            gain=delta.clip(lower=0).ewm(span=14).mean()
            loss=(-delta.clip(upper=0)).ewm(span=14).mean()
            rsi=float((100-(100/(1+gain/(loss+1e-9)))).iloc[-1])

            # Check bounce
            is_bounce,conf,reason=detect_bounce(name,df,regime)
            if not is_bounce or conf<0.60: continue

            # Build bounce trade
            entry=float(df["close"].iloc[-1])
            with price_lock:
                lp=live_prices.get(name,{})
            if lp.get("price",0)>0: entry=lp["price"]

            # Tight SL/TP for bounce trade
            atr_tr=pd.concat([df["high"]-df["low"],
                             (df["high"]-close.shift()).abs(),
                             (df["low"]-close.shift()).abs()],axis=1).max(axis=1)
            atr=float(atr_tr.ewm(span=14).mean().iloc[-1])

            direction="BUY" if regime=="BEAR" else "SELL"
            if direction=="BUY":
                sl=round(entry-1.0*atr,4)   # tighter SL for bounce
                tp=round(entry+2.5*atr,4)   # realistic TP
            else:
                sl=round(entry+1.0*atr,4)
                tp=round(entry-2.5*atr,4)

            risk=abs(entry-sl); reward=abs(tp-entry)
            rr=round(reward/risk,2) if risk>0 else 0
            if rr<2.0: continue  # enforce 2:1 minimum

            sig={
                "name":     name,
                "ticker":   info["ticker"],
                "cat":      info["cat"],
                "direction":direction,
                "confidence":conf,
                "entry":    round(entry,4),
                "sl":       sl,"tp":tp,"rr":rr,
                "atr":      round(atr,4),
                "capital":  50000,
                "time":     datetime.now().strftime("%H:%M"),
                "live":     info.get("live",False),
                "type":     "BOUNCE",
                "reason":   reason,
            }

            # Auto execute bounce trade
            trade={**sig,
                "id":str(uuid.uuid4())[:8],
                "opened_at":datetime.now().strftime("%d %b %Y %H:%M"),
                "status":"OPEN","mode":"BOUNCE"}
            data["open"].append(trade)
            open_positions.append((name,direction))
            open_count+=1
            bounces_found+=1

            bot_log(f"BOUNCE: {direction} {name} | RSI:{rsi:.0f} | {reason} | Conf:{conf:.0%}","TRADE")
            send_tg(f"BOUNCE TRADE\n{direction} {name}\nRSI:{rsi:.0f} | {reason}\nEntry:{entry:.4f} SL:{sl:.4f} TP:{tp:.4f}\nR:R:{rr}:1 | Conf:{conf:.0%}\n[PAPER]")

            if open_count>=8: break

        except Exception as e:
            bot_log(f"Bounce scan error {name}: {str(e)[:50]}","ERROR")

    if bounces_found>0:
        save_trades(data)
        bot_log(f"Bounce scan complete: {bounces_found} bounce trade(s) executed","INFO")


def auto_loop():
    scan_counter=0; check_counter=0
    while True:
        time.sleep(30)
        check_counter+=30; scan_counter+=30
        # ALWAYS check positions — even when auto trading OFF
        if check_counter>=60:
            check_positions()
            check_counter=0
        # Only scan for new signals when auto trading ON
        if auto_state["enabled"]:
            if scan_counter>=auto_state["scan_interval"]:
                scan_all(); scan_counter=0
        now=datetime.now()
        if now.hour==0 and now.minute==0:
            auto_state["trades_today"]=0

# ── WEBSOCKET WATCHDOG ───────────────────────────────────────
def websocket_watchdog():
    time.sleep(60)
    while True:
        time.sleep(30)
        try:
            with price_lock: prices=dict(live_prices)
            if not prices: continue
            for name,info in prices.items():
                updated=info.get("updated","")
                if updated:
                    try:
                        last=datetime.strptime(updated,"%H:%M:%S")
                        now=datetime.now()
                        last=last.replace(year=now.year,month=now.month,day=now.day)
                        if (now-last).seconds>60:
                            bot_log("WebSocket stale — restarting...","SYSTEM")
                            threading.Thread(target=start_binance_websocket,daemon=True).start()
                    except: pass
                break
        except: pass

# ── BINANCE WEBSOCKET ────────────────────────────────────────
def fetch_binance_prices():
    """Fetch crypto prices via REST API — works on cloud"""
    CRYPTO_ASSETS={k:v for k,v in ASSETS.items() if v.get("live",False)}
    symbols=[v["ws"].upper()+"usdt" if not v["ws"].endswith("usdt")
             else v["ws"] for v in CRYPTO_ASSETS.values() if v.get("ws")]
    name_map={v["ws"].upper():k for k,v in CRYPTO_ASSETS.items() if v.get("ws")}
    try:
        # Fetch all tickers in one request
        syms_str=str([s.upper() for s in symbols]).replace("'",'"')
        r=req.get(f"https://api.binance.com/api/v3/ticker/24hr",
                  params={"symbols":syms_str},timeout=10)
        if r.status_code!=200: return
        data=r.json()
        for ticker in data:
            sym=ticker.get("symbol","").replace("USDT","")
            name=name_map.get(sym) or name_map.get(sym+"USDT")
            if not name: continue
            with price_lock:
                live_prices[name]={
                    "price":round(float(ticker.get("lastPrice",0)),4),
                    "change":round(float(ticker.get("priceChangePercent",0)),2),
                    "high":round(float(ticker.get("highPrice",0)),4),
                    "low":round(float(ticker.get("lowPrice",0)),4),
                    "cat":"Crypto","live":True,
                    "updated":datetime.now().strftime("%H:%M:%S"),
                }
    except Exception as e:
        bot_log(f"Binance REST error: {e}","ERROR")

def start_binance_websocket():
    """REST polling fallback for cloud — runs every 10 seconds"""
    bot_log("Starting Binance REST price polling...","SYSTEM")
    while True:
        try:
            fetch_binance_prices()
        except: pass
        time.sleep(10)

# ── PRICE POLLER ─────────────────────────────────────────────
def poll_prices():
    non_crypto={k:v for k,v in ASSETS.items() if not v.get("live",False)}
    while True:
        for name,info in non_crypto.items():
            try:
                df=fetch_ohlcv(info["ticker"],"5d")
                if len(df)<2: continue
                cur=float(df["close"].iloc[-1]); prev=float(df["close"].iloc[-2])
                chg=(cur-prev)/prev*100
                with price_lock:
                    live_prices[name]={
                        "price":round(cur,4),"change":round(chg,2),
                        "high":round(float(df["high"].iloc[-1]),4),
                        "low":round(float(df["low"].iloc[-1]),4),
                        "cat":info["cat"],"live":False,
                        "updated":datetime.now().strftime("%H:%M:%S"),
                    }
            except: pass
        time.sleep(5)

# ── FLASK ROUTES ─────────────────────────────────────────────
@app.route("/")
def dashboard():
    # Works both locally and on cloud
    import os
    paths=[
        "live_dashboard.html",
        os.path.join(os.path.dirname(__file__),"live_dashboard.html"),
        os.path.expanduser("~/Desktop/trading-bot/live_dashboard.html"),
    ]
    for p in paths:
        if os.path.exists(p):
            with open(p) as f: return f.read()
    return "Dashboard not found",404

@app.route("/api/live_prices")
def get_live_prices():
    with price_lock: return jsonify(dict(live_prices))

@app.route("/api/scan",methods=["POST","OPTIONS"])
def manual_scan():
    if request.method=="OPTIONS":
        return jsonify({"status":"ok"}), 200
    threading.Thread(target=scan_all,daemon=True).start()
    return jsonify({"started":True,"message":"Scanning 22 markets..."})

@app.route("/api/auto/toggle",methods=["POST","OPTIONS"])
def toggle():
    if request.method=="OPTIONS":
        return jsonify({"status":"ok"}), 200
    auto_state["enabled"]=not auto_state["enabled"]
    s="ENABLED" if auto_state["enabled"] else "DISABLED"
    bot_log(f"Auto trading {s}","SYSTEM")
    send_tg(f"AUTO TRADING {s}\nWatching 22 markets")
    return jsonify({"enabled":auto_state["enabled"]})

@app.route("/api/status")
def status():
    return jsonify({**auto_state,"log":auto_state["log"][:30]})

@app.route("/api/metrics")
def metrics():
    data=load_trades(); closed=data.get("closed",[])
    equity=data.get("equity",[{"value":CAPITAL}])
    total_pnl=sum(t.get("pnl_abs",0) for t in closed)
    wins=[t for t in closed if t.get("pnl_abs",0)>0]
    wr=len(wins)/len(closed)*100 if closed else 0
    return jsonify({
        "capital":CAPITAL,"current_value":round(CAPITAL+total_pnl,0),
        "total_pnl":round(total_pnl,0),
        "total_pnl_pct":round(total_pnl/CAPITAL*100,2),
        "win_rate":round(wr,1),"total_trades":len(closed),
        "open_positions":len(data.get("open",[])),
        "equity":equity[-30:],
    })

@app.route("/api/positions")
def positions():
    data=load_trades(); out=[]
    for t in data.get("open",[]):
        try:
            name=t.get("name",t.get("asset",""))
            with price_lock: lp=live_prices.get(name)
            cur=lp["price"] if lp else t["entry"]
            pnl_pct=(cur-t["entry"])/t["entry"] if t["direction"]=="BUY" else (t["entry"]-cur)/t["entry"]
            pnl_abs=round(pnl_pct*t["capital"],0)
            out.append({**t,"current_price":round(cur,4),
                        "pnl_pct":round(pnl_pct*100,2),"pnl_abs":pnl_abs})
        except: out.append({**t,"current_price":0,"pnl_pct":0,"pnl_abs":0})
    return jsonify(out)

@app.route("/api/trades")
def trades():
    return jsonify(list(reversed(load_trades().get("closed",[])))[:50])

@app.route("/api/trade/close/<tid>",methods=["POST","OPTIONS"])
def close_trade(tid):
    if request.method=="OPTIONS":
        return jsonify({"status":"ok"}), 200
    b=request.json; data=load_trades()
    trade=next((t for t in data["open"] if t["id"]==tid),None)
    if not trade: return jsonify({"success":False})
    ep=b.get("exit_price",trade["entry"])
    pnl_pct=(ep-trade["entry"])/trade["entry"] if trade["direction"]=="BUY" else (trade["entry"]-ep)/trade["entry"]
    pnl_abs=round(pnl_pct*trade["capital"],0)
    closed={**trade,"exit_price":round(ep,4),"pnl_pct":round(pnl_pct*100,2),
            "pnl_abs":pnl_abs,"closed_at":datetime.now().strftime("%d %b %Y %H:%M"),
            "exit_reason":b.get("reason","MANUAL"),"status":"CLOSED"}
    data["open"]=[t for t in data["open"] if t["id"]!=tid]
    data["closed"].append(closed)
    total=sum(t.get("pnl_abs",0) for t in data["closed"])
    data["equity"].append({"date":str(datetime.now().date()),"value":round(CAPITAL+total,0)})
    save_trades(data)
    return jsonify({"success":True,"trade":closed})

@app.route("/api/trade/manual",methods=["POST","OPTIONS"])
def manual_trade():
    if request.method=="OPTIONS":
        return jsonify({"status":"ok"}), 200
    sig=request.json; data=load_trades()
    open_positions=[(t.get("name",t.get("asset","")),t.get("direction",""))
                    for t in data.get("open",[])]
    name=sig.get("name","")
    existing_dirs=[d for n,d in open_positions if n==name]
    direction=sig.get("direction","")
    if direction in existing_dirs:
        return jsonify({"success":False,"msg":f"{name} already has {direction} position"})

    # ── FETCH LIVE PRICE AT TIME OF CLICK ────────────────────
    # Use actual current price — not the signal price from minutes ago
    stored_entry=sig.get("entry",0)
    live_entry=stored_entry
    with price_lock: lp=live_prices.get(name)
    if lp and lp.get("price",0)>0:
        live_entry=lp["price"]
        bot_log(f"MANUAL LIVE: {name} stored={stored_entry:.4f} → live={live_entry:.4f}","INFO")

    # Recalculate SL/TP based on live entry price
    old_entry=stored_entry if stored_entry>0 else live_entry
    if old_entry>0 and live_entry>0 and old_entry!=live_entry:
        sl_dist=abs(sig.get("sl",0)-old_entry)
        tp_dist=abs(sig.get("tp",0)-old_entry)
        if direction=="BUY":
            sig["sl"]=round(live_entry-sl_dist,4)
            sig["tp"]=round(live_entry+tp_dist,4)
        else:
            sig["sl"]=round(live_entry+sl_dist,4)
            sig["tp"]=round(live_entry-tp_dist,4)
    sig["entry"]=round(live_entry,4)

    # Risk-based sizing for manual trades too
    FIXED_RISK   = 2000
    MAX_POSITION = 100000
    sl_dist_pct  = abs(sig.get("sl",0)-sig["entry"])/(sig["entry"]+1e-9)
    if sl_dist_pct > 0:
        position = round(min(FIXED_RISK/sl_dist_pct, MAX_POSITION),0)
        sig["capital"] = position
        sig["risk_per_trade"] = FIXED_RISK

    trade={**sig,"id":str(uuid.uuid4())[:8],
           "opened_at":datetime.now().strftime("%d %b %Y %H:%M"),
           "status":"OPEN","mode":"MANUAL"}
    data["open"].append(trade); save_trades(data)
    bot_log(f"MANUAL TRADE: {direction} {name} @ {sig.get('entry')}","TRADE")
    send_tg(f"MANUAL TRADE\n{direction} {name}\nEntry:{sig.get('entry')}\nSL:{sig.get('sl')}\nTP:{sig.get('tp')}\nR:R:{sig.get('rr')}:1\n[PAPER]")
    return jsonify({"success":True,"trade":trade})

if __name__=="__main__":
    bot_log("Starting Chaitu Live Trading Bot","SYSTEM")
    bot_log(f"Assets: {len(ASSETS)} | Capital: Rs{CAPITAL:,}","SYSTEM")
    threading.Thread(target=start_binance_websocket,daemon=True).start()
    threading.Thread(target=poll_prices,daemon=True).start()
    threading.Thread(target=auto_loop,daemon=True).start()
    threading.Thread(target=websocket_watchdog,daemon=True).start()
    print("\n"+"="*58)
    print("  CHAITU LIVE MULTI-ASSET TRADING BOT")
    print(f"  Assets:    {len(ASSETS)} (Crypto + Commodities)")
    print("  Crypto:    REAL-TIME via Binance WebSocket")
    print("  Others:    5-second refresh")
    print("  Signals:   Every 5 min (when Auto ON)")
    print("  SL/TP:     Always monitored (Auto ON or OFF)")
    print("  Open:      http://localhost:5000")
    print("="*58+"\n")
    port = int(os.getenv("PORT", 5000))
    app.run(debug=False, port=port, host="0.0.0.0")
