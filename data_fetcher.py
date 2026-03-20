"""
Data Fetcher – all market data from Upstox API v2 only.
No yfinance fallback. MCX commodities resolved via instrument master CSV.
"""

import csv
import gzip
import io
import logging
import time
from datetime import date, timedelta
from typing import Optional

import pandas as pd
import requests as _requests

import config

logger = logging.getLogger(__name__)


# ─── Upstox client (lazy) ─────────────────────────────────────────────────────

def _get_upstox_config():
    import upstox_client
    configuration = upstox_client.Configuration()
    configuration.access_token = config.UPSTOX_ACCESS_TOKEN
    return configuration


def _upstox_client():
    import upstox_client
    return upstox_client.ApiClient(_get_upstox_config())


# ─── Instrument Master (Upstox CSV, cached daily) ─────────────────────────────

_master_cache: dict = {"rows": None, "ts": 0.0}
_MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz"


def _load_master() -> list:
    """Download and cache the Upstox instrument master. Refresh once per day."""
    now = time.time()
    if _master_cache["rows"] is not None and now - _master_cache["ts"] < 86400:
        return _master_cache["rows"]

    logger.info("Downloading Upstox instrument master CSV...")
    try:
        r = _requests.get(_MASTER_URL, timeout=30)
        r.raise_for_status()
        buf = io.BytesIO(r.content)
        rows = []
        with gzip.open(buf, "rt") as f:
            for row in csv.DictReader(f):
                # Keep MCX_FO for commodities + NSE/BSE for reference
                if row.get("exchange") in ("MCX_FO",):
                    rows.append(row)
        _master_cache["rows"] = rows
        _master_cache["ts"] = now
        logger.info(f"Instrument master loaded: {len(rows)} MCX instruments")
        return rows
    except Exception as e:
        logger.error(f"Instrument master download failed: {e}")
        return _master_cache["rows"] or []


def _mcx_near_month_futures_key(symbol: str) -> str:
    """
    Return the Upstox instrument key for the nearest non-mini MCX futures contract.
    Raises ValueError if not found.
    """
    today = date.today().isoformat()
    prefix = symbol  # e.g. "CRUDEOIL", "NATURALGAS", "GOLD"
    mini_prefix = f"{symbol}M"

    rows = _load_master()
    candidates = [
        r for r in rows
        if r.get("instrument_type") == "FUTCOM"
        and r.get("tradingsymbol", "").startswith(prefix)
        and not r.get("tradingsymbol", "").startswith(mini_prefix)
        and r.get("expiry", "") >= today
    ]
    if not candidates:
        raise ValueError(f"No near-month futures found for {symbol}")

    nearest = sorted(candidates, key=lambda x: x["expiry"])[0]
    logger.debug(f"MCX near-month futures {symbol}: {nearest['tradingsymbol']}  key={nearest['instrument_key']}")
    return nearest["instrument_key"]


# ─── Instrument Key Helper ────────────────────────────────────────────────────

def _instrument_key(symbol: str) -> str:
    """NSE/BSE index instrument key (not for MCX – use _mcx_near_month_futures_key)."""
    if symbol in config.UPSTOX_INDEX_KEYS:
        return config.UPSTOX_INDEX_KEYS[symbol]
    return f"NSE_EQ|{symbol}"


# ─── Historical OHLCV ─────────────────────────────────────────────────────────

def fetch_ohlcv(symbol: str,
                interval: str = config.CANDLE_INTERVAL,
                lookback: int = config.LOOKBACK_CANDLES) -> pd.DataFrame:
    """Fetch OHLCV candles from Upstox. MCX uses near-month futures key."""
    if not config.UPSTOX_ACCESS_TOKEN:
        raise RuntimeError("UPSTOX_ACCESS_TOKEN not set – cannot fetch data")
    return _fetch_upstox(symbol, interval, lookback)


