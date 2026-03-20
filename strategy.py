"""
Strategy Engine – decides WHAT to trade (CE/PE option or futures),
at WHAT strike, with WHAT quantity, based on predictor signal + option chain data.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

import pandas as pd

import config
from data_fetcher import fetch_ohlcv, fetch_option_chain
from predictor import predict, SIGNAL_BUY, SIGNAL_SELL, SIGNAL_HOLD

logger = logging.getLogger(__name__)


@dataclass
class TradeSignal:
    underlying: str
    signal: str
    confidence: float
    instrument_type: str
    tradingsymbol: str
    exchange: str = "NFO"
    quantity: int = 0
    entry_price: float = 0.0
    entry_spot: float = 0.0
    stop_loss: float = 0.0
    target: float = 0.0
    strike: float = 0.0
    expiry: str = ""
    reason: str = ""
    confluence_score: int = 0
    confluence_detail: str = ""


# ─────────────────────────────────────────────
# MAIN ANALYSIS
# ─────────────────────────────────────────────

def analyse(symbol: str) -> Optional[TradeSignal]:

    now_str = datetime.now().strftime("%H:%M")

    is_mcx = symbol in config.UPSTOX_COMMODITY_KEYS

    if is_mcx:
        if not (config.MCX_MARKET_OPEN <= now_str <= config.MCX_MARKET_CLOSE):
            return None
    else:
        if not (config.ENTRY_START <= now_str <= config.ENTRY_END):
            return None

    try:
        df = fetch_ohlcv(symbol)
    except Exception as e:
        logger.error(f"{symbol}: Data fetch error: {e}")
        return None

    signal, confidence = predict(df)

    logger.info(f"{symbol} → ML Signal={signal}  Conf={confidence:.2f}")

    if signal == SIGNAL_HOLD or confidence < config.ML_CONFIDENCE_THRESHOLD:
        return None

    # ───────────── VWAP FILTER ─────────────
    from predictor import compute_indicators
    df_ind = compute_indicators(df)

    if df_ind.empty:
        return None

    last = df_ind.iloc[-1]
    price = last["close"]
    vwap  = last.get("vwap", None)

    if vwap is not None:
        if signal == SIGNAL_BUY and price < vwap:
            logger.info(f"{symbol}: VWAP blocked BUY (price < vwap)")
            return None

        if signal == SIGNAL_SELL and price > vwap:
            logger.info(f"{symbol}: VWAP blocked SELL (price > vwap)")
            return None

    # ───────────── EXPIRY FILTER ─────────────
    today = datetime.now().weekday()
    hour  = datetime.now().hour

    is_index = symbol in ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"]

    if is_index and today == 3 and hour >= 13:
        logger.info(f"{symbol}: Expiry filter active → skipping trade")
        return None

    # ───────────── CORRELATION FILTER ─────────────
    try:
        nifty_df = fetch_ohlcv("NIFTY")
        bank_df  = fetch_ohlcv("BANKNIFTY")

        nifty_ind = compute_indicators(nifty_df)
        bank_ind  = compute_indicators(bank_df)

        if not nifty_ind.empty and not bank_ind.empty:

            nifty_last = nifty_ind.iloc[-1]
            bank_last  = bank_ind.iloc[-1]

            nifty_trend = nifty_last["close"] > nifty_last["ema_short"]
            bank_trend  = bank_last["close"] > bank_last["ema_short"]

            if nifty_trend != bank_trend:
                logger.info(f"{symbol}: Index conflict → skipping trade")
                return None

    except Exception as e:
        logger.warning(f"Correlation check failed: {e}")

    # ───────────── 🔥 MARKET REGIME FILTER ─────────────
    try:
        df_regime = compute_indicators(df)

        if df_regime.empty:
            return None

        last = df_regime.iloc[-1]

        ema_short = last["ema_short"]
        ema_long  = last["ema_long"]
        atr       = last["atr"]
        price     = last["close"]

        ema_gap_pct = abs(ema_short - ema_long) / price * 100
        atr_pct = (atr / price) * 100

        is_trending = ema_gap_pct > 0.2 and atr_pct > 0.5

        if not is_trending:
            logger.info(f"{symbol}: Sideways market → skipping trade")
            return None

        logger.info(f"{symbol}: Trending ✓ EMA gap={ema_gap_pct:.2f} ATR={atr_pct:.2f}")

    except Exception as e:
        logger.warning(f"Regime filter failed: {e}")

    # ───────────── CONFLUENCE ─────────────
    score, detail = _confluence_check(df, signal)

    logger.info(f"{symbol} → Confluence={score} [{detail}]")

    if score < config.MIN_CONFLUENCE_SCORE:
        return None

    chain = fetch_option_chain(symbol)

    if chain.empty:
        logger.warning(f"{symbol}: Option chain empty")
        return None

    sig = _options_signal(symbol, df, signal, confidence, chain)

    if sig:
        sig.confluence_score = score
        sig.confluence_detail = detail

    return sig


# ─────────────────────────────────────────────
# CONFLUENCE CHECK
# ─────────────────────────────────────────────

def _confluence_check(df: pd.DataFrame, signal: str) -> Tuple[int, str]:

    from predictor import compute_indicators
    df = compute_indicators(df)

    if df.empty:
        return 0, "no-data"

    last = df.iloc[-1]

    score = 0
    flags = []
    is_buy = signal == SIGNAL_BUY

    rsi = last["rsi"]
    if is_buy and config.RSI_BUY_MIN <= rsi <= config.RSI_BUY_MAX:
        score += 1; flags.append(f"RSI✓{rsi:.0f}")
    elif not is_buy and config.RSI_SELL_MIN <= rsi <= config.RSI_SELL_MAX:
        score += 1; flags.append(f"RSI✓{rsi:.0f}")
    else:
        flags.append(f"RSI✗{rsi:.0f}")

    if is_buy and last["macd"] > last["macd_signal"]:
        score += 2; flags.append("MACD✓✓")
    elif not is_buy and last["macd"] < last["macd_signal"]:
        score += 2; flags.append("MACD✓✓")
    else:
        flags.append("MACD✗")

    if is_buy and last["ema_cross"] > 0:
        score += 1; flags.append("EMA✓")
    elif not is_buy and last["ema_cross"] < 0:
        score += 1; flags.append("EMA✓")
    else:
        flags.append("EMA✗")

    bb_pct = last["bb_pct"]
    if is_buy and 0.3 <= bb_pct <= 0.75:
        score += 1; flags.append("BB✓")
    elif not is_buy and 0.25 <= bb_pct <= 0.7:
        score += 1; flags.append("BB✓")
    else:
        flags.append("BB✗")

    roc = last["roc_5"]
    if is_buy and roc > 0:
        score += 1; flags.append("MOM✓")
    elif not is_buy and roc < 0:
        score += 1; flags.append("MOM✓")
    else:
        flags.append("MOM✗")

    stoch = last["stoch_k"]
    if is_buy and stoch < 75:
        score += 1; flags.append("STOCH✓")
    elif not is_buy and stoch > 25:
        score += 1; flags.append("STOCH✓")
    else:
        flags.append("STOCH✗")

    return score, "  ".join(flags)


# ─────────────────────────────────────────────
# OPTION SIGNAL
# ─────────────────────────────────────────────

def _options_signal(symbol: str, df: pd.DataFrame,
                    signal: str, confidence: float,
                    chain: pd.DataFrame) -> Optional[TradeSignal]:

    from predictor import compute_indicators

    df_ind = compute_indicators(df)
    if df_ind.empty:
        return None

    last = df_ind.iloc[-1]

    ltp = float(last["close"])
    atr = float(last["atr"])

    step = config.INDEX_STRIKE_STEP.get(symbol, 50)
    atm = round(ltp / step) * step

    opt_type = "CE" if signal == SIGNAL_BUY else "PE"

    logger.info(f"{symbol} → Signal={signal} → Option Type={opt_type}")

    if signal == SIGNAL_BUY:
        target_strike = atm + step
    else:
        target_strike = atm - step

    options = chain[chain["instrument_type"] == opt_type]

    if options.empty:
        logger.warning(f"{symbol}: No {opt_type} options available")
        return None

    options = options.copy()
    options["diff"] = abs(options["strike"] - target_strike)

    row = options.sort_values("diff").iloc[0]

    logger.info(
        f"{symbol} → Target={target_strike} → Selected={row['strike']} ({opt_type})"
    )

    premium = float(row.get("option_ltp", 0))

    # OPTION QUALITY
    if premium <= 0:
        return None
    if premium < 20 or premium > 300:
        return None

    iv = row.get("iv", None)
    if iv is not None:
        if iv < 10 or iv > 40:
            return None

    # 🔥 SAFE ATR SL (3–6%)
    atr_pct = (atr / ltp) * 100
    sl_pct = max(3, min(6, atr_pct))
    tgt_pct = sl_pct * 2

    sl = round(premium * (1 - sl_pct / 100), 2)
    tgt = round(premium * (1 + tgt_pct / 100), 2)

    logger.info(f"{symbol} → SL%={sl_pct:.2f} TGT%={tgt_pct:.2f}")

    qty = _calc_quantity(premium, symbol)

    return TradeSignal(
        underlying=symbol,
        signal=signal,
        confidence=confidence,
        instrument_type=opt_type,
        tradingsymbol=row["tradingsymbol"],
        quantity=qty,
        entry_price=premium,
        entry_spot=ltp,
        stop_loss=sl,
        target=tgt,
        strike=row["strike"],
        expiry=str(row.get("expiry", "")),
        reason=f"ML + Confluence + Regime + ATR",
    )


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _calc_quantity(premium, symbol):

    if "CRUDEOIL" in symbol:
        lot_size = config.MCX_LOT_SIZE.get("CRUDEOIL", 1)
    elif "NATURALGAS" in symbol:
        lot_size = config.MCX_LOT_SIZE.get("NATURALGAS", 1)
    elif "GOLD" in symbol:
        lot_size = config.MCX_LOT_SIZE.get("GOLD", 1)
    elif "BANKNIFTY" in symbol:
        lot_size = config.INDEX_LOT_SIZE.get("BANKNIFTY", 1)
    elif "NIFTY" in symbol:
        lot_size = config.INDEX_LOT_SIZE.get("NIFTY", 1)
    elif "SENSEX" in symbol:
        lot_size = config.INDEX_LOT_SIZE.get("SENSEX", 1)
    else:
        lot_size = 1

    capital = config.MAX_CAPITAL_PER_TRADE
    qty = int(capital / premium)

    return max((qty // lot_size) * lot_size, lot_size)