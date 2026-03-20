"""
AI F&O Trading Bot – Main Orchestrator
=======================================
Two independent loops:

  SLOW LOOP  (every 5 min)
    – Fetches 5-min candles, runs ML model + 6-point confluence check
    – On first signal  → WATCHING state  (Telegram alert)
    – On second signal → CONFIRMED state (fast loop takes over for entry)

  FAST LOOP  (every 10 sec)
    – For each CONFIRMED signal, polls the live index price
    – Tracks last N price ticks in a rolling buffer
    – Fires entry ONLY when real-time movement confirms the direction:
        BUY  → price dips slightly then 3 consecutive ticks UP   (pullback + momentum)
        SELL → price bounces slightly then 3 consecutive ticks DOWN
    – Cancels signal if price drifts > 0.5% from confirmation price
    – Cancels signal if not triggered within 15 minutes

    Also runs position monitor (SL / target / trailing stop) every tick.
"""

import logging
import os
import sys
import time
from collections import deque
from datetime import datetime, timedelta
from typing import Optional

import config
import notifier
from data_fetcher import fetch_ohlcv, fetch_ltp, fetch_option_ltp
from order_manager import place_entry, monitor_positions, square_off_all
from predictor import train_model
from risk_manager import check_new_trade, daily_summary, RiskViolation
from strategy import analyse, TradeSignal
from logger_setup import log_trade_entry, log_error
import io

# Force UTF-8 output (CRITICAL FIX)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
# ─── Logging ──────────────────────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
os.makedirs("data", exist_ok=True)

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(config.LOG_FILE),
    ],
)
logger = logging.getLogger("main")


# ─── Signal State Machine ─────────────────────────────────────────────────────
#
# Each symbol can be in one of three states:
#   _pending   → first scan detected a signal, waiting for 2nd scan to confirm
#   _confirmed → signal confirmed, fast loop hunting for optimal entry
#
# _pending  : { symbol: {"signal": str, "obj": TradeSignal} }
# _confirmed: { symbol: {"signal": str, "obj": TradeSignal,
#                         "ref_price": float,   ← index price at confirmation
#                         "expires_at": datetime,
#                         "ticks": deque } }

_pending:   dict = {}
_confirmed: dict = {}

# ── No-trade alert state (set properly in main()) ─────────────────────────────
last_trade_time     = 0.0
last_no_trade_alert = 0.0


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _now_str() -> str:
    return datetime.now().strftime("%H:%M")


def _is_market_open() -> bool:
    """True if NSE or MCX session is active."""
    now = _now_str()
    nse_open = config.MARKET_OPEN <= now < config.MARKET_CLOSE
    mcx_open = config.MCX_MARKET_OPEN <= now < config.MCX_MARKET_CLOSE
    return nse_open or mcx_open


def _nse_open() -> bool:
    now = _now_str()
    return config.MARKET_OPEN <= now < config.NSE_MARKET_CLOSE


def _mcx_open() -> bool:
    now = _now_str()
    return config.MCX_MARKET_OPEN <= now < config.MCX_MARKET_CLOSE


def _wait_for_market_open() -> None:
    while not _is_market_open():
        logger.info(f"Market closed ({_now_str()}). Waiting for {config.MARKET_OPEN}...")
        time.sleep(60)


def _train_on_startup() -> None:
    import pandas as pd
    logger.info("Training ML model on historical data...")
    frames = []
    for sym in config.WATCHLIST:
        try:
            df = fetch_ohlcv(sym, lookback=500)
            frames.append(df)
            logger.info(f"  Loaded {len(df)} candles for {sym}")
        except Exception as e:
            logger.warning(f"  Could not load {sym}: {e}")
    if frames:
        train_model(pd.concat(frames))
        logger.info("Model training complete.")
    else:
        logger.warning("No data for training – rule-based fallback will be used.")


# ─── Entry Trigger Logic ──────────────────────────────────────────────────────

