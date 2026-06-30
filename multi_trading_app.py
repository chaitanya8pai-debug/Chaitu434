
from flask import Flask, jsonify, request
import yfinance as yf
import pandas as pd
import json, os, uuid, warnings, logging, threading, time
from datetime import datetime
from dotenv import load_dotenv
import requests as req

warnings.filterwarnings("ignore")
logging.getLogger("werkzeug").setLevel(logging.ERROR)
load_dotenv("/Users/vinayaka/Desktop/trading-bot/.env")

app     = Flask(__name__, static_folder=".", template_folder=".")
CAPITAL = 500000
TRADES_FILE = "/Users/vinayaka/Desktop/trading-bot/paper_trades.json"
TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN","")
CHAT    = os.getenv("TELEGRAM_CHAT_ID","")

# ── FULL ASSET UNIVERSE ──────────────────────────────────────
ASSETS = {
    "Brent Crude":   {"ticker":"BZ=F",         "cat":"Commodity","icon":"oil"},
    "WTI Crude":     {"ticker":"CL=F",         "cat":"Commodity","icon":"oil"},
    "Natural Gas":   {"ticker":"NG=F",         "cat":"Commodity","icon":"fire"},
    "Gold":          {"ticker":"GC=F",         "cat":"Commodity","icon":"gold"},
    "Silver":        {"ticker":"SI=F",         "cat":"Commodity","icon":"silver"},
    "Copper":        {"ticker":"HG=F",         "cat":"Commodity","icon":"copper"},
    "Platinum":      {"ticker":"PL=F",         "cat":"Commodity","icon":"platinum"},
    "Nifty 50":      {"ticker":"^NSEI",        "cat":"Index",    "icon":"chart"},
    "Sensex":        {"ticker":"^BSESN",       "cat":"Index",    "icon":"chart"},
    "Bank Nifty":    {"ticker":"^NSEBANK",     "cat":"Index",    "icon":"bank"},
    "Nifty IT":      {"ticker":"^CNXIT",       "cat":"Index",    "icon":"tech"},
    "S&P 500":       {"ticker":"^GSPC",        "cat":"Index",    "icon":"chart"},
    "Nasdaq":        {"ticker":"^IXIC",        "cat":"Index",    "icon":"tech"},
    "Dow Jones":     {"ticker":"^DJI",         "cat":"Index",    "icon":"chart"},
    "Reliance":      {"ticker":"RELIANCE.NS",  "cat":"Stock",    "icon":"stock"},
    "TCS":           {"ticker":"TCS.NS",       "cat":"Stock",    "icon":"tech"},
    "HDFC Bank":     {"ticker":"HDFCBANK.NS",  "cat":"Stock",    "icon":"bank"},
    "Infosys":       {"ticker":"INFY.NS",      "cat":"Stock",    "icon":"tech"},
    "ICICI Bank":    {"ticker":"ICICIBANK.NS", "cat":"Stock",    "icon":"bank"},
    "Wipro":         {"ticker":"WIPRO.NS",     "cat":"Stock",    "icon":"tech"},
    "Adani Ports":   {"ticker":"ADANIPORTS.NS","cat":"Stock",    "icon":"stock"},
    "Bajaj Finance": {"ticker":"BAJFINANCE.NS","cat":"Stock",    "icon":"bank"},
    "L&T":           {"ticker":"LT.NS",        "cat":"Stock",    "icon":"stock"},
    "ONGC":          {"ticker":"ONGC.NS",      "cat":"Stock",    "icon":"oil"},
    "USD/INR":       {"ticker":"USDINR=X",     "cat":"Forex",    "icon":"forex"},
    "EUR/USD":       {"ticker":"EURUSD=X",     "cat":"Forex",    "icon":"forex"},
    "GBP/USD":       {"ticker":"GBPUSD=X",     "cat":"Forex",    "icon":"forex"},
    "USD/JPY":       {"ticker":"USDJPY=X",     "cat":"Forex",    "icon":"forex"},
    "EUR/INR":       {"ticker":"EURINR=X",     "cat":"Forex",    "icon":"forex"},
    "GBP/INR":       {"ticker":"GBPINR=X",     "cat":"Forex",    "icon":"forex"},
    "Bitcoin":       {"ticker":"BTC-USD",      "cat":"Crypto",   "icon":"btc"},
    "Ethereum":      {"ticker":"ETH-USD",      "cat":"Crypto",   "icon":"eth"},
    "Solana":        {"ticker":"SOL-USD",      "cat":"Crypto",   "icon":"crypto"},
    "BNB":           {"ticker":"BNB-USD",      "cat":"Crypto",   "icon":"crypto"},
}

