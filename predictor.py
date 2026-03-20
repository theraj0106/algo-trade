"""
Predictor – computes technical indicators and runs an ML model
to generate BUY / SELL / HOLD signals with a confidence score.
"""

import logging
import os
import pickle
from typing import Tuple

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import StandardScaler

import config

logger = logging.getLogger(__name__)

MODEL_PATH  = "data/model.pkl"
SCALER_PATH = "data/scaler.pkl"

SIGNAL_BUY  = "BUY"
SIGNAL_SELL = "SELL"
SIGNAL_HOLD = "HOLD"


# ─────────────────────────────────────────────
# TECHNICAL INDICATORS
# ─────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    close = df["close"]
    high  = df["high"]
    low   = df["low"]
    vol   = df["volume"]

    # EMA
    df["ema_short"] = close.ewm(span=config.EMA_SHORT, adjust=False).mean()
    df["ema_long"]  = close.ewm(span=config.EMA_LONG,  adjust=False).mean()
    df["ema_cross"] = df["ema_short"] - df["ema_long"]

    # RSI
    delta  = close.diff()
    gain   = delta.clip(lower=0)
    loss   = -delta.clip(upper=0)
    avg_g  = gain.ewm(com=config.RSI_PERIOD - 1, adjust=False).mean()
    avg_l  = loss.ewm(com=config.RSI_PERIOD - 1, adjust=False).mean()
    rs     = avg_g / avg_l.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    # MACD
    ema_fast     = close.ewm(span=config.MACD_FAST, adjust=False).mean()
    ema_slow     = close.ewm(span=config.MACD_SLOW, adjust=False).mean()
    df["macd"]        = ema_fast - ema_slow
    df["macd_signal"] = df["macd"].ewm(span=config.MACD_SIGNAL, adjust=False).mean()
    df["macd_hist"]   = df["macd"] - df["macd_signal"]

    # Bollinger Bands
    sma            = close.rolling(config.BB_PERIOD).mean()
    std            = close.rolling(config.BB_PERIOD).std()
    df["bb_upper"] = sma + config.BB_STD * std
    df["bb_lower"] = sma - config.BB_STD * std
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / sma
    df["bb_pct"]   = (close - df["bb_lower"]) / (df["bb_upper"] - df["bb_lower"] + 1e-9)

    # ATR
    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low  - close.shift()).abs()
    tr  = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["atr"] = tr.ewm(com=13, adjust=False).mean()

    # OBV
    vol_safe = vol.replace(0, np.nan).fillna(1)
    obv = (np.sign(close.diff()) * vol_safe).fillna(0).cumsum()
    df["obv"] = obv
    df["obv_slope"] = obv.diff(5)

    # Volume ratio
    vol_ma = vol.rolling(20).mean()
    df["vol_ratio"] = np.where(vol_ma > 0, vol / vol_ma, 1.0)

    # Momentum
    df["roc_5"]  = close.pct_change(5)
    df["roc_10"] = close.pct_change(10)

    # Stochastic
    low14  = low.rolling(14).min()
    high14 = high.rolling(14).max()
    df["stoch_k"] = 100 * (close - low14) / (high14 - low14 + 1e-9)
    df["stoch_d"] = df["stoch_k"].rolling(3).mean()

    # 🔥 NEW: VWAP (Institutional level indicator)
    typical_price = (high + low + close) / 3
    cumulative_tp_vol = (typical_price * vol).cumsum()
    cumulative_vol = vol.cumsum().replace(0, np.nan)
    df["vwap"] = cumulative_tp_vol / cumulative_vol

    return df.dropna()


FEATURE_COLS = [
    "ema_cross", "rsi", "macd", "macd_signal", "macd_hist",
    "bb_width", "bb_pct", "atr", "obv_slope", "vol_ratio",
    "roc_5", "roc_10", "stoch_k", "stoch_d",
]


def _make_labels(df: pd.DataFrame, forward: int = 3) -> pd.Series:
    future_ret = df["close"].shift(-forward) / df["close"] - 1
    labels = pd.Series(1, index=df.index)
    labels[future_ret >  0.005] = 2
    labels[future_ret < -0.005] = 0
    return labels


# ─────────────────────────────────────────────
# MODEL TRAINING
# ─────────────────────────────────────────────

def train_model(df: pd.DataFrame) -> None:
    df = compute_indicators(df)
    labels = _make_labels(df)

    valid = labels.notna()
    X = df.loc[valid, FEATURE_COLS].values
    y = labels[valid].astype(int).values

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    model = RandomForestClassifier(
        n_estimators=200,
        max_depth=8,
        min_samples_leaf=10,
        class_weight="balanced",
        random_state=42,
        n_jobs=-1,
    )
    model.fit(X_scaled, y)

    os.makedirs("data", exist_ok=True)
    pickle.dump(model, open(MODEL_PATH, "wb"))
    pickle.dump(scaler, open(SCALER_PATH, "wb"))

    logger.info(f"Model trained on {len(X)} samples")


def _load_model():
    if not os.path.exists(MODEL_PATH):
        return None, None
    return pickle.load(open(MODEL_PATH, "rb")), pickle.load(open(SCALER_PATH, "rb"))


# ─────────────────────────────────────────────
# PREDICTION
# ─────────────────────────────────────────────

def predict(df: pd.DataFrame) -> Tuple[str, float]:
    df = compute_indicators(df)

    if df.empty:
        return SIGNAL_HOLD, 0.0

    model, scaler = _load_model()

    if model is None:
        return _rule_based_signal(df)

    last_row = df[FEATURE_COLS].iloc[[-1]].values
    X_scaled = scaler.transform(last_row)
    proba = model.predict_proba(X_scaled)[0]

    label_map = {0: SIGNAL_SELL, 1: SIGNAL_HOLD, 2: SIGNAL_BUY}
    pred_class = int(np.argmax(proba))
    confidence = float(proba[pred_class])

    return label_map[pred_class], confidence


def _rule_based_signal(df: pd.DataFrame) -> Tuple[str, float]:
    last = df.iloc[-1]
    score = 0

    if last["rsi"] < 35:
        score += 2
    elif last["rsi"] > 65:
        score -= 2

    if last["macd_hist"] > 0 and df["macd_hist"].iloc[-2] < 0:
        score += 2
    elif last["macd_hist"] < 0 and df["macd_hist"].iloc[-2] > 0:
        score -= 2

    score += 1 if last["ema_cross"] > 0 else -1

    if last["bb_pct"] < 0.1:
        score += 1
    elif last["bb_pct"] > 0.9:
        score -= 1

    if score >= 3:
        return SIGNAL_BUY, min(0.5 + score * 0.05, 0.85)
    elif score <= -3:
        return SIGNAL_SELL, min(0.5 + abs(score) * 0.05, 0.85)

    return SIGNAL_HOLD, 0.5