def _check_momentum_trigger(ticks: deque, signal: str) -> bool:
    """
    Returns True when the price buffer shows real movement in the signal direction.

    BUY  pattern: any dip in the buffer followed by TICK_MOMENTUM_COUNT
                  consecutive rising prices at the end  (pullback → resumption)
    SELL pattern: any bounce followed by TICK_MOMENTUM_COUNT consecutive falls.

    A plain run of TICK_MOMENTUM_COUNT ticks in the right direction also qualifies
    (momentum entry, no dip needed).
    """
    n = config.TICK_MOMENTUM_COUNT
    if len(ticks) < n + 1:
        return False

    prices = list(ticks)

    if signal == "BUY":
        # Last N ticks all rising
        tail = prices[-n:]
        if all(tail[i] < tail[i + 1] for i in range(len(tail) - 1)):
            return True
        # Dip-then-rise: at some point before the tail the price was lower
        if len(prices) >= n + 2:
            pre_tail_low  = min(prices[-(n + 2):-n])
            if pre_tail_low < prices[-n] and tail[-1] > tail[0]:
                return True

    else:  # SELL
        tail = prices[-n:]
        if all(tail[i] > tail[i + 1] for i in range(len(tail) - 1)):
            return True
        if len(prices) >= n + 2:
            pre_tail_high = max(prices[-(n + 2):-n])
            if pre_tail_high > prices[-n] and tail[-1] < tail[0]:
                return True

    return False


def _execute_confirmed_signal(symbol: str, entry: dict) -> None:
    """Place the trade for a confirmed + triggered signal."""
    signal: TradeSignal = entry["obj"]
    try:
        try:
            from strategy import _estimate_premium, _strike_step
            live_spot = fetch_ltp(symbol)
            step      = _strike_step(symbol)
            atm       = round(live_spot / step) * step

            if signal.instrument_type == "CE":
                live_strike = atm + step
            else:
                live_strike = atm - step

            live_premium = fetch_option_ltp(symbol, live_strike, signal.instrument_type)
            if live_premium is None:
                live_premium = _estimate_premium(live_spot, live_strike, signal.instrument_type, symbol)
                logger.info(f"{symbol}: using estimated premium ₹{live_premium:.2f} (option LTP unavailable)")
            else:
                logger.info(f"{symbol}: live option LTP ₹{live_premium:.2f} for {live_strike:.0f}{signal.instrument_type}")

            if live_premium > 0:
                signal.entry_spot    = live_spot
                signal.strike        = live_strike
                signal.entry_price   = live_premium
                signal.stop_loss     = round(live_premium * (1 - config.STOP_LOSS_PCT  / 100), 2)
                signal.target        = round(live_premium * (1 + config.TARGET_PROFIT_PCT / 100), 2)
                signal.tradingsymbol = f"{symbol}{live_strike:.0f}{signal.instrument_type}-PAPER"

        except Exception as e:
            logger.warning(f"Could not refresh live price for {symbol}: {e}")

        check_new_trade(signal)
        trade_id = place_entry(signal)

        if trade_id:
            # ✅ ENTRY LOG ADDED
            log_trade_entry(
                trade_id,
                symbol,
                signal.signal,
                signal.entry_price,
                signal.quantity
            )

            notifier.notify_entry(
                tradingsymbol=signal.tradingsymbol,
                signal=signal.signal,
                qty=signal.quantity,
                entry=signal.entry_price,
                sl=signal.stop_loss,
                tgt=signal.target,
                conf=signal.confidence,
                underlying=signal.underlying,
                opt_type=signal.instrument_type,
                strike=signal.strike,
                confluence=signal.confluence_score,
                confluence_detail=signal.confluence_detail,
            )

            logger.info(f"[ENTRY] {symbol} {signal.signal} → trade placed")

            # Reset the no-trade timer so the 30-min alert won't fire
            # immediately after a real entry
            global last_trade_time, last_no_trade_alert
            last_trade_time     = time.time()
            last_no_trade_alert = time.time()

    except RiskViolation as e:
        logger.warning(f"Risk block {symbol}: {e}")

    except Exception as e:
        # ✅ replaced error logger
        log_error(f"Entry failed {symbol}: {e}")
        notifier.notify_error(f"Entry error {symbol}: {e}")

# ─── Slow Loop: Candle Analysis (every 5 min) ─────────────────────────────────

