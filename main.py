import os
import logging
from datetime import datetime
import requests
import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask

# ============ ENV & CONFIG ============

TD_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# Default pairs if PAIRS env is not set
DEFAULT_PAIRS = (
    "GBPCAD,USDCAD,EURCAD,"
    "USDJPY,EURGBP,GBPUSD,GBPAUD,"
    "AUDUSD,GBPJPY,EURNZD,NZDUSD,EURUSD"
)
PAIRS = os.getenv("PAIRS", DEFAULT_PAIRS)

# --- Trading settings ---
INTERVAL = "15min"                # chart timeframe on TwelveData
SCAN_EVERY_S = 15 * 60            # run every 15 minutes

EMA_FAST = 9
EMA_SLOW = 21
RSI_LEN = 14
ATR_LEN = 14

# RSI levels (slightly wide to get more trades)
RSI_BUY_MIN = 52.0
RSI_SELL_MAX = 48.0

# Volatility & TP/SL
ATR_MULT_SL = 1.0        # SL distance
TP_R_MULT = 1.5          # TP = SL * TP_R_MULT
MIN_ATR_PIPS = 5         # skip tiny ATR moves (in pips)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

# remember last direction per pair so we don't spam duplicates
last_signal_dir = {}

# ============ HELPERS ============

def td_symbol(pair: str) -> str:
    pair = pair.upper()
    if len(pair) == 6:
        return f"{pair[:3]}/{pair[3:]}"
    return pair


def fetch_data(pair: str) -> pd.DataFrame:
    if not TD_API_KEY:
        raise RuntimeError("TWELVEDATA_API_KEY is missing")

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


def rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    gain = (delta.clip(lower=0)).rolling(length).mean()
    loss = (-delta.clip(upper=0)).rolling(length).mean()
    rs = gain / loss
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


def send_telegram(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        log.error("Missing Telegram BOT_TOKEN or CHAT_ID")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, json=data, timeout=10)
        if resp.status_code != 200:
            log.error("Telegram send failed: %s", resp.text)
    except Exception as e:
        log.error("Telegram send exception: %s", e)


# ============ STRATEGY LOGIC ============

def check_signal(pair: str) -> str | None:
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

    # Skip if not enough history yet
    if pd.isna(ema_f_n) or pd.isna(ema_s_n) or pd.isna(rsi_n) or pd.isna(atr_n):
        return None

    # Convert ATR to "pips" for volatility filter
    if pair.endswith("JPY"):
        pip_size = 0.01
    else:
        pip_size = 0.0001

    atr_pips = atr_n / pip_size
    if atr_pips < MIN_ATR_PIPS:
        # market too dead -> skip
        return None

    # Trend-based direction (not just one crossover)
    bullish_trend = ema_f_n > ema_s_n and price > ema_f_n > ema_s_n
    bearish_trend = ema_f_n < ema_s_n and price < ema_f_n < ema_s_n

    buy
