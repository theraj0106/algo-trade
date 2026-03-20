"""
Notifier – sends trade alerts and daily summary via Telegram.
Silently skips if credentials are not configured.
"""

import logging
import requests

import config

logger = logging.getLogger(__name__)

BASE_URL = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage"


def _send(text: str) -> None:
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.debug("Telegram not configured – skipping notification")
        return
    payload = {"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    for attempt in range(2):          # 1 attempt + 1 retry
        try:
            resp = requests.post(BASE_URL, json=payload, timeout=10)
            resp.raise_for_status()
            return
        except Exception as e:
            if attempt == 0:
                logger.debug(f"Telegram attempt 1 failed, retrying: {e}")
            else:
                logger.warning(f"Telegram send failed: {e}")


def notify_signal_expired(underlying: str, signal: str, reason: str) -> None:
    _send(
        f"⏰ <b>Signal Expired</b>  [{underlying}]\n"
        f"Direction : {'Bullish ▲' if signal == 'BUY' else 'Bearish ▼'}\n"
        f"Reason    : {reason}"
    )


def notify_watching(underlying: str, signal: str, opt_type: str,
                    strike: float, conf: float, premium: float) -> None:
    """Sent on first scan – signal detected but not yet confirmed."""
    action = "CALL (CE)" if opt_type == "CE" else "PUT (PE)"
    msg = (
        f"👀 <b>Watching Signal</b>  [{underlying}]\n"
        f"Direction : {'Bullish ▲' if signal == 'BUY' else 'Bearish ▼'}\n"
        f"Option    : {action}  Strike ₹{strike:.0f}\n"
        f"Est. Premium : ₹{premium:.2f}  |  Conf {conf:.0%}\n"
        f"⏳ Waiting for next scan to confirm before buying..."
    )
    _send(msg)


def notify_entry(tradingsymbol: str, signal: str, qty: int,
                 entry: float, sl: float, tgt: float, conf: float,
                 underlying: str = "", opt_type: str = "", strike: float = 0,
                 confluence: int = 0, confluence_detail: str = "") -> None:
    mode   = "PAPER" if config.PAPER_TRADE else "LIVE"
    action = "BUY CALL (CE)" if opt_type == "CE" else ("BUY PUT (PE)" if opt_type == "PE" else signal)
    risk   = round((entry - sl) * qty, 2) if signal == "BUY" else round((sl - entry) * qty, 2)
    reward = round((tgt - entry) * qty, 2) if signal == "BUY" else round((entry - tgt) * qty, 2)
    rr     = f"{abs(reward/risk):.1f}R" if risk != 0 else "–"
    conf_bar = "🟩" * min(confluence, 7) + "⬜" * max(7 - confluence, 0)
    msg = (
        f"{'🟢' if signal == 'BUY' else '🔴'} <b>[{mode}] NEW TRADE</b>\n"
        f"Index     : <b>{underlying or tradingsymbol}</b>\n"
        f"Option    : <code>{tradingsymbol}</code>\n"
        f"Action    : <b>{action}</b>  (conf {conf:.0%})\n"
        f"Strike    : ₹{strike:.0f}\n"
        f"Premium   : ₹{entry:.2f}  ×  {qty} qty\n"
        f"Capital   : ₹{entry * qty:,.0f}\n"
        f"SL        : ₹{sl:.2f}  |  Target : ₹{tgt:.2f}\n"
        f"R:R       : {rr}\n"
        f"Analysis  : {conf_bar} ({confluence}/7)\n"
        f"<code>{confluence_detail}</code>"
    )
    _send(msg)


def notify_exit(tradingsymbol: str, exit_price: float,
                pnl: float, reason: str) -> None:
    emoji = "✅" if pnl >= 0 else "❌"
    mode  = "PAPER" if config.PAPER_TRADE else "LIVE"
    msg = (
        f"{emoji} <b>[{mode}] TRADE CLOSED</b>\n"
        f"Symbol : <code>{tradingsymbol}</code>\n"
        f"Exit   : ₹{exit_price:.2f}\n"
        f"PnL    : ₹{pnl:+.2f}\n"
        f"Reason : {reason}"
    )
    _send(msg)


def notify_daily_summary(total: int, winners: int, total_pnl: float) -> None:
    win_rate = (winners / total * 100) if total else 0
    emoji = "📈" if total_pnl >= 0 else "📉"
    msg = (
        f"{emoji} <b>Daily Summary</b>\n"
        f"Trades  : {total}  |  Winners: {winners} ({win_rate:.0f}%)\n"
        f"Net PnL : ₹{total_pnl:+.2f}"
    )
    _send(msg)


# def notify_risk_violation(message: str) -> None:
#     _send(f"⚠️ <b>Risk Alert</b>\n{message}")


def notify_error(message: str) -> None:
    _send(f"🔴 <b>Bot Error</b>\n{message}")


def notify_startup(paper: bool) -> None:
    mode = "PAPER TRADE" if paper else "LIVE TRADE"
    watchlist = ", ".join(config.WATCHLIST)
    nse_syms = [s for s in config.WATCHLIST if s not in config.UPSTOX_COMMODITY_KEYS]
    mcx_syms = [s for s in config.WATCHLIST if s in config.UPSTOX_COMMODITY_KEYS]
    msg = (
        f"🚀 <b>AI F&O AlgoBot Started  [{mode}] 🚀 </b>\n"
        f"NSE/BSE  : <code>{', '.join(nse_syms)}</code>\n"
        f"MCX      : <code>{', '.join(mcx_syms)}</code>\n"
        f"Scan every : {config.SCAN_INTERVAL}s candle  |  {config.TICK_INTERVAL}s tick\n"
        f"Capital/trade : ₹{config.MAX_CAPITAL_PER_TRADE:,.0f}\n"
        f"SL : {config.STOP_LOSS_PCT}%  |  Target : {config.TARGET_PROFIT_PCT}%\n"
        f"Max positions : {config.MAX_OPEN_POSITIONS}"
    )
    _send(msg)


# def notify_scan_result(signals_found: int, skipped: list) -> None:
#     """Called after each scan cycle with a brief summary."""
#     if signals_found == 0 and not skipped:
#         return   # silent when nothing to report
#     if signals_found > 0:
#         lines = [f"🔍 <b>Scan</b>  |  Signals found: {signals_found}"]
#         _send("\n".join(lines))


def notify_no_trade(minutes: int = 30) -> None:
    """Sent periodically when no trade has been executed in the last `minutes` minutes."""
    _send(
        f"🔍 <b>No Trade in Last {minutes} Min</b>\n"
        f"No entry was triggered in the last {minutes} minutes.\n"
        f"Bot is actively scanning the market — standing by for the next signal."
    )


def notify_position_update(symbol: str, opt_type: str, strike: float,
                            entry: float, ltp: float, pnl: float, sl: float, tgt: float) -> None:
    """Periodic update for an open position."""
    arrow = "▲" if ltp >= entry else "▼"
    pnl_sign = "+" if pnl >= 0 else ""
    msg = (
        f"📊 <b>Position Update</b>\n"
        f"<code>{symbol} {opt_type} {strike:.0f}</code>\n"
        f"Entry ₹{entry:.2f}  →  LTP ₹{ltp:.2f} {arrow}\n"
        f"PnL : <b>₹{pnl_sign}{pnl:.2f}</b>\n"
        f"SL ₹{sl:.2f}  |  Target ₹{tgt:.2f}"
    )
    _send(msg)
