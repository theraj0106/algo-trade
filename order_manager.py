"""
Order Manager – places, tracks, and exits orders via Upstox API v2.
In PAPER_TRADE mode all orders are simulated locally (REALISTIC MODE ENABLED).
"""

import logging
import time
import random
from typing import Optional

import config
import notifier
from risk_manager import record_entry, record_exit, update_trailing_stop, get_open_trades
from strategy import TradeSignal
from logger_setup import log_trade_exit, log_error   # ✅ FIXED IMPORT

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# UPSTOX API
# ─────────────────────────────────────────────

def _get_upstox_order_api():
    import upstox_client
    configuration = upstox_client.Configuration()
    configuration.access_token = config.UPSTOX_ACCESS_TOKEN
    return upstox_client.OrderApi(upstox_client.ApiClient(configuration))


# ─────────────────────────────────────────────
# ENTRY
# ─────────────────────────────────────────────

def place_entry(signal: TradeSignal) -> Optional[int]:
    if config.PAPER_TRADE:
        return _paper_entry(signal)
    return _live_entry(signal)


# 🔥 REALISTIC PAPER ENTRY (MERGED)
def _paper_entry(signal: TradeSignal) -> int:

    ideal_price = signal.entry_price

    # ⏳ delay simulation
    delay = random.uniform(0.5, 1.5)
    time.sleep(delay)

    # spread + slippage
    spread_pct = random.uniform(0.005, 0.015)
    slippage_pct = random.uniform(0.002, 0.01)

    executed_price = ideal_price * (1 + spread_pct + slippage_pct)
    executed_price = round(executed_price, 2)

    signal.entry_price = executed_price

    trade_id = record_entry(signal)

    logger.info(
        f"[PAPER-REAL] ENTRY {signal.signal} {signal.tradingsymbol} "
        f"ideal={ideal_price:.2f} → exec={executed_price:.2f} "
        f"SL={signal.stop_loss:.2f} TGT={signal.target:.2f}"
    )

    return trade_id


# ─────────────────────────────────────────────
# LIVE ENTRY (UNCHANGED)
# ─────────────────────────────────────────────

def _live_entry(signal: TradeSignal) -> Optional[int]:
    import upstox_client

    api = _get_upstox_order_api()
    txn_type = "BUY" if signal.signal == "BUY" else "SELL"

    body = upstox_client.PlaceOrderRequest(
        quantity=signal.quantity,
        product="I",
        validity="DAY",
        price=0,
        instrument_token=signal.tradingsymbol,
        order_type="MARKET",
        transaction_type=txn_type,
        disclosed_quantity=0,
        trigger_price=0,
        is_amo=False,
        tag="AiTradingBot",
    )

    try:
        resp = api.place_order(body, "2.0")
        order_id = resp.data.order_id

        fill_price = _wait_for_fill(api, order_id)
        if fill_price is None:
            return None

        signal.entry_price = fill_price
        trade_id = record_entry(signal)

        _place_sl_order(api, signal)

        return trade_id

    except Exception as e:
        log_error(f"Entry failed: {e}")
        return None


def _wait_for_fill(api, order_id: str, timeout: int = 15) -> Optional[float]:
    for _ in range(timeout):
        time.sleep(1)
        try:
            orders = api.get_order_details(order_id=order_id, api_version="2.0")
            if orders.data.status == "complete":
                return float(orders.data.average_price)
        except Exception:
            pass
    return None


def _place_sl_order(api, signal: TradeSignal) -> None:
    import upstox_client

    sl_txn = "SELL" if signal.signal == "BUY" else "BUY"

    body = upstox_client.PlaceOrderRequest(
        quantity=signal.quantity,
        product="I",
        validity="DAY",
        price=signal.stop_loss,
        instrument_token=signal.tradingsymbol,
        order_type="SL",
        transaction_type=sl_txn,
        trigger_price=signal.stop_loss,
        disclosed_quantity=0,
        is_amo=False,
        tag="AiTradingBot_SL",
    )

    try:
        api.place_order(body, "2.0")
    except Exception as e:
        logger.warning(f"SL order failed: {e}")


# ─────────────────────────────────────────────
# EXIT
# ─────────────────────────────────────────────

_REASON_LABEL = {
    "stop_loss": "Stop Loss hit",
    "trailing_stop": "Trailing Stop hit",
    "target": "Target reached",
    "eod_squareoff": "End of day square-off",
    "manual": "Manual exit",
}


