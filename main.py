import os
import time
import requests
import yfinance as yf
import pandas as pd
from flask import Flask
from threading import Thread

# === Telegram Setup ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# === Forex pairs to track ===
PAIRS = ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "XAUUSD=X", "AUDUSD=X"]

# === Indicators ===
def rsi(series, period=14):
    delta = series.diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def generate_signal(df):
    df["EMA200"] = df["Close"].ewm(span=200, adjust=False).mean()
    df["RSI21"] = rsi(df["Close"], 21)
    last = df.iloc[-1]
    price = float(last["Close"])
    ema200 = float(last["EMA200"])
    rsi21 = float(last["RSI21"])

    # Basic buy/sell logic
    if price > ema200 and rsi21 < 30:
        return "BUY"
    elif price < ema200 and rsi21 > 70:
        return "SELL"
    else:
        return None

def fetch_data(symbol):
    data = yf.download(symbol, period="7d", interval="1h")
    return data

def send_telegram_message(msg):
    if BOT_TOKEN and CHAT_ID:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": msg}
        try:
            requests.post(url, json=payload)
        except Exception as e:
            print("Error sending message:", e)

def check_signals():
    print("🔄 Checking forex signals...")
    for pair in PAIRS:
        df = fetch_data(pair)
        signal = generate_signal(df)
        if signal:
            message = f"📊 {pair.replace('=X','')} Signal: {signal}"
            print(message)
            send_telegram_message(message)
        else:
            print(f"{pair.replace('=X','')} — No signal.")
    print("✅ Done checking signals.\n")

# === Scheduler loop ===
def run_bot():
    send_telegram_message("🤖 Bot started successfully on Render!")
    while True:
        check_signals()
        time.sleep(600)  # every 10 minutes

# === Flask app to keep Render alive ===
app = Flask(__name__)

@app.route('/')
def home():
    return "✅ Forex Signal Bot is running."

# Run Flask in a separate thread so it stays alive
if __name__ == '__main__':
    Thread(target=run_bot).start()
    app.run(host='0.0.0.0', port=10000)
