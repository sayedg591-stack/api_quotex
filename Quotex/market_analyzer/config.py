"""
Configuration for the Quotex Market Analyzer.
Edit this file to change the asset, timeframes, and indicator settings.
"""

# ─── Asset ───────────────────────────────────────────────────────────────────
# Any symbol from quotex/api_quotex/constants.py ASSETS dict is valid.
DEFAULT_ASSET = "EURUSD_otc"

# ─── Account ─────────────────────────────────────────────────────────────────
IS_DEMO = True          # True = demo account, False = live account

# ─── Session ─────────────────────────────────────────────────────────────────
# Path to session.json written by quotex/login.py (relative to workspace root)
SESSION_FILE = "quotex/session.json"

# ─── Timeframes to analyze ────────────────────────────────────────────────────
# Key = human label used in output; Value = seconds (Quotex API timeframe).
# Available: 30s, 1m, 2m, 3m, 5m, 10m, 15m, 30m, 45m, 1h, 2h, 3h, 4h
ANALYSIS_TIMEFRAMES = ["1m", "5m", "15m"]

# Main timeframe used for full indicator analysis and the report
PRIMARY_TIMEFRAME = "5m"

# ─── Candle history ───────────────────────────────────────────────────────────
# Number of historical candles to download per timeframe.
# Keep ≥ 200 for EMA-200 to be meaningful.
CANDLE_COUNT = 250

# ─── Live streaming ───────────────────────────────────────────────────────────
# Duration in seconds to stream live quotes after fetching history.
# Set to 0 to skip live streaming.
LIVE_STREAM_SECONDS = 15

# ─── Output ───────────────────────────────────────────────────────────────────
OUTPUT_DIR = "market_analyzer/output"
CSV_FILENAME = "candles.csv"

# ─── Indicators ───────────────────────────────────────────────────────────────
EMA_PERIODS    = [9, 21, 50, 200]
RSI_PERIOD     = 14
MACD_FAST      = 12
MACD_SLOW      = 26
MACD_SIGNAL    = 9
BB_PERIOD      = 20
BB_STD         = 2.0
ATR_PERIOD     = 14
ADX_PERIOD     = 14
STOCH_RSI_PERIOD = 14   # RSI period; stochastic window also = 14
SR_LOOKBACK    = 30     # candles to scan for support / resistance levels

# ─── Signal thresholds ────────────────────────────────────────────────────────
RSI_OVERSOLD   = 30
RSI_OVERBOUGHT = 70
ADX_TRENDING   = 25     # ADX above this = trending market
STOCH_RSI_LO   = 20
STOCH_RSI_HI   = 80

# ─── Phase 7: Smart Scanner ───────────────────────────────────────────────────
MIN_PAYOUT = 80.0   # minimum payout % for the scanner's payout hard gate

# ─── OTC-tuned indicator settings ──────────────────────────────────────────────
# OTC assets (name contains "_otc") trade on synthetic/weekend feeds with
# different volatility characteristics than live-market pairs, so they get
# tighter/faster indicator periods. Use get_indicator_settings(asset) below
# to auto-select the right settings for a given asset name.
OTC_INDICATOR_SETTINGS = {
    "bb_period": 20, "bb_std": 1.8,
    "rsi_period": 9,
    "stoch_k_period": 5, "stoch_d_period": 3,
    "cci_period": 14,
}
DEFAULT_INDICATOR_SETTINGS = {
    "bb_period": 20, "bb_std": 2.0,
    "rsi_period": 14,
    "stoch_k_period": 14, "stoch_d_period": 3,
    "cci_period": 20,
}


def get_indicator_settings(asset: str) -> dict:
    """Return OTC-tuned settings if `asset` is an OTC symbol, else defaults."""
    if asset and "_otc" in asset.lower():
        return dict(OTC_INDICATOR_SETTINGS)
    return dict(DEFAULT_INDICATOR_SETTINGS)