def place_exit(trade_id: int, tradingsymbol: str, quantity: int,
               exchange: str, current_price: float,
               original_signal: str, reason: str = "manual") -> float:

    if config.PAPER_TRADE:
        return _paper_exit(trade_id, tradingsymbol, current_price, reason)

    return _live_exit(trade_id, tradingsymbol, quantity,
                      current_price, original_signal, reason)


# 🔥 REALISTIC PAPER EXIT (MERGED)
def _paper_exit(trade_id: int, tradingsymbol: str,
                current_price: float, reason: str) -> float:

    spread_pct = random.uniform(0.005, 0.015)
    slippage_pct = random.uniform(0.003, 0.012)

    executed_price = current_price * (1 - spread_pct - slippage_pct)
    executed_price = round(executed_price, 2)

    pnl = record_exit(trade_id, executed_price, reason)

    # ✅ EXIT LOG ADDED
    log_trade_exit(trade_id, executed_price, pnl)

    logger.info(
        f"[PAPER-REAL] EXIT {tradingsymbol} "
        f"market={current_price:.2f} → exec={executed_price:.2f} "
        f"PnL=₹{pnl:.2f} [{reason}]"
    )

    notifier.notify_exit(
        tradingsymbol=tradingsymbol,
        exit_price=executed_price,
        pnl=pnl,
        reason=_REASON_LABEL.get(reason, reason),
    )

    return pnl


# ─────────────────────────────────────────────
# LIVE EXIT (UNCHANGED)
# ─────────────────────────────────────────────

def _live_exit(trade_id: int, tradingsymbol: str, quantity: int,
               current_price: float, original_signal: str, reason: str) -> float:

    import upstox_client

    api = _get_upstox_order_api()
    exit_txn = "SELL" if original_signal == "BUY" else "BUY"

    body = upstox_client.PlaceOrderRequest(
        quantity=quantity,
        product="I",
        validity="DAY",
        price=0,
        instrument_token=tradingsymbol,
        order_type="MARKET",
        transaction_type=exit_txn,
        disclosed_quantity=0,
        trigger_price=0,
        is_amo=False,
        tag="AiTradingBot_EXIT",
    )

    try:
        resp = api.place_order(body, "2.0")
        order_id = resp.data.order_id

        fill_price = _wait_for_fill(api, order_id) or current_price

        pnl = record_exit(trade_id, fill_price, reason)

        # ✅ EXIT LOG ADDED
        log_trade_exit(trade_id, fill_price, pnl)

        notifier.notify_exit(
            tradingsymbol=tradingsymbol,
            exit_price=fill_price,
            pnl=pnl,
            reason=_REASON_LABEL.get(reason, reason),
        )

        return pnl

    except Exception as e:
        log_error(f"Exit failed: {e}")
        return 0.0


# ─────────────────────────────────────────────
# POSITION MONITOR (UNCHANGED)
# ─────────────────────────────────────────────

def _estimate_option_ltp(trade: dict, current_spot: float) -> float:
    inst = trade.get("instrument_type", "FUT")
    entry_premium = trade["entry_price"]
    entry_spot = trade.get("entry_spot", 0)

    if inst not in ("CE", "PE") or entry_spot == 0:
        return current_spot

    delta = 0.5 if inst == "CE" else -0.5
    return max(entry_premium + delta * (current_spot - entry_spot), 0.05)


def monitor_positions(get_price_fn) -> None:

    for trade in get_open_trades():
        trade_id = trade["id"]
        symbol = trade["symbol"]
        ts = trade["tradingsymbol"]
        qty = trade["quantity"]
        sig = trade["signal"]
        tgt = trade["target"]

        try:
            spot = get_price_fn(symbol)
        except Exception:
            continue

        ltp = _estimate_option_ltp(trade, spot)
        entry = trade["entry_price"]
        sl = trade["stop_loss"]

        profit_pct = (ltp - entry) / entry * 100

        if profit_pct >= 3:
            update_trailing_stop(trade_id, ltp)

        if ltp <= sl:
            place_exit(trade_id, ts, qty, "NFO", sl, sig, "stop_loss")
        elif ltp >= tgt:
            place_exit(trade_id, ts, qty, "NFO", tgt, sig, "target")


def square_off_all(get_price_fn) -> None:
    for trade in get_open_trades():
        try:
            spot = get_price_fn(trade["symbol"])
            exit_price = _estimate_option_ltp(trade, spot)
        except Exception:
            exit_price = trade["entry_price"]

        place_exit(
            trade["id"], trade["tradingsymbol"],
            trade["quantity"], "NFO",
            exit_price, trade["signal"], "eod_squareoff"
        )

    logger.info("All positions squared off")