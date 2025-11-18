import os
import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

import requests
import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask

# ================== ENV VARS ==================
TD_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# Default pairs – you can override with PAIRS env
PAIRS = os.getenv(
    "PAIRS",
    "EURUSD,GBPUSD,USDJPY,EURCAD,GBPAUD,GBPCAD,USDCAD,GBPJPY,EURNZD,EURGBP,AUDUSD,NZDUSD",
)

INTERVAL = "30min"
SCAN_EVERY_S = 15 * 60  # every 15 minutes

# Strategy parameters (Option A: High Accuracy)
EMA_FAST = 20
EMA_SLOW = 50
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

RSI_LEN = 14
RSI_BUY_MIN = 40.0
RSI_BUY_MAX = 65.0
RSI_SELL_MIN = 35.0
RSI_SELL_MAX = 60.0

ADX_LEN = 14
ADX_MIN = 20.0  # minimum trend strength

ATR_LEN = 14
ATR_SL_MULT = 1.0
TP1_MULT = 1.0   # 1R
TP2_MULT = 1.8   # ~1.8R
TP3_MULT = 2.5   # ~2.5R

# How many pairs per run (to stay under 8 requests / minute)
PAIRS_PER_RUN = 6

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

# Track last direction per pair to avoid spam
last_signal_dir = {}
pair_index = 0  # for rotating through pairs


# ================== HELPERS ==================
def td_symbol(pair: str) -> str:
    pair = pair.upper()
    if len(pair) == 6:
        return f"{pair[:3]}/{pair[3:]}"
    return pair


def fetch_data(pair: str) -> pd.DataFrame:
    if not TD_API_KEY:
        raise RuntimeError("TWELVEDATA_API_KEY env var is missing")

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


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def rsi(series: pd.Series, length: int) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(length).mean()
    avg_loss = loss.rolling(length).mean()

    rs = avg_gain / avg_loss
    rsi_val = 100 - (100 / (1 + rs))
    return rsi_val


def atr(df: pd.DataFrame, length: int) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
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


def adx(df: pd.DataFrame, length: int) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)

    plus_dm = (high - prev_high).clip(lower=0)
    minus_dm = (prev_low - low).clip(lower=0)

    plus_dm = plus_dm.where(plus_dm > minus_dm, 0.0)
    minus_dm = minus_dm.where(minus_dm > plus_dm, 0.0)

    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr_val = tr.rolling(length).mean()
    plus_di = 100 * (plus_dm.rolling(length).mean() / atr_val)
    minus_di = 100 * (minus_dm.rolling(length).mean() / atr_val)

    dx = (abs(plus_di - minus_di) / (plus_di + minus_di)).replace([pd.NA, pd.NaT], 0) * 100
    adx_val = dx.rolling(length).mean()
    return adx_val


def macd(series: pd.Series):
    ema_fast = ema(series, MACD_FAST)
    ema_slow = ema(series, MACD_SLOW)
    macd_line = ema_fast - ema_slow
    signal = ema(macd_line, MACD_SIGNAL)
    hist = macd_line - signal
    return macd_line, signal, hist


def within_trading_session() -> bool:
    """Trading window: 07:15–22:00 Europe/Madrid, Monday–Friday."""
    now_local = datetime.now(ZoneInfo("Europe/Madrid"))
    # Monday=0 ... Sunday=6
    if now_local.weekday() >= 5:
        return False

    t = now_local.time()
    start = time(7, 15)
    end = time(22, 0)
    return start <= t <= end


def cross_above(curr_fast, curr_slow, prev_fast, prev_slow) -> bool:
    return prev_fast <= prev_slow and curr_fast > curr_slow


def cross_below(curr_fast, curr_slow, prev_fast, prev_slow) -> bool:
    return prev_fast >= prev_slow and curr_fast < curr_slow


def send_telegram(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        log.error("Missing BOT_TOKEN or CHAT_ID env vars")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }
    try:
        r = requests.post(url, json=data, timeout=10)
        if r.status_code != 200:
            log.error("Telegram error: %s", r.text)
    except Exception as e:
        log.error("Telegram send failed: %s", e)


def format_price(pair: str, value: float) -> str:
    if pair.endswith("JPY"):
        return f"{value:.3f}"
    else:
        return f"{value:.5f}"