def run_candle_scan() -> None:
    """
    Full ML + confluence analysis on 5-min candles.
    Manages the IDLE → WATCHING → CONFIRMED state transitions.
    """
    global _pending, _confirmed
    signals_found = 0
    skipped = []

    for symbol in config.WATCHLIST:
        # Skip if already confirmed and fast loop is handling it
        if symbol in _confirmed:
            skipped.append(f"{symbol}(waiting-entry)")
            continue

        # Only scan MCX symbols during MCX hours, NSE symbols during NSE hours
        is_mcx = symbol in config.UPSTOX_COMMODITY_KEYS
        if is_mcx and not _mcx_open():
            skipped.append(f"{symbol}(mcx-closed)")
            continue
        if not is_mcx and not _nse_open():
            skipped.append(f"{symbol}(nse-closed)")
            continue

        try:
            signal = analyse(symbol)

            if signal is None:
                if symbol in _pending:
                    logger.info(f"{symbol}: watching signal cleared – conditions changed")
                    _pending.pop(symbol, None)
                skipped.append(symbol)
                continue

            prev = _pending.get(symbol)

            if prev is None or prev["signal"] != signal.signal:
                if config.PAPER_TRADE:
                    # ── PAPER MODE: confirm on first scan, no 2nd scan needed ─
                    logger.info(
                        f"{symbol}: signal {signal.signal} CONFIRMED (paper, single-scan) "
                        f"conf={signal.confidence:.0%}  confluence={signal.confluence_score}/6"
                    )
                    # fall through to CONFIRMED block below
                else:
                    # ── LIVE MODE: WATCHING – wait for 2nd scan confirmation ──
                    _pending[symbol] = {"signal": signal.signal, "obj": signal}
                    logger.info(
                        f"{symbol}: signal {signal.signal} WATCHING "
                        f"conf={signal.confidence:.0%}  confluence={signal.confluence_score}/6"
                    )
                    notifier.notify_watching(
                        underlying=signal.underlying,
                        signal=signal.signal,
                        opt_type=signal.instrument_type,
                        strike=signal.strike,
                        conf=signal.confidence,
                        premium=signal.entry_price,
                    )
                    skipped.append(f"{symbol}(watching)")
                    continue

            # ── CONFIRMED ─────────────────────────────────────────────────────
            _pending.pop(symbol, None)
            try:
                ref_price = fetch_ltp(symbol)
            except Exception:
                ref_price = signal.entry_spot or signal.entry_price

            _confirmed[symbol] = {
                "signal":     signal.signal,
                "obj":        signal,
                "ref_price":  ref_price,
                "expires_at": datetime.now() + timedelta(minutes=config.SIGNAL_EXPIRY_MIN),
                "ticks":      deque(maxlen=20),
            }
            signals_found += 1
            logger.info(
                f"{symbol}: signal {signal.signal} CONFIRMED – "
                f"fast loop hunting entry  ref_price={ref_price:.2f}"
            )

        except Exception as e:
            logger.error(f"Scan error {symbol}: {e}", exc_info=True)
            notifier.notify_error(f"Scan error {symbol}: {e}")

    # notifier.notify_scan_result(signals_found, skipped)


# ─── Fast Loop: Real-time Entry Timing (every 10 sec) ─────────────────────────

