import os
import time
import pandas as pd
import yfinance as yf
import ta
from flask import Flask
from telegram import Bot

# ======================
# TELEGRAM CONFIG
# ======================
TOKEN = os.getenv("BOT_TOKEN")  # Must match Render Environment Variable
CHAT_ID = os.getenv("CHAT_ID")

bot = Bot(token=TOKEN)
app = Flask(__name__)

# ======================
# SETTINGS
# ======================
PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD"]
TIMEFRAME = "15m"
PERIOD = "1d"

# ======================
# SIGNAL LOGIC
# ======================
def get_signals():
    for pair in PAIRS:
        symbol = pair + "=X"  # yfinance requires this format for Forex

        data = yf.download(symbol, period=PERIOD, interval=TIMEFRAME)

        if data.empty:
            print(f"⚠️ No data found for {pair}")
            continue

        close = data["Close"].squeeze()

        # Indicators
        data["ema_short"] = ta.trend.EMAIndicator(close, window=9).ema_indicator()
        data["ema_long"] = ta.trend.EMAIndicator(close, window=21).ema_indicator()
        data["rsi"] = ta.momentum.RSIIndicator(close, window=14).rsi()

        ema_short = data["ema_short"].iloc[-1]
        ema_long = data["ema_long"].iloc[-1]
        rsi = data["rsi"].iloc[-1]
        price = close.iloc[-1]

        # SIGNAL LOGIC
        signal = None
        sl = None
        tp = None

        if ema_short > ema_long and rsi < 70:
            signal = "📈 BUY"
            sl = price * 0.995  # 0.5% Stop Loss
            tp = price * 1.010  # 1% Take Profit
        elif ema_short < ema_long and rsi > 30:
            signal = "📉 SELL"
            sl = price * 1.005
            tp = price * 0.990
        else:
            signal = "⏸ No clear signal"

        # Format message
        message = (
            f"{signal} Signal for {pair}\n"
            f"Price: {price:.5f}\n"
            f"RSI: {rsi:.2f}\n"
        )

        if "BUY" in signal or "SELL" in signal:
            message += f"🎯 TP: {tp:.5f}\n🛑 SL: {sl:.5f}"

        print(message)

        # Send to Telegram
        try:
            bot.send_message(chat_id=CHAT_ID, text=message)
        except Exception as e:
            print(f"Telegram Error for {pair}: {e}")

# ======================
# FLASK KEEP-ALIVE
# ======================
@app.route("/")
def home():
    return "✅ Forex Signal Bot is running successfully!"

# ======================
# MAIN LOOP
# ======================
if __name__ == "__main__":
    print("🚀 Starting Multi-Pair Forex Signal Bot...")
    while True:
        try:
            get_signals()
        except Exception as e:
            print(f"⚠️ Error: {e}")
        time.sleep(900)  # 15 minutes
