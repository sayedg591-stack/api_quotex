"""
Technical indicators for the Quotex Market Analyzer.
All functions operate on pandas Series or DataFrames with columns:
  open, high, low, close, volume  (timestamp as index).
No external TA libraries required — pure pandas/numpy.
"""

from __future__ import annotations
import numpy as np
import pandas as pd
from typing import Dict, Any


def _safe_float(value: Any, default: float = 0.0) -> float:
    """
    Convert a value to a finite float, substituting `default` for NaN/Inf/None.
    Guards the "No NaN outputs" requirement against edge cases such as a flat
    market (zero directional movement -> 0/0 in ADX/DI), a too-short candle
    history, or an asset with genuinely zero volume everywhere.
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if np.isnan(f) or np.isinf(f):
        return default
    return f


# ─── EMA ─────────────────────────────────────────────────────────────────────

def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average (pandas ewm span)."""
    return series.ewm(span=period, adjust=False).mean()


# ─── RSI ─────────────────────────────────────────────────────────────────────

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    Relative Strength Index via Wilder EMA.
    Edge cases:
      avg_loss == 0  → all gains  → RSI = 100
      avg_gain == 0  → all losses → RSI = 0
      both == 0      → no change  → RSI = 50
    """
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()

    # Build RSI avoiding division by zero
    rsi_vals = pd.Series(index=series.index, dtype=float)
    both_zero = (avg_gain == 0) & (avg_loss == 0)
    all_gain   = avg_loss == 0
    all_loss   = avg_gain == 0
    normal     = ~all_gain & ~all_loss

    rsi_vals[both_zero] = 50.0
    rsi_vals[all_gain & ~both_zero] = 100.0
    rsi_vals[all_loss & ~both_zero] = 0.0
    rs = avg_gain[normal] / avg_loss[normal]
    rsi_vals[normal] = 100 - (100 / (1 + rs))
    return rsi_vals


# ─── MACD ────────────────────────────────────────────────────────────────────

def macd(series: pd.Series, fast: int = 12, slow: int = 26,
         signal: int = 9) -> Dict[str, pd.Series]:
    """MACD line, Signal line, Histogram."""
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return {"macd": macd_line, "signal": signal_line, "histogram": histogram}


# ─── Bollinger Bands ─────────────────────────────────────────────────────────

def bollinger_bands(series: pd.Series, period: int = 20,
                    std_mult: float = 2.0) -> Dict[str, pd.Series]:
    """Upper, Middle (SMA), Lower Bollinger Bands."""
    sma = series.rolling(period).mean()
    std = series.rolling(period).std(ddof=0)
    return {
        "upper":  sma + std_mult * std,
        "middle": sma,
        "lower":  sma - std_mult * std,
        "width":  (2 * std_mult * std) / sma,   # normalised BB width
    }


# ─── ATR ─────────────────────────────────────────────────────────────────────

def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range (Wilder EMA of True Range)."""
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"]  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


# ─── VWAP ────────────────────────────────────────────────────────────────────

def vwap(df: pd.DataFrame) -> pd.Series:
    """
    Volume-Weighted Average Price.
    Uses all rows in df (intraday session approximation).
    Falls back to SMA-close when volume is zero everywhere.
    """
    vol = df["volume"].fillna(0)
    typical = (df["high"] + df["low"] + df["close"]) / 3
    cum_vol = vol.cumsum()
    cum_tp_vol = (typical * vol).cumsum()
    if cum_vol.iloc[-1] == 0:
        # No volume data — fall back to cumulative simple average
        return typical.expanding().mean()
    return cum_tp_vol / cum_vol.replace(0, np.nan)


# ─── ADX ─────────────────────────────────────────────────────────────────────

def adx(df: pd.DataFrame, period: int = 14) -> Dict[str, pd.Series]:
    """
    Average Directional Index with +DI and -DI.
    Uses Wilder's smoothing (ewm alpha = 1/period).
    """
    alpha = 1 / period
    high, low, close = df["high"], df["low"], df["close"]
    prev_high  = high.shift(1)
    prev_low   = low.shift(1)
    prev_close = close.shift(1)

    # True Range
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)

    # Directional Movement
    up_move   = high - prev_high
    down_move = prev_low - low
    plus_dm  = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    plus_dm_s  = pd.Series(plus_dm,  index=df.index).ewm(alpha=alpha, adjust=False).mean()
    minus_dm_s = pd.Series(minus_dm, index=df.index).ewm(alpha=alpha, adjust=False).mean()
    atr_s      = tr.ewm(alpha=alpha, adjust=False).mean()

    plus_di  = 100 * plus_dm_s  / atr_s.replace(0, np.nan)
    minus_di = 100 * minus_dm_s / atr_s.replace(0, np.nan)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_line = dx.ewm(alpha=alpha, adjust=False).mean()

    return {"+di": plus_di, "-di": minus_di, "adx": adx_line}


# ─── Stochastic RSI ──────────────────────────────────────────────────────────

def stochastic_rsi(series: pd.Series, rsi_period: int = 14,
                   stoch_period: int = 14,
                   k_smooth: int = 3, d_smooth: int = 3) -> Dict[str, pd.Series]:
    """
    Stochastic applied to RSI values.
    Returns %K and %D (smoothed).
    """
    rsi_vals = rsi(series, rsi_period)
    min_rsi = rsi_vals.rolling(stoch_period).min()
    max_rsi = rsi_vals.rolling(stoch_period).max()
    denom   = (max_rsi - min_rsi).replace(0, np.nan)
    stoch_k = 100 * (rsi_vals - min_rsi) / denom
    stoch_k_smooth = stoch_k.rolling(k_smooth).mean()
    stoch_d_smooth = stoch_k_smooth.rolling(d_smooth).mean()
    return {"k": stoch_k_smooth, "d": stoch_d_smooth}


# ─── Support & Resistance ─────────────────────────────────────────────────────

