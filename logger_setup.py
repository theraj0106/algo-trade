"""
Advanced Logging System for AI F&O Trading Bot
=============================================

Features:
- Daily rotating logs (midnight)
- Separate logs: system, trades, errors
- Trade lifecycle tracking (entry, exit, duration)
- No duplicate handlers
- Safe for multi-module usage
"""

import logging
import os
import sys
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler

# ─── Setup directories ───────────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)

# ─── Active Trades Store ─────────────────────────────────────────────────────
ACTIVE_TRADES = {}


# ─── Handler Creator ─────────────────────────────────────────────────────────
def _create_handler(filename, level=logging.INFO):
    handler = TimedRotatingFileHandler(
        f"logs/{filename}",
        when="midnight",
        interval=1,
        backupCount=10,
        encoding="utf-8"
    )
    handler.suffix = "%Y-%m-%d"

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )
    handler.setFormatter(formatter)
    handler.setLevel(level)

    return handler


# ─── Logger Setup ────────────────────────────────────────────────────────────
def setup_loggers():
    # Prevent duplicate handlers
    if logging.getLogger("system").handlers:
        return (
            logging.getLogger("system"),
            logging.getLogger("trade"),
            logging.getLogger("error")
        )

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s"
    ))

    # System logger
    system_logger = logging.getLogger("system")
    system_logger.setLevel(logging.INFO)
    system_logger.addHandler(console)
    system_logger.addHandler(_create_handler("system.log"))

    # Trade logger
    trade_logger = logging.getLogger("trade")
    trade_logger.setLevel(logging.INFO)
    trade_logger.addHandler(_create_handler("trades.log"))

    # Error logger
    error_logger = logging.getLogger("error")
    error_logger.setLevel(logging.ERROR)
    error_logger.addHandler(_create_handler("errors.log", logging.ERROR))

    return system_logger, trade_logger, error_logger


# Initialize loggers
logger, trade_logger, error_logger = setup_loggers()


# ─── Trade Lifecycle Logging ─────────────────────────────────────────────────

def log_trade_entry(trade_id, symbol, side, price, qty):
    """Call this when trade is placed"""
    entry_time = datetime.now()

    ACTIVE_TRADES[trade_id] = {
        "symbol": symbol,
        "side": side,
        "entry_price": price,
        "entry_time": entry_time,
        "quantity": qty
    }

    trade_logger.info(
        f"ENTRY | ID={trade_id} | {symbol} | {side} | "
        f"Price={price} | Qty={qty} | Time={entry_time}"
    )


def log_trade_exit(trade_id, exit_price, pnl):
    """Call this when trade is exited"""
    trade = ACTIVE_TRADES.get(trade_id)

    if not trade:
        return

    exit_time = datetime.now()
    duration = exit_time - trade["entry_time"]
    duration_str = str(duration).split(".")[0]

    # EXIT log
    trade_logger.info(
        f"EXIT | ID={trade_id} | {trade['symbol']} | "
        f"ExitPrice={exit_price} | PnL={pnl} | Time={exit_time}"
    )

    # FULL trade lifecycle log
    trade_logger.info(
        f"TRADE | ID={trade_id} | {trade['symbol']} | {trade['side']} | "
        f"Entry={trade['entry_price']} @ {trade['entry_time']} | "
        f"Exit={exit_price} @ {exit_time} | "
        f"PnL={pnl} | Duration={duration_str}"
    )

    # Remove after exit
    ACTIVE_TRADES.pop(trade_id, None)


# ─── Helper Functions ─────────────────────────────────────────────────────────

def log_info(msg):
    logger.info(msg)


def log_error(msg):
    error_logger.error(msg, exc_info=True)