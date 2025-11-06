import os, time, json, math, requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from dateutil import tz
from keep_alive import keep_alive
keep_alive()
# ================== CONFIG ==================
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "d5a7c81a30c04fa0b3ad90c6ffc3ad08")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("CHAT_ID")

PAIRS = ["EUR/USD", "GBP/USD", "USD/JPY"]  # Pairs to analyze
INTERVAL = "1h"  # 1-hour candles
SLEEP_SECONDS = 1200  # check every 20 min (safe for free API)
CACHE_FILE = "fx_signal_cache.json"
TZ_DISPLAY = "Europe/London"

ATR_MULT_SL = 1.5
RR = 2.0
# ============================================
INTERVAL = "1h"      # 1-hour candles are reliable
SLEEP_SECONDS = 1200 # 20 minutes between checks
PAIRS = ["EUR/USD", "GBP/USD", "USD/JPY"]

def fetch_twelve_data(pair: str, interval="1h", outputsize=100):
    """Fetch recent forex candles from Twelve Data."""
    base, quote = pair.replace(" ", "").split("/")
    url = (
        "https://api.twelvedata.com/time_series"
        f"?symbol={base}/{quote}&interval={interval}"
        f"&apikey={TWELVE_DATA_API_KEY}&outputsize={outputsize}"
    )
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    data = r.json()
    if "values" not in data:
        raise RuntimeError(f"Twelve Data error for {pair}: {data.get('message','unknown')}")
    df = pd.DataFrame(data["values"])
    df = df.rename(columns={"open": "open", "high": "high", "low": "low", "close": "close"})
    df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]].astype(float)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.sort_values("datetime").set_index("datetime")
    return df


def ema(series, length): return series.ewm(span=length, adjust=False).mean()

def rsi(series, length=14):
    delta = series.diff()
    up = np.where(delta > 0, delta, 0.0)
    down = np.where(delta < 0, -delta, 0.0)
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

def last_cross_up(a, b):  return len(a)>1 and a.iloc[-2]<=b.iloc[-2] and a.iloc[-1]>b.iloc[-1]
def last_cross_down(a, b):return len(a)>1 and a.iloc[-2]>=b.iloc[-2] and a.iloc[-1]<b.iloc[-1]


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
    curr = df.iloc[-1]  # most recent closed candle
    price = last["close"]
    ema200 = last["EMA200"]
    rsi21 = last["RSI21"]
    atr14 = last["ATR14"]
    crossed_up, crossed_down = last_cross_up(df["MACD"], df["MACDs"]), last_cross_down(df["MACD"], df["MACDs"])

    trend_long, trend_short = price > ema200, price < ema200
    action, reason = "HOLD", []

    # Skip if volatility too low
    if atr14 < df["ATR14"].rolling(50).mean().iloc[-1] * 0.6:
        reason.append("Low volatility (ATR below average)")
        return {"pair": pair, "action": "HOLD", "entry": price, "stop_loss": None, "take_profit": None,
                "time": df.index[-2].isoformat(), "reason": "; ".join(reason)}

    # Volume filter (only if column exists)
    vol_pass = True
    if "volume" in df.columns:
        vol_pass = df["volume"].iloc[-2] > df["volume"].rolling(20).mean().iloc[-2]
        if not vol_pass:
            reason.append("Volume below 20-period average")

    # BUY setup
    if trend_long and rsi21 < 30 and crossed_up and curr["close"] > last["close"] and vol_pass:
        action = "BUY"
        reason.append(f"RSI21 {rsi21:.1f}<30, MACD cross up, price > EMA200, confirming bullish candle")
    # SELL setup
    elif trend_short and rsi21 > 70 and crossed_down and curr["close"] < last["close"] and vol_pass:
        action = "SELL"
        reason.append(f"RSI21 {rsi21:.1f}>70, MACD cross down, price < EMA200, confirming bearish candle")
    else:
        reason.append(f"No qualified setup (RSI={rsi21:.1f})")

    if action == "HOLD":
        return {"pair": pair, "action": "HOLD", "entry": price, "stop_loss": None, "take_profit": None,
                "time": df.index[-2].isoformat(), "reason": "; ".join(reason)}

    sl_dist = ATR_MULT_SL * atr14
    if action == "BUY":
        sl, tp = price - sl_dist, price + RR * sl_dist
    else:
        sl, tp = price + sl_dist, price - RR * sl_dist

    return {"pair": pair, "action": action, "entry": round(price,6),
            "stop_loss": round(sl,6), "take_profit": round(tp,6),
            "time": df.index[-2].isoformat(), "reason": "; ".join(reason)}