def support_resistance(df: pd.DataFrame,
                       lookback: int = 30) -> Dict[str, float]:
    """
    Identify key support and resistance levels from recent candles.
    Support  = lowest low  in the lookback window.
    Resistance = highest high in the lookback window.
    Also returns a mid-pivot: (resistance + support) / 2.
    """
    recent = df.tail(lookback)
    support_level    = float(recent["low"].min())
    resistance_level = float(recent["high"].max())
    pivot            = (support_level + resistance_level) / 2
    return {
        "support":    support_level,
        "resistance": resistance_level,
        "pivot":      pivot,
    }


# ─── Support & Resistance — Zone Engine (Phase 6) ─────────────────────────────
# Genuinely new/additive. Does NOT touch support_resistance() above — that
# function and its 3 output keys (support/resistance/pivot) are unchanged and
# byte-identical to before, for full backward compatibility with existing
# callers (calculate_all(), print_report(), the API response's
# indicators.support / indicators.resistance fields, none of which are
# modified by anything below).

_SR_ADX_TREND_THRESHOLD = 25.0  # local copy, mirrors config.ADX_TRENDING; kept
                                 # independent to avoid coupling indicators.py
                                 # (pure pandas/numpy, no project imports) to config.py
_SR_TOO_CLOSE_ATR = 0.5         # within this many ATRs of a zone = "too close" / unsafe


def _swing_high_low_prices(df: pd.DataFrame, k: int = 2) -> tuple[pd.Series, pd.Series]:
    """
    Vectorized fractal swing detection: bar i is a swing high if its `high`
    equals the max of a centered (2k+1)-bar window; swing low analogous.
    Returns two Series aligned to df.index, value = the swing price at that
    bar (NaN everywhere else). No Python loop.

    Note on look-ahead: a centered window means bar i's swing status isn't
    knowable until k bars later (needs k future bars to confirm it was a
    local extreme). Callers that need causal/backtest-safe values should
    apply `.shift(k)` to these series before using them (see
    backtest.py::_factor_votes for the historical version). The live
    snapshot function below doesn't need this shift trick since it's
    evaluating a fixed point in time, not building a walk-forward vote
    series; pandas' rolling(center=True) already naturally returns NaN for
    the most recent k bars (not enough trailing data to confirm them yet),
    which is the correct behavior for "as of now."
    """
    if len(df) == 0:
        empty = pd.Series(dtype=float)
        return empty, empty
    window = 2 * k + 1
    high, low = df["high"], df["low"]
    is_high = high == high.rolling(window, center=True).max()
    is_low = low == low.rolling(window, center=True).min()
    swing_high_price = high.where(is_high)
    swing_low_price = low.where(is_low)
    return swing_high_price, swing_low_price


def _cluster_zones(prices: list[float], merge_tol: float) -> list[Dict[str, Any]]:
    """
    Merge nearby swing prices into zones (sorted single-pass merge — cheap,
    operates on a handful of swing points, not every candle).
    Each zone: {"price": average of merged points, "touches": count}.
    """
    if not prices:
        return []
    pts = sorted(prices)
    clusters: list[list[float]] = [[pts[0]]]
    for p in pts[1:]:
        if p - clusters[-1][-1] <= merge_tol:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    return [{"price": sum(c) / len(c), "touches": len(c)} for c in clusters]


def _empty_sr_detail() -> Dict[str, Any]:
    """Safe, well-defined result when there isn't enough history to detect
    any zones — never crashes, never returns partial/inconsistent data."""
    return {
        "nearest_support": None, "nearest_resistance": None,
        "support_strength": 0.0, "resistance_strength": 0.0,
        "support_distance": None, "resistance_distance": None,
        "support_distance_atr": None, "resistance_distance_atr": None,
        "safe_entry": True,   # nothing known nearby = no detected danger zone
        "breakout_possible": False, "bounce_possible": False,
        "zone_reliability": 0.0,
        "support_zone": None, "resistance_zone": None,
    }


