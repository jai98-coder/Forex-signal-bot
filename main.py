import os
import time
import threading
from datetime import datetime, timezone

import requests
import pandas as pd
import numpy as np
import yfinance as yf
from flask import Flask

# =======================================================
# ENVIRONMENT VARIABLES
# =======================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
SCAN_EVERY_MIN = int(os.getenv("SCAN_EVERY_MIN", "60"))  # once every hour
PAIRS_ENV = os.getenv("PAIRS", "EURUSD=X,GBPUSD=X,USDJPY=X,AUDUSD=X,XAUUSD=X")
PAIRS = [p.strip().upper() for p in PAIRS_ENV.split(",") if p.strip()]

# =======================================================
# TELEGRAM MESSAGE FUNCTION
# =======================================================
def tg_send(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        print("❌ Missing BOT_TOKEN or CHAT_ID")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=15
        )
    except Exception as e:
        print("Telegram send error:", e)

# =======================================================
# INDICATORS
# =======================================================
def rsi(series: pd.Series, length=21):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/length, min_periods=length).mean()
    avg_loss = loss.ewm(alpha=1/length, min_periods=length).mean()
    rs = avg_gain / (avg_loss + 1e-12)
    return 100 - (100 / (1 + rs))

def ema(series, length=200):
    return series.ewm(span=length, adjust=False).mean()

def atr(df, length=14):
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/length, adjust=False).mean()

# =======================================================
# FETCH DATA
# =======================================================
def fetch_data(symbol):
    try:
        df = yf.download(symbol, period="7d", interval="1h", progress=False)
        df.dropna(inplace=True)
        return df
    except Exception as e:
        print(f"[{symbol}] Error fetching data:", e)
        return None

# =======================================================
# STRATEGY
# =======================================================
def compute_indicators(df):
    df["EMA200"] = ema(df["Close"], 200)
    df["RSI21"] = rsi(df["Close"], 21)
    df["ATR14"] = atr(df, 14)
    df["Body"] = (df["Close"] - df["Open"]).abs()
    df["BodyMean20"] = df["Body"].rolling(20).mean()
    df["High20"] = df["High"].rolling(20).max()
    df["Low20"] = df["Low"].rolling(20).min()
    return df.dropna()

def generate_signal(df, symbol):
    df = compute_indicators(df)
    if len(df) < 220:
        return None

    last = df.iloc[-1]
    prev = df.iloc[-2]

    price, ema200, r, a = last["Close"], last["EMA200"], last["RSI21"], last["ATR14"]

    # Skip if volatility too low
    if a <= 0 or (a / price) < 0.00005:
        return None

    # Stronger filters to reduce noise
    body_ok = last["Body"] > (last["BodyMean20"] * 1.3)
    bull_break = price > prev["High"] and price > last["High20"] * 0.999
    bear_break = price < prev["Low"] and price < last["Low20"] * 1.001

    buy_bias = price > ema200 and r >= 58
    sell_bias = price < ema200 and r <= 42
    buy_momo = body_ok and (last["Close"] > last["Open"]) and bull_break
    sell_momo = body_ok and (last["Close"] < last["Open"]) and bear_break

    side, score = None, 0
    if buy_bias and buy_momo:
        side = "BUY"
        score = (r - 50) + 1000 * (price - ema200) / a
    elif sell_bias and sell_momo:
        side = "SELL"
        score = (50 - r) + 1000 * (ema200 - price) / a

    # Filter weak signals
    if not side or abs(score) < 25:  # Higher = stricter filter
        return None

    sl_mult, tp_mult = 1.8, 3.2
    if side == "BUY":
        sl = round(price - sl_mult * a, 6)
        tp = round(price + tp_mult * a, 6)
    else:
        sl = round(price + sl_mult * a, 6)
        tp = round(price - tp_mult * a, 6)

    return {
        "symbol": symbol,
        "side": side,
        "entry": round(price, 6),
        "sl": sl,
        "tp": tp,
        "rsi": round(r, 2),
        "ema200": round(ema200, 6),
        "atr": round(a, 6),
        "score": round(score, 2),
        "time": df.index[-1].isoformat(),
    }

# =======================================================
# SCANNER
# =======================================================
_last_alert = {"symbol": None, "side": None, "time": None}

def scan_best():
    results = []
    for sym in PAIRS:
        df = fetch_data(sym)
        if df is None:
            continue
        sig = generate_signal(df, sym)
        if sig:
            results.append(sig)
    if not results:
        return None

    best = max(results, key=lambda s: s["score"])
    if (
        _last_alert["symbol"] == best["symbol"]
        and _last_alert["side"] == best["side"]
        and _last_alert["time"] == best["time"]
    ):
        return None

    _last_alert.update(best)
    return best

def format_signal(sig):
    t = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"📊 <b>{sig['symbol']} Signal</b>\n"
        f"Time: <code>{t}</code>\n\n"
        f"Action: <b>{sig['side']}</b>\n"
        f"Entry: <code>{sig['entry']}</code>\n"
        f"Stop Loss: <code>{sig['sl']}</code>\n"
        f"Take Profit: <code>{sig['tp']}</code>\n\n"
        f"RSI(21): <code>{sig['rsi']}</code>\n"
        f"EMA200: <code>{sig['ema200']}</code>\n"
        f"ATR(14): <code>{sig['atr']}</code>\n"
        f"Score: <code>{sig['score']}</code>\n"
    )

# =======================================================
# BOT LOOP
# =======================================================
def run_bot():
    tg_send("🤖 Forex Bot started successfully on Render ✅")
    while True:
        try:
            best = scan_best()
            if best:
                tg_send(format_signal(best))
            else:
                print("No strong signal found.")
        except Exception as e:
            print("Error:", e)
        time.sleep(SCAN_EVERY_MIN * 60)

# =======================================================
# FLASK KEEP-ALIVE
# =======================================================
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running", 200

def main():
    threading.Thread(target=run_bot, daemon=True).start()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()
