import os
import time
import pandas as pd
import yfinance as yf
import ta
from flask import Flask
from telegram import Bot

# ======================
# Telegram Bot Setup
# ======================
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

bot = Bot(token=TOKEN)

app = Flask(__name__)

# ======================
# Forex Signal Function
# ======================
def get_signals():
    pair = "EURUSD=X"
    data = yf.download(pair, period="1d", interval="15m")

    if data.empty:
        print("No data retrieved.")
        return

    # Ensure 1D series to avoid ValueError
    close = data["Close"].squeeze()

    # Technical Indicators
    data["ema_short"] = ta.trend.EMAIndicator(close, window=9).ema_indicator()
    data["ema_long"] = ta.trend.EMAIndicator(close, window=21).ema_indicator()
    data["rsi"] = ta.momentum.RSIIndicator(close, window=14).rsi()

    # Latest values
    ema_short = data["ema_short"].iloc[-1]
    ema_long = data["ema_long"].iloc[-1]
    rsi = data["rsi"].iloc[-1]
    price = close.iloc[-1]

    # Trading logic
    if ema_short > ema_long and rsi < 70:
        signal = f"📈 BUY Signal for {pair}\nPrice: {price:.5f}\nRSI: {rsi:.2f}"
    elif ema_short < ema_long and rsi > 30:
        signal = f"📉 SELL Signal for {pair}\nPrice: {price:.5f}\nRSI: {rsi:.2f}"
    else:
        signal = f"⏸ No clear signal for {pair}\nPrice: {price:.5f}\nRSI: {rsi:.2f}"

    print(signal)
    bot.send_message(chat_id=CHAT_ID, text=signal)

# ======================
# Flask Web Server (Keep alive for Render)
# ======================
@app.route("/")
def home():
    return "Bot is running!"

if __name__ == "__main__":
    print("Starting Forex Signal Bot...")
    while True:
        try:
            get_signals()
        except Exception as e:
            print(f"Error: {e}")
        time.sleep(900)  # every 15 minutes
