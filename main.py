import pandas as pd
import numpy as np
import time
import requests
from flask import Flask
import os

app = Flask(__name__)

# =========================
# 🔧 Environment variables
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY")

# =========================
# 💬 Telegram
# =========================
def send_telegram_message(text):
    if BOT_TOKEN and CHAT_ID:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": text}
        try:
            requests.post(url, json=payload, timeout=10)
        except Exception as e:
            print(f"⚠️ Telegram send failed: {e}")
    else:
        print("⚠️ Missing BOT_TOKEN or CHAT_ID")

# =========================
# 📈 Technical Indicators
# =========================
def rsi(series, length=14):
    delta = series.diff()
    gain = np.where(delta > 0, delta, 0)
    loss = np.where(delta < 0, -delta, 0)
    avg_gain = pd.Series(gain).rolling(length).mean()
    avg_loss = pd.Series(loss).rolling(length).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def atr(df, length=14):
    high_low = df["high"] - df["low"]
    high_close = np.abs(df["high"] - df["close"].shift())
    low_close = np.abs(df["low"] - df["close"].shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(length).mean()

# =========================
# ⚙️ Signal Generation
# =========================
def generate_signal(df, pair):
    df["EMA200"] = df["close"].ewm(span=200, adjust=False).mean()
    df["RSI21"] = rsi(df["close"], 21)
    df["ATR14"] = atr(df, 14)

    last = df.iloc[-1]
    price = float(last["close"])
    ema200 = float(last["EMA200"])
    rsi21 = float(last["RSI21"])
    atr14 = float(last["ATR14"])

    if price > ema200 and rsi21 > 55:
        action = "BUY"
        reason = "Price above EMA200 and RSI > 55"
    elif price < ema200 and rsi21 < 45:
        action = "SELL"
        reason = "Price below EMA200 and RSI < 45"
    else:
        action = "HOLD"
        reason = "No clear momentum"

    signal = {
        "pair": pair,
        "action": action,
        "entry": round(price, 5),
        "stop_loss": round(price - (atr14 * 2), 5) if action == "BUY" else round(price + (atr14 * 2), 5),
        "take_profit": round(price + (atr14 * 3), 5) if action == "BUY" else round(price - (atr14 * 3), 5),
        "time": str(df.index[-1]),
        "reason": reason
    }

    print(signal)

    if action in ["BUY", "SELL"]:
        msg = (
            f"📊 Forex Signal\n"
            f"Pair: {signal['pair']}\n"
            f"Action: {signal['action']}\n"
            f"Entry: {signal['entry']}\n"
            f"Stop Loss: {signal['stop_loss']}\n"
            f"Take Profit: {signal['take_profit']}\n"
            f"Reason: {signal['reason']}"
        )
        send_telegram_message(msg)

    return signal

# =========================
# 🔄 Fetch from Twelve Data
# =========================
def fetch_data(symbol="EUR/USD", interval="1h"):
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": 300,
        "apikey": TWELVE_DATA_API_KEY,
    }
    try:
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        if "values" in data:
            df = pd.DataFrame(data["values"])
            df = df.rename(columns={"datetime": "time"})
            df["time"] = pd.to_datetime(df["time"])
            df = df.sort_values("time")
            df = df.astype(float, errors="ignore")
            df.set_index("time", inplace=True)
            return df
        else:
            print(f"⚠️ Error fetching {symbol}: {data}")
            return pd.DataFrame()
    except Exception as e:
        print(f"⚠️ API error for {symbol}: {e}")
        return pd.DataFrame()

@app.route("/")
def home():
    return "✅ Multi-Pair Forex Signal Bot (Twelve Data API connected)"

# =========================
# 🚀 Main Loop
# =========================
if __name__ == "__main__":
    pairs = ["EUR/USD", "GBP/USD", "USD/JPY", "XAU/USD"]
    print("Starting Forex Signal Bot...")

    while True:
        for pair in pairs:
            print(f"\nFetching data for {pair}...")
            df = fetch_data(pair, "1h")
            if not df.empty:
                generate_signal(df, pair)
            else:
                print(f"⚠️ No data fetched for {pair}.")
            time.sleep(5)  # small pause between requests

        print("\n✅ Cycle complete — sleeping for 10 minutes...\n")
        time.sleep(600)
