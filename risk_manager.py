"""
Risk Manager – enforces position limits, daily loss limits,
trailing stops, and decides whether a new trade is allowed.
"""

import logging
import sqlite3
from datetime import date, datetime
from typing import List, Optional

import config
from strategy import TradeSignal

logger = logging.getLogger(__name__)


def _db():
    import os
    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    _init_db(conn)
    return conn


def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            date            TEXT,
            symbol          TEXT,
            tradingsymbol   TEXT,
            signal          TEXT,
            instrument_type TEXT DEFAULT 'FUT',
            strike          REAL DEFAULT 0,
            quantity        INTEGER,
            entry_price     REAL,
            entry_spot      REAL DEFAULT 0,
            exit_price      REAL DEFAULT 0,
            stop_loss       REAL,
            target          REAL,
            highest_price   REAL DEFAULT 0,
            status          TEXT DEFAULT 'OPEN',
            pnl             REAL DEFAULT 0,
            paper           INTEGER DEFAULT 1,
            created_at      TEXT
        )
    """)
    # Migrate existing DB – add new columns if absent
    for col, defn in [
        ("instrument_type", "TEXT DEFAULT 'FUT'"),
        ("strike",          "REAL DEFAULT 0"),
        ("entry_spot",      "REAL DEFAULT 0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {defn}")
            conn.commit()
        except Exception:
            pass  # column already exists
    conn.commit()


# ─── Guards ───────────────────────────────────────────────────────────────────

class RiskViolation(Exception):
    pass


def check_new_trade(signal: TradeSignal) -> None:
    """
    Raises RiskViolation if the trade should NOT be placed.
    Call this before placing any order.
    """
    conn = _db()
    today = str(date.today())

    # 1. Max trades per day (counts both open and closed)
    total_today = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE date=?", (today,)
    ).fetchone()[0]
    if total_today >= config.MAX_OPEN_POSITIONS:
        raise RiskViolation(
            f"Max trades for today ({config.MAX_OPEN_POSITIONS}) reached"
        )

    # 2. Daily loss circuit-breaker
    daily_pnl = conn.execute(
        "SELECT COALESCE(SUM(pnl),0) FROM trades WHERE date=?", (today,)
    ).fetchone()[0]
    if daily_pnl <= -config.MAX_DAILY_LOSS:
        raise RiskViolation(
            f"Daily loss limit ₹{config.MAX_DAILY_LOSS} hit  (PnL={daily_pnl:.0f})"
        )

    # 3. No duplicate open position on same symbol
    dup = conn.execute(
        "SELECT id FROM trades WHERE status='OPEN' AND symbol=? AND date=?",
        (signal.underlying, today),
    ).fetchone()
    if dup:
        raise RiskViolation(f"Already have an open trade for {signal.underlying}")

    conn.close()


# ─── Trade Lifecycle ──────────────────────────────────────────────────────────

def record_entry(signal: TradeSignal) -> int:
    """Persist a new trade entry. Returns trade id."""
    conn = _db()
    cursor = conn.execute(
        """INSERT INTO trades
           (date, symbol, tradingsymbol, signal, instrument_type, strike,
            quantity, entry_price, entry_spot,
            stop_loss, target, highest_price, status, paper, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            str(date.today()),
            signal.underlying,
            signal.tradingsymbol,
            signal.signal,
            signal.instrument_type,
            signal.strike,
            signal.quantity,
            signal.entry_price,
            signal.entry_spot,
            signal.stop_loss,
            signal.target,
            signal.entry_price,
            "OPEN",
            int(config.PAPER_TRADE),
            datetime.now().isoformat(),
        ),
    )
    conn.commit()
    trade_id = cursor.lastrowid
    conn.close()
    logger.info(f"Recorded entry  trade_id={trade_id}  {signal.tradingsymbol}  @{signal.entry_price}")
    return trade_id


def record_exit(trade_id: int, exit_price: float, reason: str = "") -> float:
    """Update trade with exit price; return realised PnL."""
    conn = _db()
    row = conn.execute(
        "SELECT signal, entry_price, quantity FROM trades WHERE id=?", (trade_id,)
    ).fetchone()
    if row is None:
        conn.close()
        return 0.0

    direction = 1 if row["signal"] == "BUY" else -1
    pnl = direction * (exit_price - row["entry_price"]) * row["quantity"]

    conn.execute(
        """UPDATE trades SET status='CLOSED', exit_price=?, pnl=?
           WHERE id=?""",
        (exit_price, pnl, trade_id),
    )
    conn.commit()
    conn.close()
    logger.info(f"Trade {trade_id} closed  exit={exit_price}  PnL=₹{pnl:.2f}  [{reason}]")
    return pnl


# ─── Trailing Stop ────────────────────────────────────────────────────────────

def update_trailing_stop(trade_id: int, current_price: float) -> Optional[float]:
    """
    Raise stop-loss as price moves in our favour.
    For options (CE/PE) we always trail upward (we always buy options).
    Returns the updated stop-loss (or None if unchanged).
    """
    conn = _db()
    row = conn.execute(
        "SELECT signal, instrument_type, highest_price, stop_loss FROM trades WHERE id=? AND status='OPEN'",
        (trade_id,),
    ).fetchone()
    if row is None:
        conn.close()
        return None

    highest = row["highest_price"]
    old_sl  = row["stop_loss"]
    # Options are always bought → trail upward regardless of signal direction
    is_long = (row["signal"] == "BUY") or (row["instrument_type"] in ("CE", "PE"))

    if is_long:
        if current_price > highest:
            new_sl = round(current_price * (1 - config.TRAILING_STOP_PCT / 100), 2)
            conn.execute(
                "UPDATE trades SET highest_price=?, stop_loss=? WHERE id=?",
                (current_price, new_sl, trade_id),
            )
            conn.commit()
            conn.close()
            if new_sl != old_sl:
                logger.info(f"Trailing SL raised  trade={trade_id}  SL={new_sl}")
                return new_sl
    else:  # SELL / PUT
        if current_price < highest or highest == 0:
            new_lowest = min(highest, current_price) if highest else current_price
            new_sl = round(new_lowest * (1 + config.TRAILING_STOP_PCT / 100), 2)
            conn.execute(
                "UPDATE trades SET highest_price=?, stop_loss=? WHERE id=?",
                (new_lowest, new_sl, trade_id),
            )
            conn.commit()
            conn.close()
            if new_sl != old_sl:
                logger.info(f"Trailing SL lowered  trade={trade_id}  SL={new_sl}")
                return new_sl

    conn.close()
    return None


# ─── Open Trades ──────────────────────────────────────────────────────────────

def get_open_trades() -> List[dict]:
    conn = _db()
    rows = conn.execute(
        "SELECT * FROM trades WHERE status='OPEN'"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def daily_summary() -> dict:
    conn = _db()
    today = str(date.today())
    rows = conn.execute(
        "SELECT COUNT(*) as total, "
        "SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) as winners, "
        "COALESCE(SUM(pnl),0) as total_pnl "
        "FROM trades WHERE date=?", (today,)
    ).fetchone()
    conn.close()
    return dict(rows) if rows else {}
