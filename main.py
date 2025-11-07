import os
import time
import threading
import warnings
import pandas as pd
import numpy as np
import yfinance as yf
import requests
from flask import Flask

# Hide unnecessary warnings
warnings.filterwarnings("ignore")

# === Telegram Bot Credentials from Environment ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# === Currency Pairs (without XAUUSD) ===
symbols = ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X"]

# === Helper: Send message to Telegram ===
def send_telegram_message(message: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message}
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print(f"Telegram Error: {e}")

# === Technical Indicators ===
def calculate_ema(series, period=200):
    return series.ewm(span=period, adjust=False).mean()

def calculate_rsi(series, period=21):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -1 * delta.clip(upper=0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# === Strategy Logic ===
def generate_signal(df):
    df["EMA200"] = calculate_ema(df["Close"], 200)
    df["RSI21"] = calculate_rsi(df["Close"], 21)
    last = df.iloc[-1]

    price = float(last["Close"])
    ema200 = float(last["EMA200"])
    rsi21 = float(last["RSI21"])

    # --- Buy signal conditions ---
    if price > ema200 and rsi21 < 70:
        sl = round(price - (price * 0.002), 5)
        tp = round(price + (price * 0.004), 5)
        return f"📊 {df.name} Signal: BUY\nEntry: {price}\nTP: {tp}\nSL: {sl}"

    # --- Sell signal conditions ---
    elif price < ema200 and rsi21 > 30:
        sl = round(price + (price * 0.002), 5)
        tp = round(price - (price * 0.004), 5)
        return f"📊 {df.name} Signal: SELL\nEntry: {price}\nTP: {tp}\nSL: {sl}"

    return None

# === Signal Checker ===
last_signals = {}

def check_signals():
    for symbol in symbols:
        try:
            df = yf.download(symbol, period="7d", interval="1h", progress=False)
            df.name = symbol
            if len(df) < 50:
                continue

            signal = generate_signal(df)
            if signal and last_signals.get(symbol) != signal:
                send_telegram_message(signal)
                last_signals[symbol] = signal
                print(f"Sent signal for {symbol}")
            else:
                print(f"No new signal for {symbol}")

        except Exception as e:
            print(f"Error fetching {symbol}: {e}")

# === Run the bot on a loop ===
def run_bot():
    send_telegram_message("🤖 Forex Bot started on Render ✅")
    while True:
        check_signals()
        time.sleep(3600)  # Run every 1 hour

# === Flask server for Render uptime ===
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running successfully!"

# === Start thread and Flask ===
if __name__ == "__main__":
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.daemon = True
    bot_thread.start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
