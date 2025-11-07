import os
import time
import threading
import json
from datetime import datetime, timezone

import requests
import pandas as pd
import numpy as np
from flask import Flask

# ---------------------------
# Environment & defaults
# ---------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
TD_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()
SCAN_EVERY_MIN = int(os.getenv("SCAN_EVERY_MIN", "10"))

# Comma separated list like: "EUR/USD,GBP/USD,USD/JPY,AUD/USD,XAU/USD"
PAIRS_ENV = os.getenv(
    "PAIRS",
    "EUR/USD,GBP/USD,USD/JPY,AUD/USD,XAU/USD"
)

PAIRS = [p.strip().upper() for p in PAIRS_ENV.split(",") if p.strip()]

# Telegram send helper (simple and robust)
def tg_send(text: str, disable_notification=False):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram credentials missing; skipping send.")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            json={
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_notification": disable_notification,
            },
            timeout=15,
        )
    except Exception as e:
        print("Telegram send error:", e)

# ---------------------------
# Data: Twelve Data helpers
# ---------------------------
TwelveBase = "https://api.twelvedata.com/time_series"

def fetch_timeseries(symbol: str, interval="1h", outputsize=300):
    """
    Returns pandas.DataFrame with columns: open, high, low, close (float) indexed by UTC datetime ascending.
    """
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "format": "JSON",
        "apikey": TD_API_KEY,
        "timezone": "UTC",
        "order": "ASC",
    }
    r = requests.get(TwelveBase, params=params, timeout=20)
    r.raise_for_status()
    j = r.json()
    if "status" in j and j["status"] == "error":
        raise RuntimeError(f"Twelve Data error for {symbol}: {j.get('message')}")
    values = j.get("values")
    if not values:
        raise RuntimeError(f"No data for {symbol}")

    df = pd.DataFrame(values)
    # TwelveData uses strings, convert
    df.rename(
        columns={"datetime": "time", "open": "Open", "high": "High", "low": "Low", "close": "Close"},
        inplace=True,
    )
    df["time"] = pd.to_datetime(df["time"], utc=True)
    for col in ["Open", "High", "Low", "Close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna().set_index("time").sort_index()
    return df

# ---------------------------
# Indicators
# ---------------------------
def rsi(series: pd.Series, length: int = 21) -> pd.Series:
    delta = series.diff()
    up = np.where(delta > 0, delta, 0.0)
    down = np.where(delta < 0, -delta, 0.0)
    roll_up = pd.Series(up, index=series.index).ewm(alpha=1/length, adjust=False).mean()
    roll_down = pd.Series(down, index=series.index).ewm(alpha=1/length, adjust=False).mean()
    rs = roll_up / (roll_down + 1e-12)
    return 100.0 - (100.0 /