# ================== SIGNAL LOGIC ==================
def build_signal(pair: str):
    df = fetch_data(pair)

    if len(df) < max(EMA_SLOW, MACD_SLOW, RSI_LEN, ATR_LEN, ADX_LEN) + 5:
        return None

    close = df["close"]

    ema_fast = ema(close, EMA_FAST)
    ema_slow = ema(close, EMA_SLOW)

    macd_line, macd_signal, macd_hist = macd(close)
    rsi_val = rsi(close, RSI_LEN)
    atr_val = atr(df, ATR_LEN)
    adx_val = adx(df, ADX_LEN)

    ema_f_now, ema_f_prev = ema_fast.iloc[-1], ema_fast.iloc[-2]
    ema_s_now, ema_s_prev = ema_slow.iloc[-1], ema_slow.iloc[-2]

    macd_hist_now, macd_hist_prev = macd_hist.iloc[-1], macd_hist.iloc[-2]
    rsi_now = rsi_val.iloc[-1]
    atr_now = atr_val.iloc[-1]
    adx_now = adx_val.iloc[-1]
    price = close.iloc[-1]

    # ATR percentile filter (volatility not too low)
    recent_atr = atr_val.dropna().tail(100)
    if len(recent_atr) >= 20:
        atr_threshold = recent_atr.quantile(0.2)
        if atr_now < atr_threshold:
            return None

    # Trend filters
    trend_up = ema_f_now > ema_s_now
    trend_down = ema_f_now < ema_s_now

    # MACD confirmation (direction + hist increasing in trend direction)
    macd_bull = macd_hist_now > 0 and macd_hist_now > macd_hist_prev
    macd_bear = macd_hist_now < 0 and macd_hist_now < macd_hist_prev

    # ADX strength
    if pd.isna(adx_now) or adx_now < ADX_MIN:
        return None

    # Price not too extended from ema_fast (avoid chasing)
    # Allow max 0.3% away from EMA_FAST
    max_ext = 0.003 * price
    if abs(price - ema_f_now) > max_ext:
        return None

    # Entry conditions (Option A high accuracy, but you picked intra-candle mode)
    buy = (
        trend_up
        and macd_bull
        and RSI_BUY_MIN <= rsi_now <= RSI_BUY_MAX
        and cross_above(ema_f_now, ema_s_now, ema_f_prev, ema_s_prev)
    )

    sell = (
        trend_down
        and macd_bear
        and RSI_SELL_MIN <= rsi_now <= RSI_SELL_MAX
        and cross_below(ema_f_now, ema_s_now, ema_f_prev, ema_s_prev)
    )

    direction = "BUY" if buy else "SELL" if sell else None
    if not direction:
        return None

    prev_dir = last_signal_dir.get(pair)
    if prev_dir == direction:
        # avoid repeating same direction signal in same trend leg
        return None

    # --- Risk / Reward: SL + TP1/2/3 ---
    risk = ATR_SL_MULT * atr_now

    if direction == "BUY":
        sl = price - risk
        tp1 = price + risk * TP1_MULT
        tp2 = price + risk * TP2_MULT
        tp3 = price + risk * TP3_MULT
        emoji = "🟢"
    else:
        sl = price + risk
        tp1 = price - risk * TP1_MULT
        tp2 = price - risk * TP2_MULT
        tp3 = price - risk * TP3_MULT
        emoji = "🔴"

    last_signal_dir[pair] = direction

    price_str = format_price(pair, price)
    sl_str = format_price(pair, sl)
    tp1_str = format_price(pair, tp1)
    tp2_str = format_price(pair, tp2)
    tp3_str = format_price(pair, tp3)

    text = (
        f"📉📈 <b>{pair}</b>\n"
        f"{emoji} <b>{direction}</b>\n"
        f"💰 Price: {price_str}\n"
        f"🛑 SL: {sl_str}\n"
        f"🎯 TP1: {tp1_str}\n"
        f"🎯 TP2: {tp2_str}\n"
        f"🎯 TP3: {tp3_str}"
    )
    return text


def run_scan():
    global pair_index

    if not within_trading_session():
        log.info("Outside trading hours (Madrid 07:15–22:00). Skipping scan.")
        return

    pairs = [p.strip().upper() for p in PAIRS.split(",") if p.strip()]
    if not pairs:
        log.error("No pairs configured in PAIRS env.")
        return

    if PAIRS_PER_RUN >= len(pairs):
        batch = pairs
    else:
        start = pair_index
        end = start + PAIRS_PER_RUN
        # wrap around
        extended = pairs + pairs
        batch = extended[start:end]
        pair_index = (pair_index + PAIRS_PER_RUN) % len(pairs)

    log.info("Scanning pairs this run: %s", ", ".join(batch))

    for p in batch:
        try:
            signal = build_signal(p)
            if signal:
                log.info("Signal for %s", p)
                send_telegram(signal)
            else:
                log.info("No valid signal for %s", p)
        except Exception as e:
            log.error("Error %s: %s", p, e)


# ================== FLASK APP (keep-alive) ==================
app = Flask(__name__)


@app.get("/")
def health():
    return "OK", 200


def main():
    log.info("🚀 Starting Forex Signal Bot")

    if not TD_API_KEY:
        log.error("Missing TWELVEDATA_API_KEY env var")
    if not BOT_TOKEN or not CHAT_ID:
        log.error("Missing BOT_TOKEN or CHAT_ID env vars")

    # Run once at startup
    run_scan()

    # Schedule every 15 minutes (UTC)
    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(run_scan, "interval", seconds=SCAN_EVERY_S)
    sched.start()

    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
