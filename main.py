import os
import time
import yfinance as yf
import pandas as pd
import ta
from telegram import Bot
from flask import Flask
from threading import Thread

# Telegram setup
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
bot = Bot(token=BOT_TOKEN)

# Flask web server (for Render uptime)
app = Flask(__name__)

@app.route('/')
def home():
    return "Forex Bot is running"

def run():
    app.run(host='0.0.0.0', port=8080)

Thread(target=run).start()

# Pairs to track
pairs = ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCAD=X"]

def get_signals():
    for pair in pairs:
        data = yf.download(pair, period="1d", interval="15m")
        if len(data) < 50:
            continue

        data["rsi"] = ta.momentum.RSIIndicator(data["Close"], window=14).rsi()
        data["ema_short"] = ta.trend.EMAIndicator(data["Close"], window=9).ema_indicator()
        data["ema_long"] = ta.trend.EMAIndicator(data["Close"], window=21).ema_indicator()

        last = data.iloc[-1]
        previous = data.iloc[-2]

        # Buy / Sell logic
        if previous["ema_short"] < previous["ema_long"] and last["ema_short"] > last["ema_long"] and last["rsi"] < 70:
            bot.send_message(chat_id=CHAT_ID, text=f"📈 BUY signal for {pair}")
        elif previous["ema_short"] > previous["ema_long"] and last["ema_short"] < last["ema_long"] and last["rsi"] > 30:
            bot.send_message(chat_id=CHAT_ID, text=f"📉 SELL signal for {pair}")

while True:
    get_signals()
    time.sleep(900)  # every 15 minutes
