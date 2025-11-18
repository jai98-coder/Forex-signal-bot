import os
import logging
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import requests
import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask

# ============ ENV & CONFIG ============

TD_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# Default pairs – you can override with PAIRS env if you want
DEFAULT_PAIRS = [
    "GBPCAD", "USDCAD", "EURCAD", "USDJPY", "EURGBP", "GBPUSD",
    "GBPAUD", "AUDUSD", "GBPJPY", "EURNZD", "NZDUSD", "EURUSD",
]

pairs_env = os.getenv("PAIRS", "").strip()
if pairs_env:
    ALL_PAIRS = [p.strip().upper() for p in pairs_env.split(",") if p.strip()]
else:
    ALL_PAIRS = DEFAULT_PAIRS

INTERVAL = "1h"          # 1-hour candles
SCAN_EVERY_S = 60 * 60   # scan once per hour

# Indicator settings (conservative)
EMA_FAST = 20
EMA_SLOW = 50
RSI_LEN = 14
ADX_LEN = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

RSI_BUY_MIN = 55.0
RSI_BUY_MAX = 70.0
RSI_SELL_MIN = 30.0
RSI_SELL_MAX = 45.0

ADX_MIN = 22.0           # need clear trend

ATR_LEN = 14
ATR_MULT_SL = 1.0        # tighter SL for scalps
TP1_R_MULT = 1.0
TP2_R_MULT = 1.6
TP3_R_MULT = 2.2

# Trading session – Europe/Madrid
TRADING_TZ = ZoneInfo("Europe/Madrid")
TRADING_START = time(7, 15)   # 07:15 local
TRADING_END = time(22, 0)     # 22:00 local

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

# Track last signal dir & time to avoid spam
last_signal_state = {}  # pair -> {"dir": "BUY"/"SELL", "time": datetime}

# ========= PAIR GROUPING (respect 8 credits/min) =========

def build_groups(pairs, max_per_group=8):
    groups = []
    for i in range(0, len(pairs), max_per_group):
        groups.append(pairs[i:i + max_per_group])
    return groups or [[]]

PAIR_GROUPS = build_groups(ALL_PAIRS, max_per_group=8)
group_index = 0  # will rotate each hour


# ============ HELPERS ============

def td_symbol(pair: str) -> str:
    """Convert 'EURUSD' -> 'EUR/USD' for TwelveData."""
    pair = pair.upper()
    if len(pair) == 6:
        return f"{pair[:3]}/{pair[3:]}"
    return pair


def fetch_data(pair: str) -> pd.DataFrame:
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": td_symbol(pair),
        "interval": INTERVAL,
        "apikey": TD_API_KEY,
        "outputsize": 300,
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
    return df.dropna()


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(length).mean()
    avg_loss = loss.rolling(length).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    return 100 - (100 / (1 + rs))


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
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


def adx(df: pd.DataFrame, length: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)

    plus_dm = (high - prev_high)
    minus_dm = (prev_low - low)
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr_series = tr.rolling(length).mean()

    plus_di = 100 * (plus_dm.rolling(length).mean() / atr_series)
    minus_di = 100 * (minus_dm.rolling(length).mean() / atr_series)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).abs()
    return dx.rolling(length).mean()


def macd(series: pd.Series):
    fast = ema(series, MACD_FAST)
    slow = ema(series, MACD_SLOW)
    line = fast - slow
    signal = ema(line, MACD_SIGNAL)
    hist = line - signal
    return line, signal, hist


def in_trading_session(now_utc: datetime) -> bool:
    local = now_utc.astimezone(TRADING_TZ)
    t = local.time()
    if TRADING_START <= t <= TRADING_END:
        return True
    log.info("Outside trading hours (Europe/Madrid): %s", local)
    return False


def format_price(pair: str, value: float) -> str:
    if pair.endswith("JPY"):
        return f"{value:.3f}"
    else:
        return f"{value:.5f}"


