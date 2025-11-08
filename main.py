# --- Patch for Python 3.13 removing stdlib imghdr (needed by telegram 13.x) ---
import sys, types
sys.modules['imghdr'] = types.SimpleNamespace(what=lambda *_: None)
# ------------------------------------------------------------------------------

import os
import time
import math
import logging
from typing import List, Tuple

from flask import Flask
import pandas as pd
import yfinance as yf
from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange
from telegram import Bot
from apscheduler.schedulers.background import BackgroundScheduler

# ---------------------- Settings (env-first, with sane defaults) ---------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# Comma-separated pairs WITHOUT "=X" are okay (e.g., "EURUSD,GBPUSD,USDJPY")
PAIRS = [s.strip().upper() for s in os.getenv("PAIRS", "EURUSD,GBPUSD,USDJPY").split(",") if s.strip()]

# Yahoo Finance interval & lookback
TIMEFRAME = os.getenv("TIMEFRAME", "15m").strip()     # e.g., 5m, 15m, 1h
PERIOD = os.getenv("PERIOD", "2d").strip()            # 2d gives enough candles

# Indicator params
EMA_FAST = int(os.getenv("EMA_FAST", "9"))
EMA_SLOW = int(os.getenv("EMA_SLOW", "21"))
RSI_LEN  = int(os.getenv("RSI_LEN", "14"))
ATR_LEN  = int(os.getenv("ATR_LEN", "14"))
SL_ATR   = float(os.getenv("SL_ATR", "1.5"))          # stop distance = SL_ATR * ATR
TP_ATR   = float(os.getenv("TP_ATR", "2.0"))          # take-profit distance = TP_ATR * ATR

# Scheduler cadence
CHECK_EVERY_MIN = int(os.getenv("CHECK_EVERY_MIN", "15"))

PORT = int(os.getenv("PORT", "8080"))

# ------------------------------------------------------------------------------
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("forex-bot")

# Telegram bot (lazy-init so we fail early if token missing)
_bot: Bot = None


def yahoo_symbol(sym: str) -> str:
    """Map 'EURUSD' -> 'EURUSD=X' unless already a Yahoo-style ticker."""
    s = sym.upper()
    return s if s.endswith("=X") else f"{s}=X"


def decimals_for(sym: str) -> int:
    """Rough formatting: JPY pairs 3 decimals, others 5."""
    return 3 if sym.endswith("JPY") else 5


def _ensure_series(close_col) -> pd.Series:
    """Guard against (n,1) DataFrame columns."""
    if isinstance(close_col, pd.DataFrame):
        return close_col.iloc[:, 0]
    return close_col


def fetch_signals_for_symbol(symbol: str) -> Tuple[bool, str]:
    """
    Returns (should_send, message).
    Strategy:
      - EMA(9) cross EMA(21) filter with RSI(14) > 50 for buys / < 50 for sells
      - Entry = last close
      - SL = ATR * SL_ATR
      - TP = ATR * TP_ATR
    """
    ysym = yahoo_symbol(symbol)

    # Pull recent candles
    data = yf.download(ysym, period=PERIOD, interval=TIMEFRAME, progress=False)
    if data is None or data.empty:
        return False, f"{symbol}: no data."

    # Safeguard multi-index / column oddities
    close = _ensure_series(data.get("Close"))
    high = _ensure_series(data.get("High"))
    low  = _ensure_series(data.get("Low"))

    if close is None or close.isna().all():
        return False, f"{symbol}: missing close data."

    # Indicators
    ema_fast = EMAIndicator(close=close, window=EMA_FAST).ema_indicator()
    ema_slow = EMAIndicator(close=close, window=EMA_SLOW).ema_indicator()
    rsi = RSIIndicator(close=close, window=RSI_LEN).rsi()
    atr = AverageTrueRange(high=high, low=low, close=close, window=ATR_LEN, fillna=False).average_true_range()

    last = close.index[-1]
    price = float(close.iloc[-1])
    e_fast = float(ema_fast.iloc[-1])
    e_slow = float(ema_slow.iloc[-1])
    r = float(rsi.iloc[-1])
    a = float(atr.iloc[-1]) if not math.isnan(float(atr.iloc[-1])) else 0.0

    dec = decimals_for(symbol)

    direction = None
    if e_fast > e_slow and r > 50:
        direction = "BUY"
        sl = price - SL_ATR * a if a > 0 else price * (1.0 - 0.001)  # fallback ~10 pips
        tp = price + TP_ATR * a if a > 0 else price * (1.0 + 0.002)
    elif e_fast < e_slow and r < 50:
        direction = "SELL"
        sl = price + SL_ATR * a if a > 0 else price * (1.0 + 0.001)
        tp = price - TP_ATR * a if a > 0 else price * (1.0 - 0.002)

    if not direction:
        return False, f"{symbol}: no clear setup (EMA{EMA_FAST}/{EMA_SLOW}, RSI={r:.1f})."

    msg = (
        f"📈 *{symbol}* signal\n"
        f"• Direction: *{direction}*\n"
        f"• Price: {price:.{dec}f}\n"
        f"• EMA({EMA_FAST})={e_fast:.{dec}f}, EMA({EMA_SLOW})={e_slow:.{dec}f}\n"
        f"• RSI({RSI_LEN})={r:.1f}\n"
        f"• ATR({ATR_LEN})={a:.{dec}f}\n"
        f"• SL: {sl:.{dec}f}\n"
        f"• TP: {tp:.{dec}f}\n"
        f"⏱ Timeframe: {TIMEFRAME}"
    )
    return True, msg


def run_scan():
    global _bot
    if not BOT_TOKEN or not CHAT_ID:
        log.error("BOT_TOKEN/CHAT_ID missing. Set Render environment variables.")
        return
    if _bot is None:
        _bot = Bot(token=BOT_TOKEN)

    for sym in PAIRS:
        try:
            should_send, message = fetch_signals_for_symbol(sym)
            log.info(message)
            if should_send:
                # Telegram v13 is synchronous — just call it
                _bot.send_message(chat_id=CHAT_ID, text=message, parse_mode="Markdown")
        except Exception as e:
            log.exception(f"Error processing {sym}: {e}")


# --------------------------- Flask endpoints -----------------------------------
@app.route("/")
def index():
    return "Forex Signal Bot is running ✅"

@app.route("/health")
def health():
    return "ok"


def start_scheduler():
    sched = BackgroundScheduler(timezone="UTC")
    # Start immediately, then every CHECK_EVERY_MIN minutes
    sched.add_job(run_scan, "interval", minutes=CHECK_EVERY_MIN, next_run_time=None, id="scan")
    sched.start()
    log.info(f"Scheduler started: every {CHECK_EVERY_MIN} min | Pairs={PAIRS}")


if __name__ == "__main__":
    log.info("🚀 Starting Multi-Pair Forex Signal Bot...")
    if not BOT_TOKEN or not CHAT_ID:
        log.error("Environment BOT_TOKEN or CHAT_ID not set. Exiting.")
        # Keep the web server up so you can still see the error on Render:
        app.run(host="0.0.0.0", port=PORT)
        sys.exit(1)

    # Run an immediate scan at startup (non-blocking safety)
    try:
        run_scan()
    except Exception as e:
        log.exception(f"Startup scan error: {e}")

    start_scheduler()
    log.info("✅ Forex Signal Bot is running on Render!")
    app.run(host="0.0.0.0", port=PORT)