def detect_support_resistance_zones(df: pd.DataFrame,
                                     lookback: int = 30,
                                     atr_value: float | None = None,
                                     adx_value: float | None = None,
                                     k: int = 2) -> Dict[str, Any]:
    """
    Rich support/resistance zone engine — swing-point detection + ATR-based
    zone merging + geometry/frequency-derived strength (NOT a fixed
    per-level lookup table). Purely additive; does not affect
    support_resistance() above in any way.

    atr_value / adx_value are optional — if the caller (calculate_all())
    already computed them, pass them in to avoid recomputation; otherwise a
    local proxy is used. Never raises on NaN/empty/short input — returns
    _empty_sr_detail() instead.
    """
    if df is None or len(df) < (2 * k + 3):
        return _empty_sr_detail()

    window_df = df.tail(lookback) if lookback else df
    if len(window_df) < (2 * k + 3):
        window_df = df  # fall back to full history if lookback is too tight

    close = window_df["close"].dropna()
    if len(close) == 0:
        return _empty_sr_detail()
    current_price = float(close.iloc[-1])

    atr_v = atr_value if (atr_value is not None and atr_value > 0) else _local_atr_proxy(window_df, lookback=min(14, len(window_df)))
    if not atr_v or atr_v <= 0:
        rng = (window_df["high"] - window_df["low"]).mean()
        atr_v = float(rng) if rng and rng > 0 else 1e-6  # guard divide-by-zero, never crash

    swing_high_price, swing_low_price = _swing_high_low_prices(window_df, k=k)
    swing_highs = swing_high_price.dropna().tolist()
    swing_lows = swing_low_price.dropna().tolist()

    merge_tol = 0.5 * atr_v
    resistance_zones = _cluster_zones(swing_highs, merge_tol)
    support_zones = _cluster_zones(swing_lows, merge_tol)

    max_r_touch = max((z["touches"] for z in resistance_zones), default=1)
    max_s_touch = max((z["touches"] for z in support_zones), default=1)
    for z in resistance_zones:
        z["strength"] = round(_clamp(100.0 * z["touches"] / max(max_r_touch, 1)), 1)
    for z in support_zones:
        z["strength"] = round(_clamp(100.0 * z["touches"] / max(max_s_touch, 1)), 1)

    below = [z for z in support_zones if z["price"] <= current_price]
    above = [z for z in resistance_zones if z["price"] >= current_price]
    nearest_support_zone = max(below, key=lambda z: z["price"]) if below else None
    nearest_resistance_zone = min(above, key=lambda z: z["price"]) if above else None

    support_distance = (current_price - nearest_support_zone["price"]) if nearest_support_zone else None
    resistance_distance = (nearest_resistance_zone["price"] - current_price) if nearest_resistance_zone else None
    support_distance_atr = (support_distance / atr_v) if support_distance is not None else None
    resistance_distance_atr = (resistance_distance / atr_v) if resistance_distance is not None else None

    safe_entry = True
    if support_distance_atr is not None and support_distance_atr < _SR_TOO_CLOSE_ATR:
        safe_entry = False
    if resistance_distance_atr is not None and resistance_distance_atr < _SR_TOO_CLOSE_ATR:
        safe_entry = False

    near_a_level = (
        (support_distance_atr is not None and support_distance_atr < 1.0) or
        (resistance_distance_atr is not None and resistance_distance_atr < 1.0)
    )
    strong_trend = adx_value is not None and adx_value >= _SR_ADX_TREND_THRESHOLD
    breakout_possible = bool(near_a_level and strong_trend)
    bounce_possible = bool(near_a_level and not strong_trend)

    strengths = [z["strength"] for z in (nearest_support_zone, nearest_resistance_zone) if z]
    zone_reliability = round(sum(strengths) / len(strengths), 1) if strengths else 0.0

    return {
        "nearest_support": round(nearest_support_zone["price"], 5) if nearest_support_zone else None,
        "nearest_resistance": round(nearest_resistance_zone["price"], 5) if nearest_resistance_zone else None,
        "support_strength": nearest_support_zone["strength"] if nearest_support_zone else 0.0,
        "resistance_strength": nearest_resistance_zone["strength"] if nearest_resistance_zone else 0.0,
        "support_distance": round(support_distance, 5) if support_distance is not None else None,
        "resistance_distance": round(resistance_distance, 5) if resistance_distance is not None else None,
        "support_distance_atr": round(support_distance_atr, 3) if support_distance_atr is not None else None,
        "resistance_distance_atr": round(resistance_distance_atr, 3) if resistance_distance_atr is not None else None,
        "safe_entry": safe_entry,
        "breakout_possible": breakout_possible,
        "bounce_possible": bounce_possible,
        "zone_reliability": zone_reliability,
        "support_zone": nearest_support_zone,
        "resistance_zone": nearest_resistance_zone,
    }


# ─── Phase 7.3 Part 3 — New OTC Indicators ─────────────────────────────────────
# Registered in webapp/indicator_registry.py. Integrated into Backtest and
# Settings only in this phase — deliberately NOT yet wired into
# _confluence_factor_votes()/DEFAULT_CONFLUENCE_WEIGHTS, per the explicit
# instruction to connect them to Confluence only in a later, separate step.
# All three reuse the existing _candle_metrics/_clamp/_local_atr_proxy
# helpers (Phase 5) and detect_support_resistance_zones (Phase 6) — no
# duplicated geometry math.

def detect_wick_rejection(df: pd.DataFrame, atr_value: float | None = None) -> Dict[str, Any] | None:
    """
    Single-candle wick-dominance rejection: a long wick on one side, with
    little continuation in that direction, suggests price was rejected at
    that level. direction/strength/reliability all geometry-derived.
    """
    if len(df) < 1:
        return None
    last = _candle_metrics(df.iloc[-1])
    if last["range"] <= 0:
        return None
    atr_v = atr_value if (atr_value is not None and atr_value > 0) else _local_atr_proxy(df)
    range_to_atr = (last["range"] / atr_v) if atr_v > 0 else 1.0

    if last["lower_wick_ratio"] >= 0.5 and last["lower_wick_ratio"] > last["upper_wick_ratio"] * 2:
        direction, wick_ratio = "BUY", last["lower_wick_ratio"]
    elif last["upper_wick_ratio"] >= 0.5 and last["upper_wick_ratio"] > last["lower_wick_ratio"] * 2:
        direction, wick_ratio = "SELL", last["upper_wick_ratio"]
    else:
        return None

    strength = _clamp(100.0 * wick_ratio)
    reliability = _clamp(50.0 * wick_ratio + 50.0 * min(range_to_atr, 1.5) / 1.5)
    return {"name": "wick_rejection", "direction": direction,
            "strength_score": round(strength, 1), "reliability_score": round(reliability, 1)}


def detect_liquidity_sweep(df: pd.DataFrame, lookback: int = 20,
                           atr_value: float | None = None) -> Dict[str, Any] | None:
    """
    Detects a liquidity sweep: price briefly pushes beyond a recent swing
    extreme (grabbing resting stop/liquidity orders) then closes back inside
    it within the same candle — a classic stop-hunt reversal pattern.
      Buy Side Sweep: high breaks above a recent high, closes back below it -> SELL
      Sell Side Sweep: low breaks below a recent low, closes back above it -> BUY
    "Fake Breakout" (the third named case in the spec) is covered by the
    separate detect_false_breakout() below, which uses the Phase 6 S/R zone
    engine rather than raw swing extremes — giving genuinely distinct
    market-structure framing instead of a duplicate check.
    """
    if len(df) < lookback + 2:
        return None
    window = df.tail(lookback + 1)
    prior = window.iloc[:-1]
    if len(prior) == 0:
        return None
    recent_high = float(prior["high"].max())
    recent_low = float(prior["low"].min())
    last = _candle_metrics(df.iloc[-1])
    atr_v = atr_value if (atr_value is not None and atr_value > 0) else _local_atr_proxy(df)

    if last["high"] > recent_high and last["close"] < recent_high:
        sweep_type, direction = "buy_side_sweep", "SELL"
        excess = last["high"] - recent_high
    elif last["low"] < recent_low and last["close"] > recent_low:
        sweep_type, direction = "sell_side_sweep", "BUY"
        excess = recent_low - last["low"]
    else:
        return None

    excess_to_atr = (excess / atr_v) if atr_v > 0 else 0.0
    range_to_atr = (last["range"] / atr_v) if atr_v > 0 else 1.0
    strength = _clamp(100.0 * min(excess_to_atr, 1.5) / 1.5)
    reliability = _clamp(
        40.0 * min(excess_to_atr, 1.0)
        + 30.0 * (1.0 - min(last["body_ratio"], 1.0))
        + 30.0 * min(range_to_atr, 1.5) / 1.5
    )
    return {"name": "liquidity_sweep", "sweep_type": sweep_type, "direction": direction,
            "strength_score": round(strength, 1), "reliability_score": round(reliability, 1)}


