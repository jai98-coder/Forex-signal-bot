import os
import time
import threading
import pandas as pd
import numpy as np
import yfinance as yf
import requests
from flask import Flask

# ------------------ Telegram Setup ------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

def send_telegram_message(message):
    """Send a message to the Telegram chat."""
    if not BOT_TOKEN or not CHAT_ID:
        print("⚠️ Telegram not configured properly (BOT_TOKEN or CHAT_ID missing)")
        return
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": message}
        requests.post(url, json=payload)
    except Exception as e:
        print(f"❌ Telegram send failed: {e}")

# ------------------ Indicators ------------------
def EMA(series, period=200):
    return series.ewm(span=period, adjust=False).mean()

def RSI(series, period=21):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def ATR(df, period=14):
    high_low = df["High"] - df["Low"]
    high_close = np.abs(df["High"] - df["Close"].shift())
    low_close = np.abs(df["Low"] - df["Close"].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    return true_range.rolling(window=period).mean()

# ------------------ Signal Generation ------------------
def generate_signal(df):
    """Generate buy/sell signal for given forex data."""
    if df is None or df.empty:
        return None

    df["EMA200"] = EMA(df["Close"], 200)
    df["RSI21"] = RSI(df["Close"], 21)
    df["ATR14"] = ATR(df, 14)
    last = df.iloc[-1]

    price = float(last["Close"])
    ema200 = float(last["EMA200"])
    rsi21 = float(last["RSI21"])

    if price > ema200 and rsi21 < 70:
        return "BUY"
    elif price < ema200 and rsi21 > 30:
        return "SELL"
    else:
        return None

# ------------------ Data Fetching ------------------
def fetch_data(symbol):
    """Fetch forex data from Yahoo Finance safely."""
    try:
        data = yf.download(symbol, period="7d", interval="1h")
        if data is None or data.empty:
            print(f"⚠️ No data for {symbol}")
            return None
        return data
    except Exception as e:
        print(f"❌ Error fetching {symbol}: {e}")
        return None

# ------------------ Forex Pairs ------------------
PAIRS = [
    "EURUSD=X",
    "GBPUSD=X",
    "USDJPY=X",
    "AUDUSD=X",
    "GC=F"  # Gold futures (reliable on Yahoo)
]

# ------------------ Bot Logic ------------------
def check_signals():
    print("🔄 Checking forex signals...")
    for pair in PAIRS:
        try:
            df = fetch_data(pair)
            if df is None or df.empty:
                print(f"⚠️ No data for {pair}, skipping.")
                continue

            signal = generate_signal(df)
            if signal:
                message = f"📊 {pair.replace('=X', '')} Signal: {signal}"
                print(message)
                send_telegram_message(message)
            else:
                print(f"{pair.replace('=X', '')} — No signal.")
        except Exception as e:
            print(f"❌ Error processing {pair}: {e}")
    print("✅ Done checking signals.\n")

def run_bot():
    send_telegram_message("🤖 Forex Bot started on Render ✅")
    while True:
        check_signals()
        time.sleep(600)  # every 10 minutes

# ------------------ Flask App (keeps Render alive) ------------------
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Forex Signal Bot is running."

# ------------------ Start Everything ------------------
if __name__ == "__main__":
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.daemon = True
    bot_thread.start()
    app.run(host="0.0.0.0", port=10000)
