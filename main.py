import os
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask

# ============ ENVIRONMENT ============

TD_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
PAIRS_ENV = os.getenv(
    "PAIRS",
    "EURUSD,GBPUSD,USDJPY,EURCAD,GBPAUD,GBPCAD,USDCAD,GBPJPY",
)

# TwelveData free tier: 8 credits per minute.
# We hard-limit to the FIRST 8 pairs to avoid 429 errors.
ALL_PAIRS = [p.strip().upper() for p in PAIRS_ENV.split(",") if p.strip()]
PAIRS = ALL_PAIRS[:8]

# ============ STRATEGY SETTINGS (1H) ============

INTERVAL = "1h"              # 1-hour candles
SCAN_EVERY_S = 15 * 60       # run every 15 minutes

EMA_FAST = 50
EMA_MID = 100
EMA_SLOW = 200

RSI_LEN = 14
ATR_LEN = 14

RSI_BUY_CROSS = 50.0         # RSI crossing UP through 50 in uptrend
RSI_SELL_CROSS = 50.0        # RSI crossing DOWN through 50 in downtrend

ATR_MULT_SL = 1.6            # SL distance ≈ 1.6 * ATR
TP1_R = 1.0                  # TP1 = 1R
TP2_R = 1.8                  # TP2 = 1.8R
TP3_R = 2.6                  # TP3 = 2.6R

MIN_SL_PIPS = 7              # ignore tiny / noisy moves
MAX_SL_PIPS = 40             # ignore huge, wide-SL setups

MADRID_TZ = ZoneInfo("Europe/Madrid")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

# To avoid duplicate alerts on the same candle
last_signal_bar = {}   # pair -> last bar datetime
last_signal_dir = {}   # pair -> "BUY" / "SELL"


# ============ HELPERS ============

def td_symbol(pair: str) -> str:
    """Convert 'EURUSD' -> 'EUR/USD' for TwelveData."""
    pair = pair.upper()
    if len(pair) == 6:
        return f"{pair[:3]}/{pair[3:]}"
    return pair