def _fetch_upstox(symbol: str, interval: str, lookback: int) -> pd.DataFrame:
    import upstox_client

    cfg = _get_upstox_config()
    api = upstox_client.HistoryApi(upstox_client.ApiClient(cfg))

    # Choose instrument key: MCX futures vs NSE/BSE index
    if symbol in config.UPSTOX_COMMODITY_KEYS:
        key = _mcx_near_month_futures_key(symbol)
    else:
        key = _instrument_key(symbol)

    to_date   = date.today()
    from_date = to_date - timedelta(days=7)

    resp = api.get_historical_candle_data1(
        instrument_key=key,
        interval=interval,
        to_date=str(to_date),
        from_date=str(from_date),
        api_version="2.0",
    )
    candles = resp.data.candles
    if not candles:
        raise ValueError(f"No candles returned for {symbol} (key={key})")

    rows = [
        {
            "datetime": c[0],
            "open":     float(c[1]),
            "high":     float(c[2]),
            "low":      float(c[3]),
            "close":    float(c[4]),
            "volume":   float(c[5]),
        }
        for c in candles
    ]
    df = pd.DataFrame(rows)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df.set_index("datetime", inplace=True)
    df.sort_index(inplace=True)
    df = _resample(df)
    df = df.tail(lookback)
    logger.debug(f"Upstox OHLCV {symbol}: {len(df)} candles (key={key})")
    return df


def _resample(df: pd.DataFrame) -> pd.DataFrame:
    rule = config.RESAMPLE_INTERVAL
    return df.resample(rule).agg({
        "open":   "first",
        "high":   "max",
        "low":    "min",
        "close":  "last",
        "volume": "sum",
    }).dropna()


# ─── Live Quote (LTP) ─────────────────────────────────────────────────────────

def fetch_ltp(symbol: str) -> float:
    """Last traded price from Upstox. MCX uses near-month futures price."""
    if not config.UPSTOX_ACCESS_TOKEN:
        raise RuntimeError("UPSTOX_ACCESS_TOKEN not set – cannot fetch LTP")

    import upstox_client
    cfg = _get_upstox_config()
    api = upstox_client.MarketQuoteApi(upstox_client.ApiClient(cfg))

    if symbol in config.UPSTOX_COMMODITY_KEYS:
        key = _mcx_near_month_futures_key(symbol)
    else:
        key = _instrument_key(symbol)

    resp = api.ltp(key, "2.0")
    data = resp.data
    if not data:
        raise ValueError(f"Empty LTP response for {symbol} (key={key})")

    quote = data.get(key) or next(iter(data.values()))
    price = float(quote.last_price)
    logger.debug(f"LTP {symbol}: ₹{price}  (key={key})")
    return price


# ─── MCX Option Lookup via Instrument Master ──────────────────────────────────

def _mcx_option_rows(symbol: str) -> list:
    """All non-mini MCX option rows for a commodity, sorted by expiry."""
    today = date.today().isoformat()
    prefix      = symbol
    mini_prefix = f"{symbol}M"
    rows = _load_master()
    opts = [
        r for r in rows
        if r.get("instrument_type") == "OPTFUT"
        and r.get("tradingsymbol", "").startswith(prefix)
        and not r.get("tradingsymbol", "").startswith(mini_prefix)
        and r.get("expiry", "") >= today
    ]
    return sorted(opts, key=lambda x: (x["expiry"], float(x.get("strike", 0))))


# ─── Option Contract Cache (NSE/BSE, refresh hourly) ─────────────────────────

_contract_cache: dict = {}


def _get_nse_contracts(underlying: str) -> list:
    now = time.time()
    cached = _contract_cache.get(underlying)
    if cached and now - cached["ts"] < 3600:
        return cached["contracts"]

    import upstox_client
    cfg = _get_upstox_config()
    api = upstox_client.OptionsApi(upstox_client.ApiClient(cfg))
    key = _instrument_key(underlying)
    resp = api.get_option_contracts(key)
    contracts = resp.data or []
    _contract_cache[underlying] = {"contracts": contracts, "ts": now}
    logger.debug(f"NSE contracts refreshed for {underlying}: {len(contracts)}")
    return contracts


# ─── Live Option LTP ──────────────────────────────────────────────────────────

