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

# MAX 8 PAIRS FOR FREE TIER
PAIRS = os.getenv("PAIRS", "EURUSD,GBPUSD,USDJPY,EURCAD,GBPAUD,GBPCAD,USDCAD,GBPJPY")

INTERVAL = "30min"
SCAN_EVERY_S = 15 * 60  # every 15 minutes

# Strategy Settings (Improved Scalping)
EMA_FAST = 5
EMA_SLOW = 20
RSI_LEN = 14
RSI_BUY_MIN = 60
RSI_SELL_MAX = 40
ATR_LEN = 14

# Tighter scalping targets
SL_ATR = 0.8
TP1_ATR = 1.2
TP2_ATR = 2.0
TP3_ATR = 3.0

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

last_signal_dir = {}

# ============ HELPERS ============
def td_symbol(pair):
    pair = pair.upper()
    return f"{pair[:3]}/{pair[3:]}" if len(pair) == 6 else pair

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
    gain = (delta.clip(lower=0)).rolling(length).mean()
    loss = (-delta.clip(upper=0)).rolling(length).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def atr(df, length=14):
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)

    return tr.rolling(length).mean()

def cross_above(nf, ns, pf, ps):
    return pf <= ps and nf > ns

def cross_below(nf, ns, pf, ps):
    return pf >= ps and nf < ns

def send_telegram(text):
    if not BOT_TOKEN or not CHAT_ID:
        log.error("Missing Telegram vars")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        log.error("Telegram error: %s", e)

def check_signal(pair):
    df = fetch_data(pair)
    close = df["close"]

    ema_fast = ema(close, EMA_FAST)
    ema_slow = ema(close, EMA_SLOW)
    rsi_val = rsi(close, RSI_LEN)
    atr_val = atr(df, ATR_LEN)

    f_now, f_prev = ema_fast.iloc[-1], ema_fast.iloc[-2]
    s_now, s_prev = ema_slow.iloc[-1], ema_slow.iloc[-2]
    rsi_now = rsi_val.iloc[-1]
    atr_now = atr_val.iloc[-1]
    price = close.iloc[-1]

    # Direction
    buy = cross_above(f_now, s_now, f_prev, s_prev) and rsi_now > RSI_BUY_MIN
    sell = cross_below(f_now, s_now, f_prev, s_prev) and rsi_now < RSI_SELL_MAX

    direction = "BUY" if buy else "SELL" if sell else None

    if not direction:
        return None

    # Prevent duplicate signals
    if last_signal_dir.get(pair) == direction:
        return None

    # PIP handling
    pip = 0.01 if pair.endswith("JPY") else 0.0001

    # SL & TP based on ATR scalping
    if direction == "BUY":
        sl = price - atr_now * SL_ATR
        tp1 = price + atr_now * TP1_ATR
        tp2 = price + atr_now * TP2_ATR
        tp3 = price + atr_now * TP3_ATR
        emoji = "🟢"
    else:
        sl = price + atr_now * SL_ATR
        tp1 = price - atr_now * TP1_ATR
        tp2 = price - atr_now * TP2_ATR
        tp3 = price - atr_now * TP3_ATR
        emoji = "🔴"

    last_signal_dir[pair] = direction

    return (
        f"📉📈 <b>{pair}</b>\n"
        f"{emoji} <b>{direction}</b>\n"
        f"💰 Price: {price:.5f}\n"
        f"🛑 SL: {sl:.5f}\n"
        f"🎯 TP1: {tp1:.5f}\n"
        f"🎯 TP2: {tp2:.5f}\n"
        f"🎯 TP3: {tp3:.5f}"
    )

def run_scan():
    pairs = [p.strip().upper() for p in PAIRS.split(",")]
    for p in pairs:
        try:
            s = check_signal(p)
            if s:
                log.info(f"Signal for {p}: {s}")
                send_telegram(s)
            else:
                log.info(f"No valid signal for {p}")
        except Exception as e:
            log.error(f"Error {p}: {e}")

# ============ FLASK ============
app = Flask(__name__)

@app.get("/")
def health():
    return "OK", 200

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