def send_telegram(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        log.error("Missing Telegram BOT_TOKEN or CHAT_ID")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        log.error("Telegram send failed: %s", e)


# ============ STRATEGY (Option B conservative) ============

def check_signal(pair: str):
    df = fetch_data(pair)
    if len(df) < max(EMA_SLOW, RSI_LEN, ATR_LEN) + 5:
        return None

    close = df["close"]
    high = df["high"]
    low = df["low"]

    ema_fast = ema(close, EMA_FAST)
    ema_slow = ema(close, EMA_SLOW)
    rsi_val = rsi(close, RSI_LEN)
    atr_val = atr(df, ATR_LEN)
    adx_val = adx(df, ADX_LEN)
    _, _, macd_hist = macd(close)

    ema_f_now, ema_f_prev = ema_fast.iloc[-1], ema_fast.iloc[-2]
    ema_s_now, ema_s_prev = ema_slow.iloc[-1], ema_slow.iloc[-2]
    rsi_now = rsi_val.iloc[-1]
    adx_now = adx_val.iloc[-1]
    macd_h_now = macd_hist.iloc[-1]
    macd_h_prev = macd_hist.iloc[-2]
    price = close.iloc[-1]
    atr_now = atr_val.iloc[-1]

    # Require healthy trend
    up_trend = ema_f_now > ema_s_now and ema_f_now > ema_f_prev and ema_s_now >= ema_s_prev
    down_trend = ema_f_now < ema_s_now and ema_f_now < ema_f_prev and ema_s_now <= ema_s_prev

    # RSI filter (avoid overextended)
    buy_rsi_ok = RSI_BUY_MIN <= rsi_now <= RSI_BUY_MAX
    sell_rsi_ok = RSI_SELL_MIN <= rsi_now <= RSI_SELL_MAX

    # Momentum: MACD histogram direction
    buy_momentum = macd_h_now > 0 and macd_h_now > macd_h_prev
    sell_momentum = macd_h_now < 0 and macd_h_now < macd_h_prev

    # Trend strength
    trend_ok = adx_now >= ADX_MIN

    buy = up_trend and buy_rsi_ok and buy_momentum and trend_ok
    sell = down_trend and sell_rsi_ok and sell_momentum and trend_ok

    direction = "BUY" if buy else "SELL" if sell else None
    if direction is None:
        return None

    # Avoid duplicate signals in same direction within last 2 candles
    now_utc = datetime.now(timezone.utc)
    st = last_signal_state.get(pair)
    if st and st["dir"] == direction:
        # only allow new signal if more than 2 candles ago
        if (now_utc - st["time"]).total_seconds() < 2 * SCAN_EVERY_S:
            return None

    # ATR-based SL/TP
    risk = float(atr_now) * ATR_MULT_SL
    if risk <= 0:
        return None

    if direction == "BUY":
        sl = price - risk
        tp1 = price + risk * TP1_R_MULT
        tp2 = price + risk * TP2_R_MULT
        tp3 = price + risk * TP3_R_MULT
        emoji = "🟢"
    else:
        sl = price + risk
        tp1 = price - risk * TP1_R_MULT
        tp2 = price - risk * TP2_R_MULT
        tp3 = price - risk * TP3_R_MULT
        emoji = "🔴"

    last_signal_state[pair] = {"dir": direction, "time": now_utc}

    price_str = format_price(pair, price)
    sl_str = format_price(pair, sl)
    tp1_str = format_price(pair, tp1)
    tp2_str = format_price(pair, tp2)
    tp3_str = format_price(pair, tp3)

    text = (
        f"📉📈 <b>{pair}</b>\n"
        f"{emoji} <b>{direction}</b>\n"
        f"💰 Entry: {price_str}\n"
        f"🛑 SL: {sl_str}\n"
        f"🎯 TP1: {tp1_str}\n"
        f"🎯 TP2: {tp2_str}\n"
        f"🎯 TP3: {tp3_str}"
    )
    return text


# ============ SCAN LOOP ============

def run_scan():
    global group_index

    now_utc = datetime.now(timezone.utc)
    if not in_trading_session(now_utc):
        return

    if not TD_API_KEY:
        log.error("Missing TWELVEDATA_API_KEY")
        return

    pairs = PAIR_GROUPS[group_index]
    log.info("Scanning group %d / %d: %s",
             group_index + 1, len(PAIR_GROUPS), ", ".join(pairs))
    group_index = (group_index + 1) % len(PAIR_GROUPS)

    for p in pairs:
        try:
            signal = check_signal(p)
            if signal:
                log.info("Signal for %s:\n%s", p, signal)
                send_telegram(signal)
            else:
                log.info("No valid signal for %s", p)
        except Exception as e:
            log.error("Error %s: %s", p, e)


# ============ FLASK (keep alive) ============

app = Flask(__name__)

@app.get("/")
def health():
    return "OK", 200


def main():
    log.info("🚀 Starting Forex Signal Bot")
    run_scan()  # first scan on startup

    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(run_scan, "interval", seconds=SCAN_EVERY_S)
    sched.start()

    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