def run_tick_check() -> None:
    """
    For every CONFIRMED signal:
      1. Fetch latest index price
      2. Check if price has drifted too far (cancel) or signal expired (cancel)
      3. Check momentum trigger → fire entry if conditions met
    Also runs position monitor.
    """
    global _confirmed

    to_remove = []

    for symbol, entry in list(_confirmed.items()):
        # ── Expiry check ──────────────────────────────────────────────────────
        if datetime.now() >= entry["expires_at"]:
            logger.info(f"{symbol}: confirmed signal expired – no good entry found")
            notifier.notify_signal_expired(
                symbol, entry["signal"],
                f"No momentum trigger in {config.SIGNAL_EXPIRY_MIN} min"
            )
            to_remove.append(symbol)
            continue

        # ── Fetch price ───────────────────────────────────────────────────────
        try:
            price = fetch_ltp(symbol)
        except Exception as e:
            logger.warning(f"LTP fetch failed {symbol}: {e}")
            continue

        entry["ticks"].append(price)
        ticks  = entry["ticks"]
        sig    = entry["signal"]
        ref    = entry["ref_price"]
        drift  = abs(price - ref) / ref * 100

        logger.debug(
            f"[TICK] {symbol}  price={price:.2f}  ref={ref:.2f}  "
            f"drift={drift:.2f}%  ticks={len(ticks)}"
        )

        # ── Drift guard: price moved too far → cancel, don't chase ────────────
        if drift > config.MAX_ENTRY_DRIFT_PCT:
            direction = "up" if price > ref else "down"
            logger.info(
                f"{symbol}: signal cancelled – price drifted {drift:.2f}% {direction} "
                f"(max {config.MAX_ENTRY_DRIFT_PCT}%)"
            )
            notifier.notify_signal_expired(
                symbol, sig,
                f"Price drifted {drift:.2f}% {direction} from ₹{ref:.2f} – not chasing"
            )
            to_remove.append(symbol)
            continue

        # ── Momentum trigger ──────────────────────────────────────────────────
        # In paper-trade mode yfinance prices update every ~1 min (not every tick),
        # so skip the tick pattern and enter as soon as we have a stable price.
        if config.PAPER_TRADE:
            if len(ticks) >= 2:   # waited at least one 10-sec poll
                logger.info(f"{symbol}: PAPER mode – entering after price confirmation  price={price:.2f}")
                _execute_confirmed_signal(symbol, entry)
                to_remove.append(symbol)
        elif _check_momentum_trigger(ticks, sig):
            logger.info(f"{symbol}: momentum trigger FIRED  price={price:.2f}  signal={sig}")
            _execute_confirmed_signal(symbol, entry)
            to_remove.append(symbol)

    for sym in to_remove:
        _confirmed.pop(sym, None)

    # ── Monitor open positions ─────────────────────────────────────────────────
    try:
        monitor_positions(fetch_ltp)
    except Exception as e:
        logger.error(f"Position monitor error: {e}", exc_info=True)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    logger.info("=" * 60)
    logger.info(f"  AI F&O Trading Bot  |  {'PAPER' if config.PAPER_TRADE else 'LIVE'}")
    logger.info("=" * 60)

    if config.PAPER_TRADE:
        logger.info("Running in PAPER TRADE mode – no real orders will be placed")
    else:
        logger.warning("LIVE TRADING MODE – real orders will be placed!")

    notifier.notify_startup(config.PAPER_TRADE)
    _train_on_startup()
    _wait_for_market_open()

    logger.info(
        f"Market open. "
        f"Slow scan every {config.SCAN_INTERVAL}s  |  "
        f"Tick check every {config.TICK_INTERVAL}s"
    )

    global last_trade_time, last_no_trade_alert
    last_candle_scan     = 0.0
    last_trade_time      = time.time()
    last_no_trade_alert  = 0.0            # fire first alert after 1 min from start
    NO_TRADE_ALERT_SECS  = 1 * 60        # 1 minute

    try:
        while _is_market_open():
            now = time.time()

            # ── Slow loop: full candle analysis ───────────────────────────────
            if now - last_candle_scan >= config.SCAN_INTERVAL:
                run_candle_scan()
                last_candle_scan = now
            else:
                secs_left = int(config.SCAN_INTERVAL - (now - last_candle_scan))
                pending_syms = list(_pending.keys())
                confirmed_syms = list(_confirmed.keys())
                if pending_syms or confirmed_syms:
                    logger.info(
                        f"Next scan in {secs_left}s  |  "
                        f"Watching: {pending_syms}  Confirmed: {confirmed_syms}"
                    )

            # ── Fast loop: real-time entry timing + position monitor ──────────
            run_tick_check()

            # ── 1-min no-trade Telegram alert ────────────────────────────────
            if (now - last_trade_time    >= NO_TRADE_ALERT_SECS and
                    now - last_no_trade_alert >= NO_TRADE_ALERT_SECS):
                notifier.notify_no_trade(minutes=1)
                last_no_trade_alert = now

            time.sleep(config.TICK_INTERVAL)

    except KeyboardInterrupt:
        logger.info("Interrupted by user.")

    finally:
        logger.info("Squaring off all open positions...")
        square_off_all(fetch_ltp)

        summary = daily_summary()
        logger.info(
            f"Daily Summary: trades={summary.get('total', 0)}  "
            f"winners={summary.get('winners', 0)}  "
            f"PnL=₹{summary.get('total_pnl', 0):.2f}"
        )
        notifier.notify_daily_summary(
            total=summary.get("total", 0),
            winners=summary.get("winners", 0),
            total_pnl=summary.get("total_pnl", 0.0),
        )
        logger.info("Bot stopped.")


if __name__ == "__main__":
    main()
