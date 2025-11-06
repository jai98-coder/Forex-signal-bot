import os
import pandas as pd
import numpy as np
import yfinance as yf
import requests
from flask import Flask
from threading import Thread
from keep_alive import keep_alive

# --- Technical indicator functions ---

def ema(series, length=14):
    return series.ewm(span=length, adjust=False).mean()

def rsi(series, length=14):
    delta = series.diff()
    up = np.where(delta > 0, delta, 0)
    down = np.where(delta < 0, -delta, 0)
    roll_up = pd.Series(up, index=series.index).rolling(length).mean()
    roll_down = pd.Series(down, index=series.index).rolling(length).mean()
    rs = roll_up / roll_down.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)

def macd(series, fast=12, slow=26, signal=9):
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line, macd_line - signal_line

def atr(high, low, close, length=14):
    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(length).mean()

def last_cross_up(a, b):
    return len(a) > 1 and a.iloc[-2] < b.iloc[-2] and a.iloc[-1] > b.iloc[-1]

def last_cross_down(a, b):
    return len(a) > 1 and a.iloc[-2] > b.iloc[-2] and a.iloc[-1] < b.iloc[-1]


# --- Signal generation ---

def generate_signal(df, pair):
    close, high, low = df["close"], df["high"], df["low"]

    # Indicators
    df["EMA50"] = ema(close, 50)
    df["EMA200"] = ema(close, 200)
    df["RSI21"] = rsi(close, 21)
    macd_line, sig_line, _ = macd(close)
    df["MACD"], df["MACDs"] = macd_line, sig_line
    df["ATR14"] = atr(high, low, close, 14)

    last = df.iloc[-2]
    curr = df.iloc[-1]
    price = last["close"]
    ema200 = last["EMA200"]
    rsi21 = last["RSI21"]
    atr14 = last["ATR14"]

    action = "HOLD"
    reason = []

    if atr14 < df["ATR14"].rolling(50).mean().iloc[-1] * 0.8:
        reason.append("Low volatility (ATR below average)")
        return {"pair": pair, "action": "HOLD", "entry": price, "stop_loss": 0, "take_profit": 0, "time": df.index[-2].isoformat(), "reason": reason}

    # Buy / Sell setups
    if last_cross_up(df["MACD"], df["MACDs"]) and rsi21 < 30 and price > ema200:
        action = "BUY"
        reason.append(f"RSI21={rsi21:.1f}<30, MACD crossed up, above EMA200")
    elif last_cross_down(df["MACD"], df["MACDs"]) and rsi21 > 70 and price < ema200:
        action = "SELL"
        reason.append(f"RSI21={rsi21:.1f}>70, MACD crossed down, below EMA200")
    else:
        reason.append("No qualified setup")

    ATR_MULT_SL = 1.5
    RR = 2
    sl_dist = ATR_MULT_SL * atr14

    if action == "BUY":
        sl, tp = price - sl_dist, price + RR * sl_dist
    elif action == "SELL":
        sl, tp = price + sl_dist, price - RR * sl_dist
    else:
        sl, tp = 0, 0

    return {
        "pair": pair,
        "action": action,
        "entry": round(price, 6),
        "stop_loss": round(sl, 6),
        "take_profit": round(tp, 6),
        "time": df.index[-2].isoformat(),
        "reason": reason
    }


# --- Telegram Bot setup ---

keep_alive()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

def send_signal_to_telegram(signal):
    message = (
        f"📊 Pair: {signal['pair']}\n"
        f"Action: {signal['action']}\n"
        f"Entry: {signal['entry']}\n"
        f"Stop Loss: {signal['stop_loss']}\n"
        f"Take Profit: {signal['take_profit']}\n"
        f"Reason: {', '.join(signal['reason'])}"
    )

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": message}
    requests.post(url, data=data)


# --- Live Data and Execution ---

# Fetch latest EUR/USD hourly data
data = yf.download("EURUSD=X", period="7d", interval="1h")
data.rename(columns={"Open":"open","High":"high","Low":"low","Close":"close","Volume":"volume"}, inplace=True)
df = data.reset_index(drop=True)

# Generate and send signal
signal = generate_signal(df, "EURUSD")
send_signal_to_telegram(signal)
