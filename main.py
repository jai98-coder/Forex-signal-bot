# --- Patch for Python 3.13 removing stdlib imghdr (some libs import it) ---
import sys, types
sys.modules['imghdr'] = types.SimpleNamespace(what=lambda *_: None)
# -----------------------------------------------------------------------

import os
import math
import logging
from typing import Tuple

from flask import Flask
import pandas as pd
import yfinance as yf
from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange
from apscheduler.schedulers.background import BackgroundScheduler
import telebot  # ✅ modern Telegram library

# -----------------------------------------------------------------------
# ENVIRONMENT CONFIG
# -----------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
PAIRS = [s.strip().upper() for s in os.getenv("PAIRS", "EURUSD,GBPUSD,USDJPY").split(",") if s.strip()]
TIMEFRAME = os.getenv("TIMEFRAME", "15m").strip()
PERIOD = os.getenv("PERIOD", "2d").strip()

EMA_FAST = int(os.getenv("EMA_FAST", "9"))
EMA_SLOW = int(os.getenv("EMA_SLOW", "21"))
RSI_LEN  = int(os.getenv("RSI_LEN", "14"))
ATR_LEN  = int(os.getenv("ATR_LEN", "14"))
SL_ATR   = float(os.getenv("SL_ATR", "1.5"))
TP_ATR   = float(os.getenv("TP_ATR", "2.0"))
CHECK_EVERY_MIN = int(os.getenv("CHECK_EVERY_MIN", "15"))
PORT = int(os.getenv("PORT", "8080"))

# -----------------------------------------------------------------------
# INIT
# -----------------------------------------------------------------------
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("forex-bot")
bot = telebot.TeleBot(BOT_TOKEN) if BOT_TOKEN else None


def yahoo_symbol(sym: str) -> str:
    return sym if sym.endswith("=X") else f"{sym}=X"


def decimals_for(sym: str) -> int:
    return 3 if sym.endswith("JPY") else 5


# -----------------------------------------------------------------------
# STRATEGY
# -----------------------------------------------------------------------
def fetch_signal(symbol: str) -> Tuple[bool, str]:
    ysym = yahoo_symbol(symbol)
    data = yf.download(ysym, period=PERIOD, interval=TIMEFRAME, progress=False)
    if data is None or data.empty:
        return False, f"{symbol}: no data."

    # --- FIX: Flatten data to avoid 2D array errors ---
    data = data.squeeze()
    if isinstance(data["Close"], pd.DataFrame):
        data["Close"] = data["Close"].squeeze()
    if isinstance(data["High"], pd.DataFrame):
        data["High"] = data["High"].squeeze()
    if isinstance(data["Low"], pd.DataFrame):
        data["Low"] = data["Low"].squeeze()
    # --------------------------------------------------

    close, high, low = data["Close"], data["High"], data["Low"]
    ema_fast = EMAIndicator(close, EMA_FAST).ema_indicator()
    ema_slow = EMAIndicator(close, EMA_SLOW).ema_indicator()
    rsi = RSIIndicator(close, RSI_LEN).rsi()
    atr = AverageTrueRange(high, low, close, ATR_LEN, fillna=False).average_true_range()

    price, e_fast, e_slow, r, a = float(close.iloc[-1]), float(ema_fast.iloc[-1]), float(ema_slow.iloc[-1]), float(rsi.iloc[-1]), float(atr.iloc[-1])
    dec = decimals_for(symbol)

    if e_fast > e_slow and r > 50:
        direction = "BUY"
        sl, tp = price - SL_ATR * a, price + TP_ATR * a
    elif e_fast < e_slow and r < 50:
        direction = "SELL"
        sl, tp = price + SL_ATR * a, price - TP_ATR * a
    else:
        return False, f"{symbol}: no clear signal (RSI={r:.1f})"

    msg = (
        f"📊 *{symbol}* SIGNAL\n"
        f"➡️ {direction}\n"
        f"💰 Price: {price:.{dec}f}\n"
        f"📈 EMA({EMA_FAST})={e_fast:.{dec}f}, EMA({EMA_SLOW})={e_slow:.{dec}f}\n"
        f"📉 RSI({RSI_LEN})={r:.1f}\n"
        f"📏 ATR({ATR_LEN})={a:.{dec}f}\n"
        f"🛑 SL: {sl:.{dec}f}\n"
        f"🎯 TP: {tp:.{dec}f}\n"
        f"⏱ {TIMEFRAME}"
    )
    return True, msg


def run_scan():
    if not bot or not CHAT_ID:
        log.error("Missing BOT_TOKEN or CHAT_ID")
        return
    for sym in PAIRS:
        try:
            ok, msg = fetch_signal(sym)
            log.info(msg)
            if ok:
                bot.send_message(CHAT_ID, msg, parse_mode="Markdown")
        except Exception as e:
            log.exception(f"Error {sym}: {e}")


# -----------------------------------------------------------------------
# WEB + SCHEDULER
# -----------------------------------------------------------------------
@app.route("/")
def index(): 
    return "Forex Signal Bot running ✅"

@app.route("/health")
def health(): 
    return "ok"

def start_scheduler():
    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(run_scan, "interval", minutes=CHECK_EVERY_MIN)
    sched.start()
    log.info(f"Scheduler started every {CHECK_EVERY_MIN} min: {PAIRS}")


# -----------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------
if __name__ == "__main__":
    log.info("🚀 Starting Forex Signal Bot")
    if not BOT_TOKEN or not CHAT_ID:
        log.error("Missing Telegram env vars.")
        app.run(host="0.0.0.0", port=PORT)
    else:
        try: 
            run_scan()
        except Exception as e: 
            log.exception(e)
        start_scheduler()
        app.run(host="0.0.0.0", port=PORT)
