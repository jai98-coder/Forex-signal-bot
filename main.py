import pandas as pd
import numpy as np
import yfinance as yf
from flask import Flask
import time

app = Flask(__name__)

# === Helper functions ===

def ema(series, length):
    return series.ewm(span=length, adjust=False).mean()

def rsi(series, length=14):
    delta = series.diff()
    up = delta.clip(lower=0)
    down = -1 * delta.clip(upper=0)
    roll_up = pd.Series(up.squeeze(), index=series.index).rolling(length).mean()
    roll_down = pd.Series(down.squeeze(), index=series.index).rolling(length).mean()
    rs = roll_up / roll_down
    return 100 - (100 / (1 + rs))

def macd(series, fast=12, slow=26, signal=9):
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line, macd_line - signal_line

def atr(high, low, close, length=14):
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(length).mean()

def last_cross_up(a, b):
    return len(a) > 1 and a.iloc[-2] < b.iloc[-2] and a.iloc[-1] > b.iloc[-1]

def last_cross_down(a, b):
    return len(a) > 1 and a.iloc[-2] > b.iloc[-2] and a.iloc[-1] < b.iloc[-1]

# === Signal generation ===

def generate_signal(df, pair):
    # Ensure 1D data
    close = df["Close"].squeeze()
    high = df["High"].squeeze()
    low = df["Low"].squeeze()

    # Indicators
    df["EMA50"] = ema(close, 50)
    df["EMA200"] = ema(close, 200)
    df["RSI21"] = rsi(close, 21)
    macd_line, sig_line, _ = macd(close)
    df["MACD"] = macd_line
    df["MACDs"] = sig_line
    df["ATR14"] = atr(high, low, close, 14)

    last = df.iloc[-2]
    curr = df.iloc[-1]
    price = last["Close"]
    ema200 = last["EMA200"]
    rsi21 = last["RSI21"]
    atr14 = last["ATR14"]

    action = "HOLD"
    reason = []

    # Basic logic
    if last_cross_up(df["MACD"], df["MACDs"]) and rsi21 < 70 and price > ema200:
        action = "BUY"
        reason.append("MACD bullish crossover & RSI < 70 & price above EMA200")

    elif last_cross_down(df["MACD"], df["MACDs"]) and rsi21 > 30 and price < ema200:
        action = "SELL"
        reason.append("MACD bearish crossover & RSI > 30 & price below EMA200")

    # Volatility filter
    if atr14 < df["ATR14"].rolling(50).mean().iloc[-1] * 0.8:
        action = "HOLD"
        reason.append("Low volatility filter (ATR)")

    # Return signal
    return {
        "pair": pair,
        "action": action,
        "entry": round(float(price), 5),
        "stop_loss": 0,
        "take_profit": 0,
        "time": str(df.index[-2]),  # Safe string version
        "reason": reason
    }

# === Flask app (keeps Render alive) ===

@app.route("/")
def home():
    return "Forex Signal Bot is running ✅"

if __name__ == "__main__":
    while True:
        print("Fetching data...")
        data = yf.download("EURUSD=X", period="7d", interval="1h")
        data.index = pd.to_datetime(data.index)  # Fix isoformat error
        signal = generate_signal(data, "EURUSD")
        print("Signal:", signal)
        time.sleep(3600)  # every 1 hour