def fetch_data(pair: str) -> pd.DataFrame:
    """Fetch 1H historical data from TwelveData."""
    if not TD_API_KEY:
        raise ValueError("TWELVEDATA_API_KEY is missing")

    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": td_symbol(pair),
        "interval": INTERVAL,
        "apikey": TD_API_KEY,
        "outputsize": 250,
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


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    gain = (delta.clip(lower=0)).rolling(length).mean()
    loss = (-delta.clip(upper=0)).rolling(length).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    high, low, close = df["high"], df["close"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(length).mean()


def cross_above(x_now, y_now, x_prev, y_prev) -> bool:
    return x_prev <= y_prev and x_now > y_now


def cross_below(x_now, y_now, x_prev, y_prev) -> bool:
    return x_prev >= y_prev and x_now < y_now


def pips_from_delta(pair: str, delta: float) -> float:
    """Convert price distance to pips (approx)."""
    if pair.endswith("JPY"):
        return abs(delta) * 100  # 0.01 ≈ 1 pip
    return abs(delta) * 10000    # 0.0001 ≈ 1 pip


def fmt_price(pair: str, price: float) -> str:
    """Nice decimal formatting for JPY vs non-JPY."""
    if pair.endswith("JPY"):
        return f"{price:.3f}"
    else:
        return f"{price:.5f}"


def trading_hours_ok(now_utc: datetime | None = None) -> bool:
    """Allow trading only between 07:15 and 22:00 Madrid time."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    local = now_utc.astimezone(MADRID_TZ)
    start = local.replace(hour=7, minute=15, second=0, microsecond=0)
    end = local.replace(hour=22, minute=0, second=0, microsecond=0)

    return start <= local <= end


def send_telegram(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        log.error("Missing BOT_TOKEN or CHAT_ID")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }

    try:
        resp = requests.post(url, json=data, timeout=15)
        if not resp.ok:
            log.error("Telegram send failed: %s", resp.text)
    except Exception as e:
        log.error("Telegram send exception: %s", e)


# ============ SIGNAL LOGIC (1H TREND + PULLBACK) ============

def check_signal(pair: str) -> str | None:
    df = fetch_data(pair)
    if len(df) < max(EMA_SLOW, RSI_LEN, ATR_LEN) + 5:
        log.info("Not enough data for %s", pair)
        return None

    close = df["close"]
    ema_fast = ema(close, EMA_FAST)
    ema_mid = ema(close, EMA_MID)
    ema_slow = ema(close, EMA_SLOW)
    rsi_val = rsi(close, RSI_LEN)
    atr_val = atr(df, ATR_LEN)

    # Latest and previous bar
    c_now = close.iloc[-1]
    c_prev = close.iloc[-2]

    e_fast_now = ema_fast.iloc[-1]
    e_fast_prev = ema_fast.iloc[-2]

    e_mid_now = ema_mid.iloc[-1]
    e_slow_now = ema_slow.iloc[-1]

    rsi_now = rsi_val.iloc[-1]
    rsi_prev = rsi_val.iloc[-2]

    atr_now = atr_val.iloc[-1]
    bar_time = df["datetime"].iloc[-1].to_pydatetime()

    # Avoid duplicate alerts on the same bar
    last_bar = last_signal_bar.get(pair)
    if last_bar is not None and bar_time == last_bar:
        return None

    # Trend filters
    uptrend = e_fast_now > e_mid_now > e_slow_now and c_now > e_fast_now
    downtrend = e_fast_now < e_mid_now < e_slow_now and c_now < e_fast_now

    if not uptrend and not downtrend:
        return None

    direction = None

    # BUY setup: uptrend, price bouncing above EMA50, RSI crossing UP through 50
    if uptrend:
        bounce = cross_above(c_now, e_fast_now, c_prev, e_fast_prev)
        rsi_cross = rsi_prev < RSI_BUY_CROSS <= rsi_now
        if bounce and rsi_cross:
            direction = "BUY"

    # SELL setup: downtrend, price bouncing below EMA50, RSI crossing DOWN through 50
    if downtrend and direction is None:
        bounce = cross_below(c_now, e_fast_now, c_prev, e_fast_prev)
        rsi_cross = rsi_prev > RSI_SELL_CROSS >= rsi_now
        if bounce and rsi_cross:
            direction = "SELL"

    if direction is None:
        return None

    # ATR-based SL / TP, with pip sanity check
    risk_price = atr_now * ATR_MULT_SL
    risk_pips = pips_from_delta(pair, risk_price)

    if pd.isna(risk_price) or risk_pips < MIN_SL_PIPS or risk_pips > MAX_SL_PIPS:
        log.info(
            "Skipping %s: risk_pips=%.1f (ATR too small/large)",
            pair,
            risk_pips,
        )
        return None

    price = c_now

    if direction == "BUY":
        sl = price - risk_price
        tp1 = price + risk_price * TP1_R
        tp2 = price + risk_price * TP2_R
        tp3 = price + risk_price * TP3_R
        color = "🟢"
    else:
        sl = price + risk_price
        tp1 = price - risk_price * TP1_R
        tp2 = price - risk_price * TP2_R
        tp3 = price - risk_price * TP3_R
        color = "🔴"

    # Remember last signal bar/direction
    last_signal_bar[pair] = bar_time
    last_signal_dir[pair] = direction

    text = (
        f"📊📉📈 <b>{pair}</b>\n"
        f"{color} <b>{direction}</b>\n"
        f"💰 Price: {fmt_price(pair, price)}\n"
        f"⛔ SL: {fmt_price(pair, sl)}\n"
        f"🥇 TP1: {fmt_price(pair, tp1)}\n"
        f"🥈 TP2: {fmt_price(pair, tp2)}\n"
        f"🥉 TP3: {fmt_price(pair, tp3)}"
    )
    return text


def run_scan():
    now_utc = datetime.now(timezone.utc)

    if not trading_hours_ok(now_utc):
        log.info("Outside Madrid trading hours – no scan.")
        return

    for pair in PAIRS:
        try:
            signal = check_signal(pair)
            if signal:
                log.info("Signal for %s", pair)
                send_telegram(signal)
            else:
                log.info("No valid signal for %s", pair)
        except Exception as e:
            log.error("Error %s: %s", pair, e)


# ============ FLASK KEEP-ALIVE ============

app = Flask(__name__)


@app.get("/")
def health():
    return "OK", 200


def main():
    log.info("🚀 Starting Forex Signal Bot (1H swing mode)")
    log.info("Pairs in use (max 8): %s", ", ".join(PAIRS))

    # First scan on startup
    run_scan()

    # Background scheduler in UTC
    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(run_scan, "interval", seconds=SCAN_EVERY_S)
    sched.start()

    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