# Priority order for scanning (smart money markets first)
SCAN_PRIORITY = [
    "Brent Crude","WTI Crude","Gold","Silver","Natural Gas","Copper","Platinum",
    "Nifty 50","Bank Nifty","Sensex","Nifty IT",
    "Reliance","TCS","HDFC Bank","Infosys","ICICI Bank","Wipro","L&T","ONGC","Adani Ports","Bajaj Finance",
    "S&P 500","Nasdaq","Dow Jones",
    "USD/INR","EUR/USD","GBP/USD","USD/JPY","EUR/INR","GBP/INR",
    "Bitcoin","Ethereum","Solana","BNB",
]

auto_state = {
    "enabled":False,"scan_interval":1800,"check_interval":300,
    "last_scan":"Never","last_check":"Never","status":"Idle",
    "trades_today":0,"max_trades_day":5,"log":[],"top_signals":[]
}

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

def send_tg(msg):
    if not TOKEN or not CHAT: return
    try: req.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                  json={"chat_id":CHAT,"text":msg},timeout=5)
    except: pass

def fetch(ticker,period="3mo"):
    df=yf.download(ticker,period=period,auto_adjust=True,progress=False)
    if isinstance(df.columns,pd.MultiIndex): df.columns=df.columns.get_level_values(0)
    df.columns=[c.lower() for c in df.columns]
    return df[["open","high","low","close","volume"]].dropna()

def get_price(ticker):
    df=fetch(ticker,"5d")
    cur=float(df["close"].iloc[-1])
    prev=float(df["close"].iloc[-2])
    chg=(cur-prev)/prev*100
    return {"price":round(cur,2),"change":round(chg,2),
            "high":round(float(df["high"].iloc[-1]),2),
            "low":round(float(df["low"].iloc[-1]),2)}

def smart_levels(df, direction):
    """
    Calculate institutional-grade SL and TP levels.
    
    SL placement:
    - BUY:  Below recent swing low (structure support)
    - SELL: Above recent swing high (structure resistance)
    - Plus ATR buffer for noise
    
    TP placement:
    - Based on next key resistance/support
    - Minimum 2:1 Risk:Reward enforced
    - Uses recent swing highs/lows as targets
    """
    close = df["close"]; high = df["high"]; low = df["low"]
    
    # ATR for volatility buffer
    tr = pd.concat([high-low,(high-close.shift()).abs(),(low-close.shift()).abs()],axis=1).max(axis=1)
    atr = float(tr.ewm(span=14).mean().iloc[-1])
    
    cur = float(close.iloc[-1])
    
    # Find swing highs and lows (last 20 bars)
    recent = df.tail(20)
    swing_highs = []
    swing_lows  = []
    for i in range(2, len(recent)-2):
        if recent["high"].iloc[i] == recent["high"].iloc[i-2:i+3].max():
            swing_highs.append(float(recent["high"].iloc[i]))
        if recent["low"].iloc[i] == recent["low"].iloc[i-2:i+3].min():
            swing_lows.append(float(recent["low"].iloc[i]))
    
    if direction == "BUY":
        # SL = below nearest swing low + ATR buffer
        valid_lows = [l for l in swing_lows if l < cur]
        if valid_lows:
            nearest_low = max(valid_lows)
            sl = nearest_low - (0.5 * atr)  # Buffer below swing low
        else:
            sl = cur - (1.5 * atr)
        
        # TP = nearest swing high above current price
        valid_highs = [h for h in swing_highs if h > cur]
        if valid_highs:
            nearest_high = min(valid_highs)
            # Ensure minimum 2:1 R:R
            risk = cur - sl
            natural_rr = (nearest_high - cur) / risk if risk > 0 else 0
            if natural_rr >= 2.0:
                tp = nearest_high
            else:
                tp = cur + (2.5 * risk)  # Force 2.5:1 R:R
        else:
            risk = cur - sl
            tp = cur + (2.5 * risk)
    
    else:  # SELL
        # SL = above nearest swing high + ATR buffer
        valid_highs = [h for h in swing_highs if h > cur]
        if valid_highs:
            nearest_high = min(valid_highs)
            sl = nearest_high + (0.5 * atr)
        else:
            sl = cur + (1.5 * atr)
        
        # TP = nearest swing low below current price
        valid_lows = [l for l in swing_lows if l < cur]
        if valid_lows:
            nearest_low = max(valid_lows)
            risk = sl - cur
            natural_rr = (cur - nearest_low) / risk if risk > 0 else 0
            if natural_rr >= 2.0:
                tp = nearest_low
            else:
                tp = cur - (2.5 * risk)
        else:
            risk = sl - cur
            tp = cur - (2.5 * risk)
    
    # Calculate actual R:R
    risk   = abs(cur - sl)
    reward = abs(tp - cur)
    rr     = round(reward / risk, 2) if risk > 0 else 0
    
    return round(sl, 4), round(tp, 4), round(atr, 4), rr

