import os
import time
import requests
import pandas as pd
import yfinance as yf
import ta

# === CONFIG ===
PAIRS = ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCAD=X"]
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
CHECK_INTERVAL = 600  # 10 minutes (in seconds)

# === TELEGRAM ===
def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        print("❌ Missing BOT_TOKEN or CHAT_ID")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": CHAT_ID, "text": message})

# === STRATEGY ===
def get_signal(symbol):
    data = yf.download(symbol, period="7d", interval="1h")
    if data.empty:
        return None

    data["EMA200"] = data["Close"].ewm(span=200).mean()
    data["RSI21"] = ta.momentum.RSIIndicator(data["Close"], window=21).rsi()
    data["ATR14"] = ta.volatility.AverageTrueRange(data["High"], data["Low"], data["Close"], window=14).average_true_range()

    last = data.iloc[-1]
    price = float(last["Close"])
    ema200 = float(last["EMA200"])
    rsi21 = float(last["RSI21"])

    # --- Simple logic ---
    if price > ema200 and rsi21 < 30:
        return f"📈 BUY signal for {symbol.replace('=X', '')}"
    elif price < ema200 and rsi21 > 70:
        return f"📉 SELL signal for {symbol.replace('=X', '')}"
    return None

# === MAIN LOOP ===
print("🚀 Forex Signal Bot started...")
send_telegram("🤖 Bot started successfully on Render!")

while True:
    for pair in PAIRS:
        try:
            signal = get_signal(pair)
            if signal:
                print(signal)
                send_telegram(signal)
            else:
                print(f"{pair}: No trade signal.")
        except Exception as e:
            print(f"Error processing {pair}: {e}")
    print(f"Waiting {CHECK_INTERVAL / 60:.0f} minutes...")
    time.sleep(CHECK_INTERVAL)
