"""
=============================================================
  CHAITU AUTONOMOUS TRADING DASHBOARD
  Fully automated paper trading — no intervention needed
  Run: python3 auto_trading_app.py
  Open: http://localhost:5000
=============================================================
"""
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

app = Flask(__name__, static_folder=".", template_folder=".")
CAPITAL     = 500000
TRADES_FILE = "/Users/vinayaka/Desktop/trading-bot/paper_trades.json"
LOG_FILE    = "/Users/vinayaka/Desktop/trading-bot/bot_log.json"
TOKEN       = os.getenv("TELEGRAM_BOT_TOKEN","")
CHAT        = os.getenv("TELEGRAM_CHAT_ID","")

# Auto-trader state
auto_state = {
    "enabled":       False,
    "scan_interval": 1800,   # 30 minutes between signal scans
    "check_interval": 300,   # 5 minutes between position checks
    "last_scan":     "Never",
    "last_check":    "Never",
    "status":        "Idle",
    "trades_today":  0,
    "max_trades_day": 3,     # Safety: max 3 auto-trades per day
    "log":           [],
}

# ── Helpers ──────────────────────────────────────────────────

def load_trades():
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE) as f: return json.load(f)
    return {"open":[], "closed":[], "equity":[{"date":str(datetime.now().date()),"value":CAPITAL}]}

def save_trades(data):
    with open(TRADES_FILE,"w") as f: json.dump(data,f,indent=2)

def bot_log(msg, level="INFO"):
    entry = {"time": datetime.now().strftime("%d %b %H:%M:%S"), "msg": msg, "level": level}
    auto_state["log"].insert(0, entry)
    auto_state["log"] = auto_state["log"][:100]   # Keep last 100 logs
    print(f"[{entry['time']}] {level}: {msg}")

def send_tg(msg):
    if not TOKEN or not CHAT: return
    try: req.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                  json={"chat_id":CHAT,"text":msg},timeout=5)
    except: pass