def fast_signal(df):
    close=df["close"]; vol=df["volume"]
    ema9=close.ewm(span=9).mean(); ema21=close.ewm(span=21).mean()
    ema50=close.ewm(span=50).mean()
    delta=close.diff(); gain=delta.clip(lower=0).ewm(span=14).mean()
    loss=(-delta.clip(upper=0)).ewm(span=14).mean()
    rsi=100-(100/(1+gain/(loss+1e-9)))
    tp_vwap=(df["high"]+df["low"]+close)/3
    vwap=(tp_vwap*vol).cumsum()/vol.cumsum()
    hl=(df["high"]-df["low"]).replace(0,1e-9)
    delta_vol=((close-df["low"])/hl-(df["high"]-close)/hl)*vol
    cum_delta=delta_vol.rolling(20).sum()
    
    # Wyckoff: volume trend
    vol_trend = vol.rolling(10).mean() / vol.rolling(30).mean()
    
    # Market structure: HH/HL
    high=df["high"]; low_=df["low"]
    recent_hh = float(high.tail(5).max()) > float(high.tail(10).head(5).max())
    recent_hl  = float(low_.tail(5).min()) > float(low_.tail(10).head(5).min())
    
    cur=float(close.iloc[-1])
    score=(
        (2 if ema9.iloc[-1]>ema21.iloc[-1] else -2)+        # EMA crossover
        (1 if cur>ema50.iloc[-1] else -1)+                   # Trend
        (1 if rsi.iloc[-1]>55 else -1 if rsi.iloc[-1]<45 else 0)+ # RSI
        (1 if cur>vwap.iloc[-1] else -1)+                    # VWAP
        (1 if cum_delta.iloc[-1]>0 else -1)+                 # Order flow
        (1 if vol_trend.iloc[-1]>1.1 else 0)+                # Volume confirmation
        (1 if (recent_hh and recent_hl) else -1 if (not recent_hh and not recent_hl) else 0) # Structure
    )
    conf=abs(score)/8.0
    if score>=4:   return "BUY",round(conf,2)
    elif score<=-4:return "SELL",round(conf,2)
    return "NEUTRAL",0.0

