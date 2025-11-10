import os
import logging
from datetime import datetime
import requests
import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask

# ============ ENV ============
TD_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# Add all pairs here — bot will only send signals when conditions are *really good*
PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "EURCAD", "GBPAUD"]

INTERVAL = "30min"
SCAN_EVERY_S = 30 * 60  # every 30 minutes

# Indicator parameters
EMA_FAST = 9
EMA_SLOW = 21
RSI_LEN = 14
ATR_LEN = 14

# Signal conditions
RSI_BUY_MIN = 55.0
RSI_SELL_MAX = 45.0

# Risk management (wider for volatile pairs)
ATR_MULT_SL = 2.0   # was 1.5
TP_R_MULT = 2.0     # same, 2× reward per risk

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

last_signal_dir = {}

# ============ HELPERS ============
def td_symbol(pair):
    pair = pair.upper()
    if len(pair) == 6:
        return f"{pair[:3]}/{pair[3:]}"
    return pair

def fetch_data(pair):
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": td_symbol(pair),
        "interval": INTERVAL,
        "apikey": TD_API_KEY,
        "outputsize": 200,
        "order": "asc",
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    if "values" not in data:
        raise ValueError(f"TwelveData error: {data}")
    df = pd.DataFrame(data["values"])
    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.sort_values("datetime").reset_index(drop=True)
    return df

def ema(series, length):
    return series.ewm(span=length, adjust=False).mean()

def rsi(series, length=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(length).mean()
    avg_loss = loss.rolling(length).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def atr(df, length=14):
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(length).mean()

def cross_above(f_now, s_now, f_prev, s_prev):
    return f_prev <= s_prev and f_now > s_now

def cross_below(f_now, s_now, f_prev, s_prev):
    return f_prev >= s_prev and f_now < s_now

def send_telegram(text):
    if not BOT_TOKEN or not CHAT_ID:
        log.error("Missing Telegram vars")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        requests.post(url, json=data, timeout=10)
    except Exception as e:
        log.error("Telegram send failed: %s", e)

def check_signal(pair):
    df = fetch_data(pair)
    close = df["close"]
    ema_fast = ema(close, EMA_FAST)
    ema_slow = ema(close, EMA_SLOW)
    rsi_val = rsi(close, RSI_LEN)
    atr_val = atr(df, ATR_LEN)

    ema_f_n, ema_f_p = ema_fast.iloc[-1], ema_fast.iloc[-2]
    ema_s_n, ema_s_p = ema_slow.iloc[-1], ema_slow.iloc[-2]
    price = close.iloc[-1]
    rsi_n = rsi_val.iloc[-1]
    atr_n = atr_val.iloc[-1]

    buy = cross_above(ema_f_n, ema_s_n, ema_f_p, ema_s_p) and rsi_n > RSI_BUY_MIN
    sell = cross_below(ema_f_n, ema_s_n, ema_f_p, ema_s_p) and rsi_n < RSI_SELL_MAX

    prev = last_signal_dir.get(pair)
    direction = "BUY" if buy else "SELL" if sell else None

    if not direction or direction == prev:
        return None  # No new valid signal

    # Calculate SL/TP (wider range)
    risk = atr_n * ATR_MULT_SL
    if pair.endswith("JPY"):
        pip = 0.01
    else:
        pip = 0.0001

    if direction == "BUY":
        sl = price - risk
        tp = price + risk * TP_R_MULT
        emoji = "🟢"
    else:
        sl = price + risk
        tp = price - risk * TP_R_MULT
        emoji = "🔴"

    last_signal_dir[pair] = direction

    return (
        f"💱 <b>{pair}</b>\n"
        f"{emoji} <b>{direction}</b>\n"
        f"💰 Price: {price:.5f}\n"
        f"🛑 SL: {sl:.5f}\n"
        f"🎯 TP: {tp:.5f}"
    )

def run_scan():
    for p in PAIRS:
        try:
            signal = check_signal(p)
            if signal:
                log.info(f"Signal for {p}: {signal}")
                send_telegram(signal)
            else:
                log.info(f"No valid signal for {p}")
        except Exception as e:
            log.error(f"Error {p}: {e}")

# ============ FLASK (keep alive) ============
app = Flask(__name__)

@app.get("/")
def health():
    return "OK", 200

def main():
    log.info("🚀 Starting Forex Signal Bot")
    run_scan()

    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(run_scan, "interval", seconds=SCAN_EVERY_S)
    sched.start()

    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)

if __name__ == "__main__":
    main()