def fetch_option_ltp(underlying: str, strike: float, opt_type: str) -> Optional[float]:
    """
    Fetch live option LTP from Upstox.
    NSE/BSE: via OptionsApi contracts + MarketQuote.
    MCX: via instrument master CSV + MarketQuote.
    """
    if not config.UPSTOX_ACCESS_TOKEN:
        return None

    import upstox_client
    cfg = _get_upstox_config()
    mq  = upstox_client.MarketQuoteApi(upstox_client.ApiClient(cfg))

    try:
        if underlying in config.UPSTOX_COMMODITY_KEYS:
            # MCX: find from instrument master
            opts = _mcx_option_rows(underlying)
            nearest_expiry = opts[0]["expiry"] if opts else None
            match = next(
                (r for r in opts
                 if r["expiry"] == nearest_expiry
                 and abs(float(r.get("strike", 0)) - strike) < 0.5
                 and r.get("option_type") == opt_type),
                None,
            )
            if not match:
                logger.warning(f"No MCX option found {underlying} {strike:.0f}{opt_type}")
                return None
            inst_key = match["instrument_key"]
        else:
            # NSE/BSE: via OptionsApi
            today = date.today()
            contracts = _get_nse_contracts(underlying)
            future = [c for c in contracts if c.expiry.date() >= today]
            if not future:
                return None
            nearest = min(c.expiry.date() for c in future)
            contract = next(
                (c for c in future
                 if c.expiry.date() == nearest
                 and abs(c.strike_price - strike) < 0.5
                 and c.instrument_type == opt_type),
                None,
            )
            if not contract:
                logger.warning(f"No NSE option found {underlying} {strike:.0f}{opt_type}")
                return None
            inst_key = contract.instrument_key

        resp = mq.ltp(inst_key, "2.0")
        if resp.data:
            quote = resp.data.get(inst_key) or next(iter(resp.data.values()))
            price = float(quote.last_price)
            logger.info(f"Live option LTP {underlying} {strike:.0f}{opt_type}: ₹{price}")
            return price

    except Exception as e:
        logger.warning(f"Option LTP fetch failed {underlying} {strike:.0f}{opt_type}: {e}")
    return None


# ─── Option Chain ─────────────────────────────────────────────────────────────

def fetch_option_chain(underlying: str, expiry_offset: int = 0) -> pd.DataFrame:
    """
    Returns ATM ± 5 strike option chain with real live LTPs.
    Always uses Upstox — paper mode simulates orders only, not prices.
    """
    if not config.UPSTOX_ACCESS_TOKEN:
        return pd.DataFrame()

    try:
        import upstox_client
        cfg = _get_upstox_config()
        mq  = upstox_client.MarketQuoteApi(upstox_client.ApiClient(cfg))

        spot = fetch_ltp(underlying)
        step = _strike_step(underlying)
        atm  = round(spot / step) * step
        target_strikes = {atm + i * step for i in range(-5, 6)}

        if underlying in config.UPSTOX_COMMODITY_KEYS:
            # ── MCX ───────────────────────────────────────────────────────────
            opts = _mcx_option_rows(underlying)
            if not opts:
                return pd.DataFrame()
            nearest_expiry = opts[0]["expiry"]
            selected = [
                r for r in opts
                if r["expiry"] == nearest_expiry
                and float(r.get("strike", 0)) in target_strikes
            ]
        else:
            # ── NSE/BSE ───────────────────────────────────────────────────────
            today     = date.today()
            contracts = _get_nse_contracts(underlying)
            future    = [c for c in contracts if c.expiry.date() >= today]
            if not future:
                return pd.DataFrame()
            nearest_expiry = str(min(c.expiry.date() for c in future))
            selected_obj   = [
                c for c in future
                if str(c.expiry.date()) == nearest_expiry
                and c.strike_price in target_strikes
            ]
            # Convert to dict-like rows for uniform processing below
            selected = [
                {
                    "instrument_key": c.instrument_key,
                    "option_type":    c.instrument_type,
                    "strike":         str(c.strike_price),
                    "expiry":         nearest_expiry,
                }
                for c in selected_obj
            ]

        if not selected:
            logger.warning(f"No option contracts found near ATM for {underlying}")
            return pd.DataFrame()

        # Batch LTP fetch — response keys use symbol format, not token format,
        # so match by position (Python dicts are insertion-ordered since 3.7)
        keys_str  = ",".join(r["instrument_key"] for r in selected)
        ltp_resp  = mq.ltp(keys_str, "2.0")
        ltp_vals  = list(ltp_resp.data.values()) if ltp_resp.data else []

        rows = []
        for i, r in enumerate(selected):
            price = float(ltp_vals[i].last_price) if i < len(ltp_vals) else 0.0
            rows.append({
                "instrument_type": r["option_type"],
                "strike":          float(r["strike"]),
                "tradingsymbol":   r["instrument_key"],
                "option_ltp":      price,
                "expiry":          r["expiry"],
            })

        df = pd.DataFrame(rows)
        logger.info(f"Option chain {underlying}: {len(df)} contracts  expiry={nearest_expiry}  spot={spot:.2f}")
        return df

    except Exception as e:
        logger.error(f"Option chain fetch failed for {underlying}: {e}", exc_info=True)
        return pd.DataFrame()


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _strike_step(symbol: str) -> int:
    if symbol in config.MCX_STRIKE_STEP:
        return config.MCX_STRIKE_STEP[symbol]
    return config.INDEX_STRIKE_STEP.get(symbol, 50)