def scan_all():
    auto_state["status"]="Scanning 34 markets..."
    bot_log("Full market scan started","INFO")
    signals=[]
    data=load_trades()
    open_assets={t["asset"] for t in data.get("open",[])}

    for name in SCAN_PRIORITY:
        if auto_state["trades_today"]>=auto_state["max_trades_day"]: break
        try:
            ticker=ASSETS[name]["ticker"]
            df=fetch(ticker)
            if len(df)<60: continue
            direction,conf=fast_signal(df)
            if direction=="NEUTRAL" or conf<0.55: continue

            entry=float(df["close"].iloc[-1])
            sl,tp,atr,rr=smart_levels(df,direction)
            # Skip if R:R below 1.8
            if rr < 1.8:
                bot_log(f"Skip {name}: R:R={rr} too low","INFO")
                continue

            risk   = abs(entry-sl)
            reward = abs(tp-entry)
            rr_actual = round(reward/risk,2) if risk>0 else 0
            sig={"name":name,"ticker":ticker,"cat":ASSETS[name]["cat"],
                 "direction":direction,"confidence":conf,"entry":round(entry,2),
                 "sl":round(sl,2),"tp":round(tp,2),"rr":rr_actual,"atr":round(atr,2),
                 "capital":round(0.1*CAPITAL,0),
                 "time":datetime.now().strftime("%H:%M")}
            signals.append(sig)

            if name not in open_assets and auto_state["enabled"]:
                trade={**sig,"id":str(uuid.uuid4())[:8],
                       "opened_at":datetime.now().strftime("%d %b %Y %H:%M"),
                       "status":"OPEN","mode":"AUTO"}
                data["open"].append(trade)
                open_assets.add(name)
                auto_state["trades_today"]+=1
                bot_log(f"AUTO TRADE: {direction} {name} @ {entry:.2f} | Conf:{conf:.0%}","TRADE")
                send_tg(f"AUTO TRADE\n{direction} {name}\nEntry:{entry:.2f} SL:{sl:.2f} TP:{tp:.2f}\nConf:{conf:.0%}\n[PAPER]")

        except: pass

    auto_state["top_signals"]=signals[:10]
    auto_state["last_scan"]=datetime.now().strftime("%d %b %H:%M")
    auto_state["status"]=f"Watching 34 markets | {len(signals)} signal(s) found"
    if signals: save_trades(data)
    bot_log(f"Scan complete: {len(signals)} signals across 34 markets")
    return signals

def check_positions():
    data=load_trades()
    if not data.get("open"): return
    changed=False
    for trade in list(data["open"]):
        try:
            ticker=ASSETS.get(trade.get("name",trade.get("asset","BTC")),{}).get("ticker","BTC-USD")
            df=fetch(ticker,"2d")
            cur=float(df["close"].iloc[-1])
            sl=trade["sl"]; tp=trade["tp"]; direction=trade["direction"]
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
                closed={**trade,"exit_price":ep,"pnl_pct":round(pnl_pct,2),"pnl_abs":pnl_abs,
                        "closed_at":datetime.now().strftime("%d %b %Y %H:%M"),"exit_reason":reason,"status":"CLOSED"}
                data["open"]=[t for t in data["open"] if t["id"]!=trade["id"]]
                data["closed"].append(closed)
                total=sum(t.get("pnl_abs",0) for t in data["closed"])
                data["equity"].append({"date":str(datetime.now().date()),"value":round(CAPITAL+total,0)})
                changed=True
                bot_log(f"CLOSED {trade.get('name','')} via {reason} | PnL:Rs{pnl_abs:+,.0f}","TRADE")
                send_tg(f"TRADE CLOSED\n{direction} {trade.get('name','')}\nReason:{reason}\nPnL:Rs{pnl_abs:+,.0f}\n[PAPER]")
        except: pass
    if changed: save_trades(data)

def auto_loop():
    scan_counter=0; check_counter=0
    while True:
        time.sleep(60)
        if not auto_state["enabled"]: continue
        check_counter+=60; scan_counter+=60
        if check_counter>=auto_state["check_interval"]:
            check_positions(); check_counter=0
            auto_state["last_check"]=datetime.now().strftime("%d %b %H:%M")
        if scan_counter>=auto_state["scan_interval"]:
            scan_all(); scan_counter=0
        if datetime.now().strftime("%H:%M")=="00:00":
            auto_state["trades_today"]=0

@app.route("/")
def dashboard():
    with open("/Users/vinayaka/Desktop/trading-bot/multi_dashboard.html") as f: return f.read()

@app.route("/api/prices")
def prices():
    result={}
    for name,info in ASSETS.items():
        try: result[name]={**get_price(info["ticker"]),"cat":info["cat"],"icon":info["icon"]}
        except: result[name]={"price":0,"change":0,"cat":info["cat"],"icon":info["icon"]}
    return jsonify(result)