def detect_false_breakout(df: pd.DataFrame, sr_detail: Dict[str, Any] | None = None,
                          atr_value: float | None = None, lookback: int = 30) -> Dict[str, Any] | None:
    """
    Detects a false break of a known Support/Resistance zone (Phase 6):
    price breaks through the level intracandle but closes back on the
    original side — a rejection/reversal signal, distinct from
    detect_liquidity_sweep() above (which uses raw swing extremes, not the
    clustered S/R zone engine).
      False Support Break:    low < support,    close > support    -> BUY
      False Resistance Break: high > resistance, close < resistance -> SELL

    Reuses detect_support_resistance_zones() (Phase 6) rather than
    recomputing zones — pass sr_detail in if the caller already has it
    (e.g. from calculate_all()) to avoid redundant work.
    """
    if len(df) < 1:
        return None
    atr_v = atr_value if (atr_value is not None and atr_value > 0) else _local_atr_proxy(df)
    if sr_detail is None:
        sr_detail = detect_support_resistance_zones(df, lookback=lookback, atr_value=atr_v)

    last = _candle_metrics(df.iloc[-1])
    support = sr_detail.get("nearest_support")
    resistance = sr_detail.get("nearest_resistance")

    if support is not None and last["low"] < support and last["close"] > support:
        break_type, direction = "false_support_break", "BUY"
        excess = support - last["low"]
    elif resistance is not None and last["high"] > resistance and last["close"] < resistance:
        break_type, direction = "false_resistance_break", "SELL"
        excess = last["high"] - resistance
    else:
        return None

    excess_to_atr = (excess / atr_v) if atr_v > 0 else 0.0
    range_to_atr = (last["range"] / atr_v) if atr_v > 0 else 1.0
    zone_reliability = sr_detail.get("zone_reliability", 0) or 0
    strength = _clamp(100.0 * min(excess_to_atr, 1.5) / 1.5)
    reliability = _clamp(
        40.0 * min(excess_to_atr, 1.0)
        + 30.0 * (zone_reliability / 100.0)
        + 30.0 * min(range_to_atr, 1.5) / 1.5
    )
    return {"name": "false_breakout", "break_type": break_type, "direction": direction,
            "strength_score": round(strength, 1), "reliability_score": round(reliability, 1)}


# ─── Trend ───────────────────────────────────────────────────────────────────

def trend_direction(df: pd.DataFrame,
                    fast_period: int = 9,
                    slow_period: int = 21) -> Dict[str, Any]:
    """
    Classify trend using EMA crossover + price position vs EMA-50.
    Returns direction string and slope of fast EMA.
    """
    close  = df["close"]
    ema_fast = ema(close, fast_period)
    ema_slow = ema(close, slow_period)
    ema_50   = ema(close, 50)

    last_fast = ema_fast.iloc[-1]
    last_slow = ema_slow.iloc[-1]
    last_50   = ema_50.iloc[-1]
    last_price = close.iloc[-1]

    # Slope of fast EMA over last 5 bars
    if len(ema_fast) >= 5 and ema_fast.iloc[-5] != 0:
        slope = (ema_fast.iloc[-1] - ema_fast.iloc[-5]) / ema_fast.iloc[-5] * 100
    else:
        slope = 0.0
    slope = _safe_float(slope)

    bullish_signals = sum([
        last_fast > last_slow,
        last_price > last_50,
        slope > 0,
    ])
    bearish_signals = sum([
        last_fast < last_slow,
        last_price < last_50,
        slope < 0,
    ])

    if bullish_signals >= 2:
        direction = "BULLISH"
    elif bearish_signals >= 2:
        direction = "BEARISH"
    else:
        direction = "SIDEWAYS"

    return {
        "direction": direction,
        "ema_fast":  round(_safe_float(last_fast), 5),
        "ema_slow":  round(_safe_float(last_slow), 5),
        "slope_pct": round(_safe_float(slope), 4),
    }


# ─── Volatility ──────────────────────────────────────────────────────────────

def volatility(df: pd.DataFrame, atr_period: int = 14) -> Dict[str, float]:
    """
    Volatility summary:
    - ATR (absolute)
    - ATR % of price
    - BB width %
    - Historical volatility (annualised std of log-returns)
    """
    atr_vals = atr(df, atr_period)
    last_atr  = _safe_float(atr_vals.iloc[-1])
    last_price = _safe_float(df["close"].iloc[-1])
    atr_pct   = (last_atr / last_price * 100) if last_price else 0.0

    bb = bollinger_bands(df["close"])
    bb_width_pct = _safe_float(bb["width"].iloc[-1]) * 100

    log_ret = np.log(df["close"] / df["close"].shift(1)).replace([np.inf, -np.inf], np.nan).dropna()
    hist_vol = _safe_float(log_ret.std() * np.sqrt(252 * 24 * 60)) * 100 if len(log_ret) > 1 else 0.0

    level = "HIGH" if atr_pct > 0.5 else ("MEDIUM" if atr_pct > 0.15 else "LOW")

    return {
        "atr":          round(last_atr, 5),
        "atr_pct":      round(_safe_float(atr_pct), 3),
        "bb_width_pct": round(_safe_float(bb_width_pct), 3),
        "hist_vol_pct": round(_safe_float(hist_vol), 3),
        "level":        level,
    }


# ─── CCI ─────────────────────────────────────────────────────────────────────