def fetch(ticker, period="3mo"):
    df = yf.download(ticker, period=period, auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
    df.columns = [c.lower() for c in df.columns]
    df.index = pd.to_datetime(df.index, utc=True)
    return df[["open","high","low","close","volume"]].dropna()

def get_live_price(ticker):
    df = fetch(ticker, period="2d")
    return float(df["close"].iloc[-1])

# ── Auto Position Monitor (runs every 5 min) ─────────────────

def check_positions():
    """Check if any open positions hit SL or TP."""
    data = load_trades()
    if not data.get("open"): return
    ticker_map = {"XAUUSD":"GC=F","BTCUSDT":"BTC-USD","ETHUSDT":"ETH-USD"}
    changed = False

    for trade in list(data["open"]):
        try:
            ticker = ticker_map.get(trade["asset"], "BTC-USD")
            cur    = get_live_price(ticker)
            entry  = trade["entry"]
            sl     = trade["stop_loss"]
            tp     = trade["take_profit"]
            direction = trade["direction"]
            reason = None

            if direction == "BUY":
                if cur <= sl: reason = "SL"
                elif cur >= tp: reason = "TP"
            else:
                if cur >= sl: reason = "SL"
                elif cur <= tp: reason = "TP"

            if reason:
                exit_price = sl if reason == "SL" else tp
                pnl_pct = (exit_price-entry)/entry*100 if direction=="BUY" else (entry-exit_price)/entry*100
                pnl_abs = pnl_pct/100 * trade["capital"]
                closed = {**trade, "exit_price":round(exit_price,2),
                          "pnl_pct":round(pnl_pct,2), "pnl_abs":round(pnl_abs,0),
                          "closed_at":datetime.now().strftime("%d %b %Y %H:%M"),
                          "exit_reason":reason, "status":"CLOSED"}
                data["open"]   = [t for t in data["open"] if t["id"]!=trade["id"]]
                data["closed"].append(closed)
                total = sum(t.get("pnl_abs",0) for t in data["closed"])
                data["equity"].append({"date":str(datetime.now().date()),"value":round(CAPITAL+total,0)})
                changed = True
                icon = "✅" if pnl_abs >= 0 else "❌"
                msg  = f"{icon} AUTO-CLOSED {direction} {trade['asset']}\nReason: {reason}\nExit: {exit_price:,.2f}\nP&L: {'+'if pnl_abs>=0 else ''}Rs{pnl_abs:,.0f} ({pnl_pct:+.2f}%)"
                bot_log(f"Auto-closed {trade['asset']} via {reason} | P&L: Rs{pnl_abs:+,.0f}", "TRADE")
                send_tg(msg)

        except Exception as e:
            bot_log(f"Position check error for {trade.get('asset')}: {e}", "ERROR")

    if changed:
        save_trades(data)

# ── Auto Signal Scanner (runs every 30 min) ──────────────────

def scan_and_execute():
    """Scan for signals and auto-execute paper trades."""
    import sys
    sys.path.insert(0, "/Users/vinayaka/Desktop/trading-bot")

    auto_state["status"] = "Scanning..."
    bot_log("Auto-scan started")

    # Safety: max trades per day
    if auto_state["trades_today"] >= auto_state["max_trades_day"]:
        bot_log(f"Max trades/day ({auto_state['max_trades_day']}) reached — skipping scan", "WARN")
        auto_state["status"] = "Max trades reached today"
        return

    try:
        from smart_money_engine import SmartMoneyOrchestrator
        gold  = fetch("GC=F"); btc = fetch("BTC-USD")
        eth   = fetch("ETH-USD"); brent = fetch("BZ=F")
        data  = load_trades()
        open_assets = {t["asset"] for t in data.get("open",[])}
        orch  = SmartMoneyOrchestrator(capital=CAPITAL, win_rate=0.55)
        # Evaluate all 4 assets
        raw_sigs = orch.evaluate_all(gold, btc, eth)
        try:
            brent_sig = orch.evaluate(brent, "BRENTOIL")
            if brent_sig: raw_sigs.append(brent_sig)
        except: pass
        # Convert SmartMoney signals to standard format
        class FakeSig:
            pass
        sigs = []
        for s in raw_sigs:
            fs = FakeSig()
            fs.asset = type("A", (), {"value": s.asset})()
            fs.signal = type("S", (), {"value": 1 if "BULL" in s.bias.value else -1})()
            fs.signal.name = s.bias.value
            fs.regime = type("R", (), {"value": s.wyckoff_phase.value})()
            fs.confidence = s.confidence
            fs.entry_price = s.entry_price
            fs.stop_loss = s.stop_loss
            fs.take_profit = s.take_profit
            fs.position_size_pct = s.position_size_pct
            sigs.append(fs)
        auto_state["last_scan"] = datetime.now().strftime("%d %b %H:%M")

        if not sigs:
            bot_log("Scan complete — no signals found")
            auto_state["status"] = "Watching — no signals"
            send_tg(f"AUTO SCAN {auto_state['last_scan']}\nNo signals. Markets watched.\nGold:${float(gold['close'].iloc[-1]):,.0f} BTC:${float(btc['close'].iloc[-1]):,.0f}")
            return

        executed = 0
        for sig in sigs:
            if sig.asset.value in open_assets:
                bot_log(f"Skipping {sig.asset.value} — position already open")
                continue
            if auto_state["trades_today"] >= auto_state["max_trades_day"]:
                break

            direction = "BUY" if sig.signal.value > 0 else "SELL"
            trade = {
                "id":          str(uuid.uuid4())[:8],
                "asset":       sig.asset.value,
                "direction":   direction,
                "entry":       round(sig.entry_price, 2),
                "stop_loss":   round(sig.stop_loss, 2),
                "take_profit": round(sig.take_profit, 2),
                "capital":     round(sig.position_size_pct * CAPITAL, 0),
                "size_pct":    round(sig.position_size_pct * 100, 1),
                "confidence":  round(sig.confidence * 100, 1),
                "regime":      sig.regime.value,
                "opened_at":   datetime.now().strftime("%d %b %Y %H:%M"),
                "status":      "OPEN",
                "mode":        "AUTO",
            }
            data["open"].append(trade)
            open_assets.add(sig.asset.value)
            auto_state["trades_today"] += 1
            executed += 1

            msg = (f"🤖 AUTO TRADE OPENED\n"
                   f"{direction} {sig.asset.value}\n"
                   f"Signal: {sig.signal.name} ({sig.confidence:.0%})\n"
                   f"Regime: {sig.regime.value}\n"
                   f"Entry:  {sig.entry_price:,.2f}\n"
                   f"SL:     {sig.stop_loss:,.2f}\n"
                   f"TP:     {sig.take_profit:,.2f}\n"
                   f"Capital: Rs{sig.position_size_pct*CAPITAL:,.0f}\n"
                   f"[PAPER TRADE]")
            bot_log(f"AUTO-OPENED {direction} {sig.asset.value} @ {sig.entry_price:.2f} | Confidence: {sig.confidence:.0%}", "TRADE")
            send_tg(msg)

        save_trades(data)
        auto_state["status"] = f"Active — {executed} trade(s) opened"
        if executed == 0:
            auto_state["status"] = "Watching — positions already open"

    except Exception as e:
        bot_log(f"Scan error: {e}", "ERROR")
        auto_state["status"] = f"Error: {str(e)[:40]}"

# ── Background Thread ─────────────────────────────────────────

def auto_trading_loop():
    """Master loop — runs scan + position checks in background."""
    scan_counter  = 0
    check_counter = 0

    while True:
        time.sleep(60)   # Wake up every 60 seconds
        if not auto_state["enabled"]:
            continue

        check_counter += 60
        scan_counter  += 60

        # Position check every 5 minutes
        if check_counter >= auto_state["check_interval"]:
            auto_state["last_check"] = datetime.now().strftime("%d %b %H:%M")
            check_positions()
            check_counter = 0

        # Signal scan every 30 minutes
        if scan_counter >= auto_state["scan_interval"]:
            scan_and_execute()
            scan_counter = 0

        # Reset daily trade counter at midnight
        if datetime.now().strftime("%H:%M") == "00:00":
            auto_state["trades_today"] = 0
            bot_log("Daily trade counter reset")

# ── API Endpoints ─────────────────────────────────────────────

@app.route("/")
def dashboard():
    with open("/Users/vinayaka/Desktop/trading-bot/auto_dashboard.html") as f:
        return f.read()

@app.route("/api/auto/toggle", methods=["POST"])
def toggle_auto():
    auto_state["enabled"] = not auto_state["enabled"]
    status = "ENABLED" if auto_state["enabled"] else "DISABLED"
    bot_log(f"Auto-trading {status}", "SYSTEM")
    send_tg(f"AUTO TRADING {status}\nBot will {'scan every 30min & execute signals' if auto_state['enabled'] else 'stop auto-executing'}")
    return jsonify({"enabled": auto_state["enabled"], "status": status})

@app.route("/api/auto/scan", methods=["POST"])
def manual_scan():
    threading.Thread(target=scan_and_execute, daemon=True).start()
    return jsonify({"started": True})

@app.route("/api/auto/status")
def get_auto_status():
    return jsonify({
        "enabled":        auto_state["enabled"],
        "status":         auto_state["status"],
        "last_scan":      auto_state["last_scan"],
        "last_check":     auto_state["last_check"],
        "trades_today":   auto_state["trades_today"],
        "max_trades_day": auto_state["max_trades_day"],
        "log":            auto_state["log"][:20],
    })

@app.route("/api/auto/settings", methods=["POST"])
def update_settings():
    b = request.json
    if "max_trades_day" in b:
        auto_state["max_trades_day"] = int(b["max_trades_day"])
    if "scan_interval" in b:
        auto_state["scan_interval"] = int(b["scan_interval"]) * 60
    bot_log(f"Settings updated: max_trades={auto_state['max_trades_day']}", "SYSTEM")
    return jsonify({"success": True})

@app.route("/api/prices")
def prices():
    result = {}
    for name, ticker in {"Gold":"GC=F","BTC":"BTC-USD","ETH":"ETH-USD"}.items():
        try:
            df   = fetch(ticker, "5d")
            cur  = float(df["close"].iloc[-1])
            prev = float(df["close"].iloc[-2])
            chg  = (cur-prev)/prev*100
            result[name] = {"price":round(cur,2),"change":round(chg,2),
                            "high":round(float(df["high"].iloc[-1]),2),
                            "low":round(float(df["low"].iloc[-1]),2)}
        except Exception as e:
            result[name] = {"price":0,"change":0,"error":str(e)}
    return jsonify(result)

@app.route("/api/signals")
def signals():
    try:
        import sys; sys.path.insert(0,"/Users/vinayaka/Desktop/trading-bot")
        from strategy_engine import StrategyOrchestrator
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
            cur=get_live_price(ticker)
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
    trade={**b,"id":str(uuid.uuid4())[:8],"opened_at":datetime.now().strftime("%d %b %Y %H:%M"),"status":"OPEN","mode":"MANUAL"}
    data["open"].append(trade); save_trades(data)
    bot_log(f"MANUAL trade opened: {b.get('direction')} {b.get('asset')}", "TRADE")
    send_tg(f"MANUAL PAPER TRADE\n{trade.get('direction')} {trade.get('asset')}\nEntry: {trade.get('entry')}\nCapital: Rs{trade.get('capital'):,.0f}")
    return jsonify({"success":True,"trade":trade})

@app.route("/api/trade/close/<tid>",methods=["POST"])
def close_trade(tid):
    b=request.json; data=load_trades()
    trade=next((t for t in data["open"] if t["id"]==tid),None)
    if not trade: return jsonify({"success":False})
    ep=b.get("exit_price",trade["entry"])
    pnl_pct=(ep-trade["entry"])/trade["entry"]*100 if trade["direction"]=="BUY" else (trade["entry"]-ep)/trade["entry"]*100
    pnl_abs=pnl_pct/100*trade["capital"]
    closed={**trade,"exit_price":ep,"pnl_pct":round(pnl_pct,2),"pnl_abs":round(pnl_abs,0),
            "closed_at":datetime.now().strftime("%d %b %Y %H:%M"),"exit_reason":b.get("reason","MANUAL"),"status":"CLOSED"}
    data["open"]=[t for t in data["open"] if t["id"]!=tid]
    data["closed"].append(closed)
    total=sum(t.get("pnl_abs",0) for t in data["closed"])
    data["equity"].append({"date":str(datetime.now().date()),"value":round(CAPITAL+total,0)})
    save_trades(data)
    bot_log(f"Trade closed: {closed['direction']} {closed['asset']} | P&L: Rs{pnl_abs:+,.0f}", "TRADE")
    send_tg(f"TRADE CLOSED\n{closed['direction']} {closed['asset']}\nP&L: {'+'if pnl_abs>=0 else ''}Rs{pnl_abs:,.0f}")
    return jsonify({"success":True,"trade":closed})

if __name__ == "__main__":
    # Start background auto-trading thread
    t = threading.Thread(target=auto_trading_loop, daemon=True)
    t.start()
    bot_log("Auto-trading engine started (disabled by default — toggle ON in dashboard)", "SYSTEM")
    print("\n" + "="*55)
    print("  CHAITU AUTONOMOUS TRADING DASHBOARD")
    print("="*55)
    print("  Background engine: RUNNING")
    print("  Auto-trading:      OFF (toggle in dashboard)")
    print("  Open browser:      http://localhost:5000")
    print("="*55 + "\n")
    app.run(debug=False, port=5000, host="0.0.0.0")
