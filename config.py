"""
Configuration for AI F&O Trading Bot
All sensitive values are loaded from environment variables (.env file)
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ─── Broker (Upstox API v2) ───────────────────────────────────────────────────
UPSTOX_API_KEY      = os.getenv("UPSTOX_API_KEY", "")
UPSTOX_API_SECRET   = os.getenv("UPSTOX_API_SECRET", "")
UPSTOX_ACCESS_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "")   # refreshed daily
UPSTOX_REDIRECT_URI = os.getenv("UPSTOX_REDIRECT_URI", "http://127.0.0.1/")

# ─── Telegram Alerts ──────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ─── Trading Universe ─────────────────────────────────────────────────────────
WATCHLIST = [
    # NSE / BSE Index F&O
    "NIFTY",
    "BANKNIFTY",
    "SENSEX",
    # MCX Commodity F&O
    "CRUDEOIL",
    "NATURALGAS",
    "GOLD",
]

# Upstox instrument key map for NSE/BSE index underlyings
UPSTOX_INDEX_KEYS = {
    "NIFTY":        "NSE_INDEX|Nifty 50",
    "BANKNIFTY":    "NSE_INDEX|Nifty Bank",
    "SENSEX":       "BSE_INDEX|SENSEX",
}

# Upstox underlying keys for MCX commodities
# These are used for get_option_contracts — verify from Upstox instrument master
UPSTOX_COMMODITY_KEYS = {
    "CRUDEOIL":   "MCX_FO|CRUDEOIL",
    "NATURALGAS": "MCX_FO|NATURALGAS",
    "GOLD":       "MCX_FO|GOLD",
}

# Exchange per symbol (used for order routing)
SYMBOL_EXCHANGE = {
    "NIFTY":        "NFO",
    "BANKNIFTY":    "NFO",
    "SENSEX":       "BSE",
    "CRUDEOIL":     "MCX",
    "NATURALGAS":   "MCX",
    "GOLD":         "MCX",
}

# Weekly expiry day per index  (0=Mon 1=Tue 2=Wed 3=Thu 4=Fri)
INDEX_EXPIRY_DAY = {
    "NIFTY":        3,   # Thursday
    "BANKNIFTY":    2,   # Wednesday
    "SENSEX":       1,   # Tuesday  (BSE weekly)
}

# NSE lot sizes (updated per SEBI circular 2024-25)
INDEX_LOT_SIZE = {
    "NIFTY":        65,
    "BANKNIFTY":    30,
    "SENSEX":       20,
}

# MCX commodity lot sizes (from Upstox instrument master)
MCX_LOT_SIZE = {
    "CRUDEOIL":   100,    # 100 barrels per lot
    "NATURALGAS": 1250,   # 1250 mmBtu per lot
    "GOLD":       1,      # 1 kg per lot (price quoted per 10g, lot value = price × 100)
}

# Strike gap per index
INDEX_STRIKE_STEP = {
    "NIFTY":        50,
    "BANKNIFTY":    100,
    "SENSEX":       100,
}

# Strike gap for MCX commodity options
MCX_STRIKE_STEP = {
    "CRUDEOIL":   50,    # ₹50 per barrel
    "NATURALGAS": 5,     # ₹5 per mmBtu
    "GOLD":       100,   # ₹100 per 10g
}

# Implied volatility proxy per symbol (for premium estimation fallback)
SYMBOL_IV = {
    "NIFTY":        0.18,
    "BANKNIFTY":    0.20,
    "SENSEX":       0.18,
    "CRUDEOIL":     0.40,   # crude oil is very volatile
    "NATURALGAS":   0.60,   # natgas is extremely volatile
    "GOLD":         0.18,
}

# ─── Risk Parameters ──────────────────────────────────────────────────────────
MAX_CAPITAL_PER_TRADE  = float(os.getenv("MAX_CAPITAL_PER_TRADE", 100000))  # ₹
MAX_OPEN_POSITIONS     = int(os.getenv("MAX_OPEN_POSITIONS", 80))   # max 10 trades/day
STOP_LOSS_PCT          = float(os.getenv("STOP_LOSS_PCT", 5))
TARGET_PROFIT_PCT      = float(os.getenv("TARGET_PROFIT_PCT", 10))
MAX_DAILY_LOSS         = float(os.getenv("MAX_DAILY_LOSS", 1500))   # ₹ circuit breaker
TRAILING_STOP_PCT      = float(os.getenv("TRAILING_STOP_PCT", 1.0))

# Max lots allowed per symbol per trade
MAX_LOTS_PER_SYMBOL = {
    "NIFTY":        1,
    "BANKNIFTY":    1,
    "SENSEX":       2,
    "CRUDEOIL":     1,
    "NATURALGAS":   1,
    "GOLD":         1,
}

# MCX trading hours (extended session)
MCX_MARKET_OPEN  = "09:00"
MCX_MARKET_CLOSE = "23:25"   # MCX closes at 23:30, stop 5 min early

# ─── Strategy ─────────────────────────────────────────────────────────────────
# Upstox supports: 1minute, 30minute, day, week, month
# We fetch 1minute and resample to 5min internally
CANDLE_INTERVAL   = "1minute"
RESAMPLE_INTERVAL = "5min"      # pandas resample rule
LOOKBACK_CANDLES  = 200         # candles used to compute indicators (after resample)
ML_CONFIDENCE_THRESHOLD = 0.60  # minimum model confidence to place a trade

# Indicator settings
RSI_PERIOD    = 14
MACD_FAST     = 12
MACD_SLOW     = 26
MACD_SIGNAL   = 9
BB_PERIOD     = 20
BB_STD        = 2
EMA_SHORT     = 9
EMA_LONG      = 21

# ─── Scheduling ───────────────────────────────────────────────────────────────
MARKET_OPEN      = "09:15"
NSE_MARKET_CLOSE = "15:20"   # NSE F&O closes at 15:30
MARKET_CLOSE     = "23:20"   # bot shuts down after MCX evening session
SCAN_INTERVAL = 10      # seconds between full candle + ML analysis (5 min)
TICK_INTERVAL = 5        # seconds between real-time price polls (entry timing)

# After signal is confirmed, wait max this many minutes for a good entry before cancelling
SIGNAL_EXPIRY_MIN = 15

# Entry trigger: need this many consecutive ticks in signal direction
TICK_MOMENTUM_COUNT = 3

# Don't enter if price has moved more than this % from signal confirmation price
MAX_ENTRY_DRIFT_PCT = 0.5

# ─── Entry Quality Gate ───────────────────────────────────────────────────────
# No new entries in first 15 min (volatile open) or after 14:30 (decay risk)
ENTRY_START   = "09:30"
ENTRY_END     = "14:30"   # NSE F&O — no new entries after 14:30 (theta decay risk)

# Minimum confluence score out of 6 checks to allow entry
MIN_CONFLUENCE_SCORE = 3

# RSI bands – only enter when RSI shows momentum, not exhaustion
RSI_BUY_MIN  = 40    # RSI must be above this for BUY
RSI_BUY_MAX  = 75    # RSI must be below this for BUY (not extremely overbought)
RSI_SELL_MIN = 25    # RSI must be above this for SELL
RSI_SELL_MAX = 62    # RSI must be below this for SELL (showing weakness)

# ─── Logging ──────────────────────────────────────────────────────────────────
LOG_FILE  = "logs/trading_bot.log"
LOG_LEVEL = "INFO"

# ─── Database ─────────────────────────────────────────────────────────────────
DB_PATH = "data/trades.db"

# ─── Paper Trading ─────────────────────────────────────────────────────────────
PAPER_TRADE = os.getenv("PAPER_TRADE", "true").lower() == "true"
