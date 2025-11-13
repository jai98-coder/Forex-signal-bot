import os
import logging
from datetime import datetime
import requests
import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask

# ============ ENVIRONMENT VARIABLES ============
TD_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

PAIRS = "EURUSD,GBPUSD,USDJPY,EURCAD,GBPAUD,GBPCAD,USDCAD,EURNZD,NZDUSD,EURGBP,AUDUSD,GBPJPY"

INTERVAL = "30min"
SCAN_EVERY_S = 15 * 60   # scan every 15 minutes

# STRATEGY SETTINGS (Option C – strongest filtering)
EMA_FAST = 9
EMA_SLOW = 21
RSI_LEN = 14
ATR_LEN = 14

RSI_BUY_MIN = 58       # higher = fewer but cleaner buys
RSI_SELL_MAX = 42      # lower  = fewer but cleaner sells

SL_ATR_MULT = 1.2      # SL distance
TP1_MULT = 1.0         # TP1 = 1× ATR
TP2_MULT = 1.7         # TP2 = 1.7× ATR
TP3_MULT = 2.3         # TP3 = 2.3× ATR

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

last_signal = {}

# ============ DATA HELPERS ============

def td_symbol(pair):
    return f"{pair[:3]}/{pair[3:]}"


def fetch_data(pair):
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": td_symbol(pair),
        "interval": INTERVAL,
        "apikey": TD_API_KEY,
        "outputsize": 200,
        "order": "asc"
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
    gain = delta.clip(lower=0).rolling(length).mean()
    loss = (-delta.clip(upper=0)).rolling(length).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def atr(df, length=14):
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)

    return tr.rolling(length).mean()


def cross_above(f_now, s_now, f_prev, s_prev):
    return f_prev <= s_prev and f_now > s_now


def cross_below(f_now, s_now, f_prev, s_prev):
    return f_prev >= s_prev and f_now < s_now


# ============ TELEGRAM SEND ============
def send_telegram(text):
    if not BOT_TOKEN or not CHAT_ID:
        log.error("Missing Telegram vars")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}

    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        log.error(f"Telegram send failed: {e}")


# ============ SIGNAL CHECKER ============
def check_signal(pair):
    df = fetch_data(pair)

    close = df["close"]
    ema_fast = ema(close, EMA_FAST)
    ema_slow = ema(close, EMA_SLOW)
    rsi_val = rsi(close, RSI_LEN)
    atr_val = atr(df, ATR_LEN)

    price = close.iloc[-1]
    ema_f_n, ema_f_p = ema_fast.iloc[-1], ema_fast.iloc[-2]
    ema_s_n, ema_s_p = ema_slow.iloc[-1], ema_slow.iloc[-2]
    rsi_n = rsi_val.iloc[-1]
    atr_n = atr_val.iloc[-1]

    # ENTRY CONDITIONS
    buy = cross_above(ema_f_n, ema_s_n, ema_f_p, ema_s_p) and rsi_n > RSI_BUY_MIN
    sell = cross_below(ema_f_n, ema_s_n, ema_f_p, ema_s_p) and rsi_n < RSI_SELL_MAX

    direction = "BUY" if buy else "SELL" if sell else None
    if direction is None:
        return None

    # Avoid duplicates
    if last_signal.get(pair) == direction:
        return None

    # ATR distances
    sl_dist = atr_n * SL_ATR_MULT
    tp1_dist = atr_n * TP1_MULT
    tp2_dist = atr_n * TP2_MULT
    tp3_dist = atr_n * TP3_MULT

    if direction == "BUY":
        sl = price - sl_dist
        tp1 = price + tp1_dist
        tp2 = price + tp2_dist
        tp3 = price + tp3_dist
        emoji = "🟢"
    else:
        sl = price + sl_dist
        tp1 = price - tp1_dist
        tp2 = price - tp2_dist
        tp3 = price - tp3_dist
        emoji = "🔴"

    last_signal[pair] = direction

    # MESSAGE
    return (
        f"📊 <b>{pair}</b>\n"
        f"{emoji} <b>{direction}</b>\n"
        f"💰 Entry: {price:.5f}\n"
        f"🎯 TP1: {tp1:.5f}\n"
        f"🎯 TP2: {tp2:.5f}\n"
        f"🎯 TP3: {tp3:.5f}\n"
        f"⛔ SL: {sl:.5f}"
    )


# ============ SCAN LOOP ============
def run_scan():
    pairs = [p.strip().upper() for p in PAIRS.split(",")]

    for p in pairs:
        try:
            sig = check_signal(p)
            if sig:
                log.info(f"Signal for {p}")
                send_telegram(sig)
            else:
                log.info(f"No valid signal for {p}")
        except Exception as e:
            log.error(f"Error {p}: {e}")


# ============ FLASK KEEP-ALIVE ============
app = Flask(__name__)

@app.get("/")
def health():
    return "OK", 200


# ============ MAIN ============
def main():
    log.info("🚀 Starting Forex Signal Bot")
    run_scan()

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(run_scan, "interval", seconds=SCAN_EVERY_S)
    scheduler.start()

    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
