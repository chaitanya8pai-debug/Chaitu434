from flask import Flask, jsonify, request
import yfinance as yf
import pandas as pd
import json, os, uuid, warnings, logging
from datetime import datetime
from dotenv import load_dotenv
import requests as req

warnings.filterwarnings("ignore")
logging.getLogger("werkzeug").setLevel(logging.ERROR)
load_dotenv("/Users/vinayaka/Desktop/trading-bot/.env")

app = Flask(__name__, static_folder=".", template_folder=".")
CAPITAL = 500000
TRADES_FILE = "/Users/vinayaka/Desktop/trading-bot/paper_trades.json"
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN","")
CHAT  = os.getenv("TELEGRAM_CHAT_ID","")

def load_trades():
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE) as f: return json.load(f)
    return {"open":[], "closed":[], "equity":[{"date":str(datetime.now().date()),"value":CAPITAL}]}

def save_trades(data):
    with open(TRADES_FILE,"w") as f: json.dump(data,f,indent=2)

def send_tg(msg):
    if not TOKEN or not CHAT: return
    try: req.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",json={"chat_id":CHAT,"text":msg},timeout=5)
    except: pass

def fetch(ticker, period="3mo"):
    df = yf.download(ticker,period=period,auto_adjust=True,progress=False)
    if isinstance(df.columns,pd.MultiIndex): df.columns = df.columns.get_level_values(0)
    df.columns = [c.lower() for c in df.columns]
    df.index = pd.to_datetime(df.index,utc=True)
    return df[["open","high","low","close","volume"]].dropna()

@app.route("/")
def dashboard():
    with open("/Users/vinayaka/Desktop/trading-bot/dashboard.html") as f: return f.read()

@app.route("/api/prices")
def prices():
    result = {}
    for name,ticker in {"Gold":"GC=F","BTC":"BTC-USD","ETH":"ETH-USD"}.items():
        try:
            df = fetch(ticker,"5d")
            cur  = float(df["close"].iloc[-1])
            prev = float(df["close"].iloc[-2])
            chg  = (cur-prev)/prev*100
            result[name] = {"price":round(cur,2),"change":round(chg,2),"high":round(float(df["high"].iloc[-1]),2),"low":round(float(df["low"].iloc[-1]),2)}
        except Exception as e:
            result[name] = {"price":0,"change":0,"error":str(e)}
    return jsonify(result)

@app.route("/api/signals")
def signals():
    try:
        import sys; sys.path.insert(0,"/Users/vinayaka/Desktop/trading-bot")
        from strategy_engine import StrategyOrchestrator, Asset
        gold=fetch("GC=F"); btc=fetch("BTC-USD"); eth=fetch("ETH-USD")
        data=load_trades()
        val=data["equity"][-1]["value"] if data["equity"] else CAPITAL
        orch=StrategyOrchestrator(capital=CAPITAL,win_rate=0.55)
        sigs=orch.evaluate(gold,btc,eth,current_portfolio_value=val)
        out=[]
        for s in sigs:
            out.append({"asset":s.asset.value,"signal":s.signal.name,
                "direction":"BUY" if s.signal.value>0 else "SELL",
                "regime":s.regime.value,"confidence":round(s.confidence*100,1),
                "entry":round(s.entry_price,2),"stop_loss":round(s.stop_loss,2),
                "take_profit":round(s.take_profit,2),
                "size_pct":round(s.position_size_pct*100,1),
                "capital":round(s.position_size_pct*CAPITAL,0),
                "time":datetime.now().strftime("%H:%M")})
        return jsonify({"signals":out,"count":len(out),"scanned_at":datetime.now().strftime("%d %b %H:%M")})
    except Exception as e:
        return jsonify({"signals":[],"count":0,"error":str(e),"scanned_at":datetime.now().strftime("%d %b %H:%M")})

@app.route("/api/metrics")
def metrics():
    data=load_trades()
    closed=data.get("closed",[]); equity=data.get("equity",[{"value":CAPITAL}])
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
            ticker={"XAUUSD":"GC=F","BTCUSDT":"BTC-USD","ETHUSDT":"ETH-USD"}.get(t["asset"],"BTC-USD")
            df=fetch(ticker,"2d"); cur=float(df["close"].iloc[-1])
            pnl_pct=(cur-t["entry"])/t["entry"]*100 if t["direction"]=="BUY" else (t["entry"]-cur)/t["entry"]*100
            pnl_abs=pnl_pct/100*t["capital"]
            out.append({**t,"current_price":round(cur,2),"pnl_pct":round(pnl_pct,2),"pnl_abs":round(pnl_abs,0)})
        except: out.append({**t,"current_price":0,"pnl_pct":0,"pnl_abs":0})
    return jsonify(out)

@app.route("/api/trades")
def trades():
    data=load_trades()
    return jsonify(list(reversed(data.get("closed",[])))[:50])

@app.route("/api/trade/open",methods=["POST"])
def open_trade():
    b=request.json; data=load_trades()
    trade={**b,"id":str(uuid.uuid4())[:8],"opened_at":datetime.now().strftime("%d %b %Y %H:%M"),"status":"OPEN"}
    data["open"].append(trade); save_trades(data)
    send_tg(f"PAPER TRADE OPENED\n{trade.get('direction')} {trade.get('asset')}\nEntry: {trade.get('entry')}\nCapital: Rs{trade.get('capital'):,.0f}")
    return jsonify({"success":True,"trade":trade})

@app.route("/api/trade/close/<tid>",methods=["POST"])
def close_trade(tid):
    b=request.json; data=load_trades()
    trade=next((t for t in data["open"] if t["id"]==tid),None)
    if not trade: return jsonify({"success":False,"error":"Not found"})
    exit_price=b.get("exit_price",trade["entry"])
    pnl_pct=(exit_price-trade["entry"])/trade["entry"]*100 if trade["direction"]=="BUY" else (trade["entry"]-exit_price)/trade["entry"]*100
    pnl_abs=pnl_pct/100*trade["capital"]
    closed={**trade,"exit_price":exit_price,"pnl_pct":round(pnl_pct,2),"pnl_abs":round(pnl_abs,0),
            "closed_at":datetime.now().strftime("%d %b %Y %H:%M"),"exit_reason":b.get("reason","MANUAL"),"status":"CLOSED"}
    data["open"]=[t for t in data["open"] if t["id"]!=tid]
    data["closed"].append(closed)
    total=sum(t.get("pnl_abs",0) for t in data["closed"])
    data["equity"].append({"date":str(datetime.now().date()),"value":round(CAPITAL+total,0)})
    save_trades(data)
    send_tg(f"PAPER TRADE CLOSED\n{closed['direction']} {closed['asset']}\nPnL: Rs{pnl_abs:+,.0f} ({pnl_pct:+.2f}%)")
    return jsonify({"success":True,"trade":closed})

if __name__=="__main__":
    print("\n" + "="*50)
    print("  CHAITU TRADING DASHBOARD")
    print("  Open browser: http://localhost:5000")
    print("="*50 + "\n")
    app.run(debug=False,port=5000,host="0.0.0.0")