def cci(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Commodity Channel Index."""
    typical_price = (df['high'] + df['low'] + df['close']) / 3
    sma = typical_price.rolling(period).mean()
    mean_dev = typical_price.rolling(period).apply(lambda x: (x - x.mean()).abs().mean())
    cci_val = (typical_price - sma) / (0.015 * mean_dev)
    return cci_val.replace([float('inf'), float('-inf')], 0).fillna(0)


# ─── Classic Stochastic Oscillator ─────────────────────────────────────────────

def stochastic_oscillator(df: pd.DataFrame, k_period: int = 14,
                          d_period: int = 3) -> Dict[str, pd.Series]:
    """
    Classic (price-based) Stochastic Oscillator — distinct from Stochastic RSI.
    """
    low_min = df['low'].rolling(k_period).min()
    high_max = df['high'].rolling(k_period).max()
    denom = (high_max - low_min).replace(0, float('nan'))
    k = 100 * (df['close'] - low_min) / denom
    k = k.fillna(50)
    d = k.rolling(d_period).mean().fillna(50)
    return {"k": k, "d": d}


# ─── Candlestick Pattern Detector ──────────────────────────────────────────────

def _candle_metrics(row: pd.Series) -> Dict[str, float]:
    """Shared per-candle geometry used by every pattern's scoring."""
    o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
    body = abs(c - o)
    range_ = h - l
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    return {
        "open": o, "high": h, "low": l, "close": c,
        "body": body, "range": range_,
        "upper_wick": upper_wick, "lower_wick": lower_wick,
        "body_ratio": (body / range_) if range_ > 0 else 0.0,
        "upper_wick_ratio": (upper_wick / range_) if range_ > 0 else 0.0,
        "lower_wick_ratio": (lower_wick / range_) if range_ > 0 else 0.0,
        "bullish": c > o,
        "bearish": c < o,
    }


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def _local_atr_proxy(df: pd.DataFrame, lookback: int = 14) -> float:
    """
    Cheap local volatility proxy used only when the caller doesn't already
    have a real ATR series/value on hand (e.g. a short backtest slice).
    Average true-range-ish measure over whatever history is available.
    Falls back to the current candle's own range if too little history.
    """
    window = df.tail(lookback)
    if len(window) == 0:
        return 0.0
    tr = (window["high"] - window["low"]).abs()
    val = float(tr.mean())
    return val if val > 0 else 0.0


def detect_candlestick_pattern_detailed(df: pd.DataFrame,
                                         atr_value: float | None = None) -> Dict[str, Any] | None:
    """
    Full candlestick pattern detector. Returns a structured result for the
    single highest-priority pattern matched on the most recent candle(s):

        {
            "name":              str,
            "direction":         "BUY" | "SELL" | "NEUTRAL",
            "strength_score":    0-100,   # how textbook/pronounced the shape is
            "reliability_score": 0-100,   # geometry-derived confidence, NOT a
                                           # fixed per-pattern-name lookup —
                                           # combines body/wick ratios, gap
                                           # quality, engulf %, and ATR-relative
                                           # significance. Not yet backtest-
                                           # calibrated (candidate follow-up).
        }
        (Inside Bar additionally includes "breakout_pending": True)

    Returns None if there isn't enough history or nothing matches.

    Priority order (first match wins, matching the exact original priority
    for the 4 pre-existing patterns so detect_candlestick_pattern() — the
    backward-compatible string wrapper below — is byte-identical to before):
      1. Doji
      2. Hammer
      3. Bullish Engulfing
      4. Bearish Engulfing
      5. Morning Star   (3-candle, checked before the single-candle
      6. Evening Star    star-shape patterns since it's the more specific /
                          rarer signal when both could apply)
      7. Inverted Hammer / Shooting Star (same geometry, disambiguated by a
                          short-term trend proxy — see below)
      8. Inside Bar
    """
    if len(df) < 2:
        return None

    last = _candle_metrics(df.iloc[-1])
    prev = _candle_metrics(df.iloc[-2])
    atr_v = atr_value if atr_value is not None else _local_atr_proxy(df)
    range_to_atr = (last["range"] / atr_v) if atr_v > 0 else 1.0  # neutral if no ATR available

    # ── 1. Doji (identical gate to the original: body/range < 0.1) ───────────
    if last["range"] > 0 and last["body_ratio"] < 0.1:
        strength = _clamp(100.0 * (1.0 - last["body_ratio"] / 0.1))
        symmetry = 1.0 - (abs(last["upper_wick"] - last["lower_wick"]) / last["range"])
        reliability = _clamp(50.0 * symmetry + 50.0 * min(range_to_atr, 1.5) / 1.5)
        return {"name": "doji", "direction": "NEUTRAL",
                "strength_score": round(strength, 1), "reliability_score": round(reliability, 1)}

    # ── 2. Hammer (identical gate to the original) ───────────────────────────
    if last["lower_wick"] > 2 * last["body"] and last["upper_wick"] < last["body"] and last["body"] > 0:
        wick_body_ratio = last["lower_wick"] / last["body"]
        strength = _clamp(100.0 * min(wick_body_ratio, 5.0) / 5.0)
        reliability = _clamp(
            40.0 * min(last["lower_wick_ratio"], 1.0)
            + 30.0 * (1.0 - min(last["upper_wick_ratio"] * 3, 1.0))
            + 30.0 * min(range_to_atr, 1.5) / 1.5
        )
        return {"name": "hammer", "direction": "BUY",
                "strength_score": round(strength, 1), "reliability_score": round(reliability, 1)}

    # ── 3. Bullish Engulfing (identical gate to the original) ────────────────
    if (prev["bearish"] and last["bullish"]
            and last["close"] > prev["open"] and last["open"] < prev["close"]):
        engulf_pct = ((last["body"] - prev["body"]) / prev["body"] * 100.0) if prev["body"] > 0 else 100.0
        strength = _clamp(engulf_pct)
        reliability = _clamp(
            40.0 * min(last["body_ratio"], 1.0)
            + 30.0 * min(engulf_pct / 100.0, 1.0)
            + 30.0 * min(range_to_atr, 1.5) / 1.5
        )
        return {"name": "bullish_engulfing", "direction": "BUY",
                "strength_score": round(strength, 1), "reliability_score": round(reliability, 1)}

    # ── 4. Bearish Engulfing (identical gate to the original) ────────────────
    if (prev["bullish"] and last["bearish"]
            and last["close"] < prev["open"] and last["open"] > prev["close"]):
        engulf_pct = ((last["body"] - prev["body"]) / prev["body"] * 100.0) if prev["body"] > 0 else 100.0
        strength = _clamp(engulf_pct)
        reliability = _clamp(
            40.0 * min(last["body_ratio"], 1.0)
            + 30.0 * min(engulf_pct / 100.0, 1.0)
            + 30.0 * min(range_to_atr, 1.5) / 1.5
        )
        return {"name": "bearish_engulfing", "direction": "SELL",
                "strength_score": round(strength, 1), "reliability_score": round(reliability, 1)}

    # ── 5/6. Morning Star / Evening Star (3-candle) ───────────────────────────
    if len(df) >= 3:
        c1 = _candle_metrics(df.iloc[-3])   # first candle (trend candle)
        c2 = _candle_metrics(df.iloc[-2])   # small "star" candle
        c3 = last                            # confirmation candle

        c1_mid = (c1["open"] + c1["close"]) / 2.0
        star_small = c2["range"] > 0 and c2["body_ratio"] < 0.35

        # Morning Star: bearish c1, small star, bullish c3 closing above c1's midpoint
        if c1["bearish"] and star_small and c3["bullish"] and c3["close"] > c1_mid and c3["body"] > 0:
            gap_dn = max(0.0, c1["close"] - max(c2["open"], c2["close"]))
            recovery = (c3["close"] - c1_mid) / c1["body"] if c1["body"] > 0 else 0.0
            strength = _clamp(100.0 * min(recovery, 1.5) / 1.5)
            reliability = _clamp(
                35.0 * (1.0 - min(c2["body_ratio"] / 0.35, 1.0))
                + 35.0 * min((gap_dn / atr_v) if atr_v > 0 else 0.0, 1.0)
                + 30.0 * min(range_to_atr, 1.5) / 1.5
            )
            return {"name": "morning_star", "direction": "BUY",
                    "strength_score": round(strength, 1), "reliability_score": round(reliability, 1)}

        # Evening Star: bullish c1, small star, bearish c3 closing below c1's midpoint
        if c1["bullish"] and star_small and c3["bearish"] and c3["close"] < c1_mid and c3["body"] > 0:
            gap_up = max(0.0, min(c2["open"], c2["close"]) - c1["close"])
            recovery = (c1_mid - c3["close"]) / c1["body"] if c1["body"] > 0 else 0.0
            strength = _clamp(100.0 * min(recovery, 1.5) / 1.5)
            reliability = _clamp(
                35.0 * (1.0 - min(c2["body_ratio"] / 0.35, 1.0))
                + 35.0 * min((gap_up / atr_v) if atr_v > 0 else 0.0, 1.0)
                + 30.0 * min(range_to_atr, 1.5) / 1.5
            )
            return {"name": "evening_star", "direction": "SELL",
                    "strength_score": round(strength, 1), "reliability_score": round(reliability, 1)}

    # ── 7. Inverted Hammer / Shooting Star (same geometry, disambiguated by a
    #        short-term trend proxy — 3-4 candle price drift if available) ────
    if last["upper_wick"] > 2 * last["body"] and last["lower_wick"] < last["body"] and last["body"] > 0:
        wick_body_ratio = last["upper_wick"] / last["body"]
        strength = _clamp(100.0 * min(wick_body_ratio, 5.0) / 5.0)
        reliability = _clamp(
            40.0 * min(last["upper_wick_ratio"], 1.0)
            + 30.0 * (1.0 - min(last["lower_wick_ratio"] * 3, 1.0))
            + 30.0 * min(range_to_atr, 1.5) / 1.5
        )
        close_col = df["close"]
        if len(close_col) >= 4:
            trend_proxy = close_col.iloc[-1] - close_col.iloc[-4]
        else:
            trend_proxy = close_col.iloc[-1] - close_col.iloc[0]
        if trend_proxy <= 0:
            # Prior short-term downtrend -> bullish reversal candidate
            return {"name": "inverted_hammer", "direction": "BUY",
                    "strength_score": round(strength, 1), "reliability_score": round(reliability, 1)}
        else:
            # Prior short-term uptrend -> bearish reversal candidate
            return {"name": "shooting_star", "direction": "SELL",
                    "strength_score": round(strength, 1), "reliability_score": round(reliability, 1)}

    # ── 8. Inside Bar (current candle fully contained within previous) ───────
    if last["high"] < prev["high"] and last["low"] > prev["low"] and prev["range"] > 0:
        compression = (prev["range"] - last["range"]) / prev["range"]
        strength = _clamp(100.0 * compression)
        reliability = _clamp(
            50.0 * min(range_to_atr, 1.5) / 1.5
            + 50.0 * min(prev["body_ratio"] * 2, 1.0)
        )
        return {"name": "inside_bar", "direction": "NEUTRAL", "breakout_pending": True,
                "strength_score": round(strength, 1), "reliability_score": round(reliability, 1)}

    return None


def detect_candlestick_pattern(df: pd.DataFrame) -> str | None:
    """
    Backward-compatible string API — kept for existing callers that only
    expect a pattern name. Thin wrapper around
    detect_candlestick_pattern_detailed(); the detection gates for doji,
    hammer, bullish_engulfing, and bearish_engulfing are copied verbatim
    (same conditions, same priority order) so this function's output is
    byte-identical to its pre-upgrade behavior for those 4 patterns.
    New patterns (inverted_hammer, shooting_star, morning_star, evening_star,
    inside_bar) can now also be returned as plain strings here — existing
    callers that only special-case the original 4 names simply treat any of
    these as an unrecognized/neutral pattern, exactly as they already treat
    any other undetected shape today.
    """
    detail = detect_candlestick_pattern_detailed(df)
    return detail["name"] if detail else None


# ─── Mean Reversion Strength ─────────────────────────────────────────────────

def mean_reversion_strength(df: pd.DataFrame,
                             bb_period: int = 20,
                             bb_std: float = 2.0) -> Dict[str, Any]:
    """
    How many Bollinger Band standard deviations the current close is from the
    midline.  Positive = above mean, negative = below mean.
    upper / lower are True when price touches or crosses the respective band.
    NaN-safe via _safe_float().
    """
    close    = df["close"]
    sma      = close.rolling(bb_period).mean()
    std      = close.rolling(bb_period).std(ddof=0)

    last_close = _safe_float(close.iloc[-1])
    last_sma   = _safe_float(sma.iloc[-1], default=last_close)
    last_std   = _safe_float(std.iloc[-1])

    strength   = 0.0 if last_std == 0 else (last_close - last_sma) / last_std
    strength   = _safe_float(strength)
    upper_band = last_sma + bb_std * last_std
    lower_band = last_sma - bb_std * last_std

    return {
        "strength": round(strength, 3),
        "upper":    bool(last_close >= upper_band),
        "lower":    bool(last_close <= lower_band),
    }


# ─── Consecutive Candle Exhaustion ───────────────────────────────────────────

def consecutive_candle_exhaustion(df: pd.DataFrame,
                                   lookback: int = 8) -> Dict[str, Any]:
    """
    Count how many consecutive same-direction candles end the last `lookback`
    bars.  Exhaustion = True when 5+ candles run in the same direction,
    signalling a stretched move likely to mean-revert.
    """
    if len(df) < 2:
        return {"count": 0, "direction": None, "exhaustion": False}

    recent = df.tail(lookback)
    opens  = recent["open"].values
    closes = recent["close"].values

    bull_count = 0
    bear_count = 0
    for i in range(len(closes) - 1, -1, -1):
        is_bull = closes[i] > opens[i]
        is_bear = closes[i] < opens[i]
        if is_bull:
            if bear_count > 0:
                break
            bull_count += 1
        elif is_bear:
            if bull_count > 0:
                break
            bear_count += 1
        else:          # doji / flat — breaks the streak
            break

    if bull_count > 0:
        direction, count = "bullish", bull_count
    elif bear_count > 0:
        direction, count = "bearish", bear_count
    else:
        direction, count = None, 0

    return {
        "count":      count,
        "direction":  direction,
        "exhaustion": count >= 5,
    }


# ─── Round Number Proximity ───────────────────────────────────────────────────

def round_number_proximity(df: pd.DataFrame,
                            pip_threshold: float = 0.0010) -> Dict[str, Any]:
    """
    Distance of the latest close from the nearest psychological round number.
    Scale auto-detected from price magnitude to handle JPY pairs, majors,
    sub-dollar crypto, etc.

    Returns near_round_number=True when distance <= pip_threshold, signalling
    the market is at a key psychological support/resistance level.
    """
    last_close = _safe_float(df["close"].iloc[-1])

    if last_close >= 100:
        round_unit = 1.0
    elif last_close >= 10:
        round_unit = 0.1
    elif last_close >= 1:
        round_unit = 0.01
    else:
        round_unit = 0.001

    nearest  = round(last_close / round_unit) * round_unit
    distance = abs(last_close - nearest)

    return {
        "distance":          round(_safe_float(distance), 6),
        "near_round_number": bool(distance <= pip_threshold),
    }


# ─── Williams %R ────────────────────────────────────────────────────────────

def williams_r(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Williams %R momentum oscillator.
    Range -100 (most oversold) .. 0 (most overbought).
    Distinct from the Stochastic Oscillator (inverted scale, no smoothing).
    Flat range (high == low over the lookback) is guarded to avoid 0/0 -> NaN,
    defaulting to -50 (neutral midpoint).
    """
    high_max = df["high"].rolling(period).max()
    low_min  = df["low"].rolling(period).min()
    denom = (high_max - low_min).replace(0, np.nan)
    wr = -100 * (high_max - df["close"]) / denom
    return wr.fillna(-50.0)


# ─── On-Balance Volume ───────────────────────────────────────────────────────

def obv(df: pd.DataFrame) -> pd.Series:
    """
    On-Balance Volume: running total of volume, added on up closes and
    subtracted on down closes. Flat closes contribute zero change.
    Cumulative by construction, so the scalar output is only meaningful
    relative to its own recent history (e.g. its own slope), not an
    absolute threshold.
    """
    close = df["close"]
    volume = df["volume"].fillna(0)
    direction = np.sign(close.diff().fillna(0))
    return (direction * volume).cumsum()


# ─── Rate of Change ──────────────────────────────────────────────────────────

def roc(series: pd.Series, period: int = 10) -> pd.Series:
    """
    Rate of Change: percentage price change over `period` bars.
    Guards division by zero (a zero-price reference bar) by defaulting to 0.
    """
    ref = series.shift(period)
    result = (series - ref) / ref.replace(0, np.nan) * 100
    return result.fillna(0.0)


# ─── Full suite ───────────────────────────────────────────────────────────────

def calculate_all(df: pd.DataFrame,
                  ema_periods=(9, 21, 50, 200),
                  rsi_period=14,
                  macd_fast=12, macd_slow=26, macd_signal=9,
                  bb_period=20, bb_std=2.0,
                  atr_period=14,
                  adx_period=14,
                  stoch_period=14,
                  sr_lookback=30,
                  cci_period=20,
                  stoch_k_period=14,
                  stoch_d_period=3) -> Dict[str, Any]:
    """
    Run every indicator and return a flat dict of scalar (latest-bar) values
    plus a few Series-valued keys for charting if needed.
    """
    close  = df["close"]
    result: Dict[str, Any] = {}

    # EMAs
    for p in ema_periods:
        key = f"ema_{p}"
        s = ema(close, p)
        result[key] = round(_safe_float(s.iloc[-1], default=_safe_float(close.iloc[-1])), 5)
        result[f"_{key}_series"] = s   # for charting

    # RSI
    rsi_s = rsi(close, rsi_period)
    result["rsi"]         = round(_safe_float(rsi_s.iloc[-1], default=50.0), 2)
    result["_rsi_series"] = rsi_s

    # MACD
    macd_d = macd(close, macd_fast, macd_slow, macd_signal)
    result["macd"]          = round(_safe_float(macd_d["macd"].iloc[-1]),      5)
    result["macd_signal"]   = round(_safe_float(macd_d["signal"].iloc[-1]),    5)
    result["macd_histogram"]= round(_safe_float(macd_d["histogram"].iloc[-1]), 5)

    # Bollinger Bands
    bb = bollinger_bands(close, bb_period, bb_std)
    last_price_for_bb = _safe_float(close.iloc[-1])
    result["bb_upper"]  = round(_safe_float(bb["upper"].iloc[-1],  default=last_price_for_bb), 5)
    result["bb_middle"] = round(_safe_float(bb["middle"].iloc[-1], default=last_price_for_bb), 5)
    result["bb_lower"]  = round(_safe_float(bb["lower"].iloc[-1],  default=last_price_for_bb), 5)
    result["bb_width"]  = round(_safe_float(bb["width"].iloc[-1]),  4)

    # ATR
    atr_s = atr(df, atr_period)
    result["atr"]         = round(_safe_float(atr_s.iloc[-1]), 5)
    result["_atr_series"] = atr_s

    # VWAP
    vwap_s = vwap(df)
    result["vwap"]         = round(_safe_float(vwap_s.iloc[-1], default=last_price_for_bb), 5)
    result["_vwap_series"] = vwap_s

    # ADX (dx/di can be NaN on a perfectly flat market -> 0/0; default to 0)
    adx_d = adx(df, adx_period)
    result["adx"]      = round(_safe_float(adx_d["adx"].iloc[-1]),  2)
    result["di_plus"]  = round(_safe_float(adx_d["+di"].iloc[-1]), 2)
    result["di_minus"] = round(_safe_float(adx_d["-di"].iloc[-1]), 2)

    # Stochastic RSI (min==max -> 0/0 when RSI is flat; default to neutral 50)
    stoch = stochastic_rsi(close, rsi_period, stoch_period)
    result["stoch_rsi_k"] = round(_safe_float(stoch["k"].iloc[-1], default=50.0), 2)
    result["stoch_rsi_d"] = round(_safe_float(stoch["d"].iloc[-1], default=50.0), 2)

    # Support & Resistance
    sr = support_resistance(df, sr_lookback)
    result.update(sr)
    # Phase 6: new, additive — richer zone-based structure. Reuses the ATR
    # and ADX values already computed above (result["atr"], result["adx"])
    # instead of recomputing them.
    result["support_resistance_detail"] = detect_support_resistance_zones(
        df, lookback=sr_lookback, atr_value=result.get("atr"), adx_value=result.get("adx")
    )

    # Phase 7.3 Part 3: new indicators — additive keys only, NOT yet part of
    # the confluence vote (see indicators.py module comment above these
    # functions). Reuses the ATR already computed and the support_resistance
    # detail just computed, avoiding redundant work.
    result["wick_rejection_detail"] = detect_wick_rejection(df, atr_value=result.get("atr"))
    result["liquidity_sweep_detail"] = detect_liquidity_sweep(df, atr_value=result.get("atr"))
    result["false_breakout_detail"] = detect_false_breakout(
        df, sr_detail=result["support_resistance_detail"], atr_value=result.get("atr")
    )

    # Trend
    trend = trend_direction(df)
    result.update(trend)

    # Volatility
    vol = volatility(df, atr_period)
    result.update(vol)

    # CCI
    cci_s = cci(df, cci_period)
    result["cci"]         = round(_safe_float(cci_s.iloc[-1]), 2)
    result["_cci_series"] = cci_s

    # Classic Stochastic Oscillator (price-based, distinct from Stochastic RSI)
    stoch_osc = stochastic_oscillator(df, stoch_k_period, stoch_d_period)
    result["stoch_k"] = round(_safe_float(stoch_osc["k"].iloc[-1], default=50.0), 2)
    result["stoch_d"] = round(_safe_float(stoch_osc["d"].iloc[-1], default=50.0), 2)
    result["_stoch_k_series"] = stoch_osc["k"]   # for confluence/backtest crossover checks
    result["_stoch_d_series"] = stoch_osc["d"]

    # Candlestick pattern
    result["candlestick_pattern"] = detect_candlestick_pattern(df)
    result["candlestick_pattern_detail"] = detect_candlestick_pattern_detailed(df, atr_value=result["atr"])

    # Mean Reversion Strength (new factor 6)
    mr = mean_reversion_strength(df, bb_period, bb_std)
    result["mr_strength"] = mr["strength"]
    result["mr_upper"]    = mr["upper"]
    result["mr_lower"]    = mr["lower"]

    # Consecutive Candle Exhaustion (new factor 7)
    exh = consecutive_candle_exhaustion(df)
    result["exh_count"]      = exh["count"]
    result["exh_direction"]  = exh["direction"]
    result["exh_exhaustion"] = exh["exhaustion"]

    # Round Number Proximity (new factor 8)
    rnp = round_number_proximity(df)
    result["rnp_distance"]          = rnp["distance"]
    result["rnp_near_round_number"] = rnp["near_round_number"]

    # Williams %R (Phase 1)
    wr_s = williams_r(df, 14)
    result["williams_r"] = round(_safe_float(wr_s.iloc[-1], default=-50.0), 2)

    # On-Balance Volume (Phase 1)
    obv_s = obv(df)
    result["obv"] = round(_safe_float(obv_s.iloc[-1]), 2)
    result["_obv_series"] = obv_s   # for confluence/backtest divergence checks (Step 4)

    # Rate of Change (Phase 1)
    roc_s = roc(close, 10)
    result["roc"] = round(_safe_float(roc_s.iloc[-1]), 3)

    # Current price
    result["price"]  = round(_safe_float(close.iloc[-1]),  5)
    result["volume"] = round(_safe_float(df["volume"].iloc[-1]), 2)

    return result