@app.route("/api/scan",methods=["POST"])
def manual_scan():
    threading.Thread(target=scan_all,daemon=True).start()
    return jsonify({"started":True})

@app.route("/api/auto/toggle",methods=["POST"])
def toggle():
    auto_state["enabled"]=not auto_state["enabled"]
    s="ENABLED" if auto_state["enabled"] else "DISABLED"
    bot_log(f"Auto trading {s}","SYSTEM")
    send_tg(f"AUTO TRADING {s}\nWatching 34 markets")
    return jsonify({"enabled":auto_state["enabled"]})

@app.route("/api/status")
def status():
    return jsonify({**auto_state,"log":auto_state["log"][:30]})

@app.route("/api/metrics")
def metrics():
    data=load_trades()
    closed=data.get("closed",[])
    equity=data.get("equity",[{"value":CAPITAL}])
    total_pnl=sum(t.get("pnl_abs",0) for t in closed)
    wins=[t for t in closed if t.get("pnl_abs",0)>0]
    wr=len(wins)/len(closed)*100 if closed else 0
    return jsonify({"capital":CAPITAL,"current_value":round(CAPITAL+total_pnl,0),
        "total_pnl":round(total_pnl,0),"total_pnl_pct":round(total_pnl/CAPITAL*100,2),
        "win_rate":round(wr,1),"total_trades":len(closed),
        "open_positions":len(data.get("open",[])),
        "equity":equity[-30:]})

@app.route("/api/positions")
def positions():
    data=load_trades(); out=[]
    for t in data.get("open",[]):
        try:
            name=t.get("name",t.get("asset","BTC"))
            ticker=ASSETS.get(name,{}).get("ticker","BTC-USD")
            df=fetch(ticker,"2d"); cur=float(df["close"].iloc[-1])
            pnl_pct=(cur-t["entry"])/t["entry"] if t["direction"]=="BUY" else (t["entry"]-cur)/t["entry"]
            pnl_abs=round(pnl_pct*t["capital"],0)
            out.append({**t,"current_price":round(cur,2),"pnl_pct":round(pnl_pct*100,2),"pnl_abs":pnl_abs})
        except: out.append({**t,"current_price":0,"pnl_pct":0,"pnl_abs":0})
    return jsonify(out)

@app.route("/api/trades")
def trades():
    return jsonify(list(reversed(load_trades().get("closed",[])))[:50])

@app.route("/api/trade/close/<tid>",methods=["POST"])
def close_trade(tid):
    b=request.json; data=load_trades()
    trade=next((t for t in data["open"] if t["id"]==tid),None)
    if not trade: return jsonify({"success":False})
    ep=b.get("exit_price",trade["entry"])
    pnl_pct=(ep-trade["entry"])/trade["entry"] if trade["direction"]=="BUY" else (trade["entry"]-ep)/trade["entry"]
    pnl_abs=round(pnl_pct*trade["capital"],0)
    closed={**trade,"exit_price":ep,"pnl_pct":round(pnl_pct*100,2),"pnl_abs":pnl_abs,
            "closed_at":datetime.now().strftime("%d %b %Y %H:%M"),"exit_reason":b.get("reason","MANUAL"),"status":"CLOSED"}
    data["open"]=[t for t in data["open"] if t["id"]!=tid]
    data["closed"].append(closed)
    total=sum(t.get("pnl_abs",0) for t in data["closed"])
    data["equity"].append({"date":str(datetime.now().date()),"value":round(CAPITAL+total,0)})
    save_trades(data)
    return jsonify({"success":True,"trade":closed})

if __name__=="__main__":
    threading.Thread(target=auto_loop,daemon=True).start()
    bot_log("Multi-asset bot started — 34 markets","SYSTEM")
    print("\n"+"="*55)
    print("  CHAITU MULTI-ASSET TRADING BOT")
    print("  Markets: 34 (Commodities+Stocks+Forex+Crypto)")
    print("  Open:    http://localhost:5000")
    print("="*55+"\n")
    app.run(debug=False,port=5000,host="0.0.0.0")
