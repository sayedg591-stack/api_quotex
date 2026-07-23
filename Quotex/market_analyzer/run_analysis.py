#!/usr/bin/env python3
"""
Quotex Market Data & Analysis System
=====================================
Connects to Quotex, fetches historical candles, optionally streams live
quotes, calculates technical indicators, and prints a full analysis report.

Usage (from workspace root):
    python market_analyzer/run_analysis.py

Prerequisites:
    1. Run:  python quotex/login.py   (logs in and saves session.json)
    2. Then: python market_analyzer/run_analysis.py

NO trades are placed — this is a market analysis tool only.
"""

from __future__ import annotations
import sys
import os
import asyncio
from pathlib import Path

# ── Path bootstrap ───────────────────────────────────────────────────────────
_MARKET_DIR = Path(__file__).resolve().parent   # market_analyzer/
_ROOT       = _MARKET_DIR.parent                # workspace root

# Add market_analyzer/ first so sibling imports (config, fetch_data, etc.) work
if str(_MARKET_DIR) not in sys.path:
    sys.path.insert(0, str(_MARKET_DIR))

# Add quotex/ so api_quotex is importable
_QUOTEX_DIR = _ROOT / "quotex"
if str(_QUOTEX_DIR) not in sys.path:
    sys.path.insert(0, str(_QUOTEX_DIR))

# ── Application imports (after path setup) ────────────────────────────────────
import config as cfg
from fetch_data import QuotexDataFetcher
from indicators import calculate_all
from analyzer import generate_signal, generate_confluence_signal, print_report
from backtest import backtest_factor_accuracy, compute_dynamic_weights


# ─── Main flow ────────────────────────────────────────────────────────────────

async def run_single_asset(asset: str, timeframe: str) -> dict | None:
    """
    Full pipeline for one asset / timeframe combination.
    1. Connect & authenticate
    2. Fetch candle history
    3. (Optional) Stream live quotes
    4. Calculate all indicators
    5. Generate signal
    6. Print report
    7. Save candles.csv
    """
    print(f"\n{'─'*60}")
    print(f"  Analysis: {asset}  [{timeframe}]")
    print(f"{'─'*60}")

    fetcher = QuotexDataFetcher(asset=asset, is_demo=cfg.IS_DEMO)

    try:
        # ── 1. Connect ───────────────────────────────────────────────────────
        await fetcher.connect()

        # ── 2. Fetch candle history ───────────────────────────────────────────
        df = await fetcher.get_candles_df(
            asset=asset,
            timeframe=timeframe,
            count=cfg.CANDLE_COUNT,
        )

        if df.empty:
            print(f"\n  ⚠  No candles received for {asset} [{timeframe}]. "
                  "The asset may be closed or unavailable right now.\n")
            return

        # ── 3. Save candles CSV ───────────────────────────────────────────────
        csv_path = fetcher.save_candles_csv(df)
        print(f"  → Candles saved to: {csv_path}")

        # ── 4. Optional live streaming ────────────────────────────────────────
        live_prices: list = []
        if cfg.LIVE_STREAM_SECONDS > 0:
            live_prices = await fetcher.subscribe_live(
                asset=asset,
                timeframe=timeframe,
                duration=cfg.LIVE_STREAM_SECONDS,
            )

        # ── 5. Calculate indicators (OTC-tuned settings auto-applied) ─────────
        otc_settings = cfg.get_indicator_settings(asset)
        is_otc = "_otc" in asset.lower()
        print(f"  → Calculating indicators … "
              f"({'OTC-tuned' if is_otc else 'default'} settings)")
        # TASK 1: print actual settings values so user can verify which were used
        print(f"     Settings: BB({otc_settings['bb_period']}, std={otc_settings['bb_std']}), "
              f"RSI({otc_settings['rsi_period']}), "
              f"Stoch(K={otc_settings['stoch_k_period']}/D={otc_settings['stoch_d_period']}), "
              f"CCI({otc_settings['cci_period']})")
        indicators = calculate_all(
            df,
            ema_periods=cfg.EMA_PERIODS,
            rsi_period=otc_settings["rsi_period"],
            macd_fast=cfg.MACD_FAST,
            macd_slow=cfg.MACD_SLOW,
            macd_signal=cfg.MACD_SIGNAL,
            bb_period=otc_settings["bb_period"],
            bb_std=otc_settings["bb_std"],
            atr_period=cfg.ATR_PERIOD,
            adx_period=cfg.ADX_PERIOD,
            stoch_period=cfg.STOCH_RSI_PERIOD,
            sr_lookback=cfg.SR_LOOKBACK,
            cci_period=otc_settings["cci_period"],
            stoch_k_period=otc_settings["stoch_k_period"],
            stoch_d_period=otc_settings["stoch_d_period"],
        )

        # ── 6. Generate signal (legacy indicator-vote signal) ──────────────────
        signal_result = generate_signal(indicators)

        # ── 6b. Backtest factor accuracy -> dynamic weights -> confluence signal
        print("  → Backtesting confluence factors on fetched history …")
        accuracies = backtest_factor_accuracy(df, indicators, lookahead=4)
        dynamic_weights = compute_dynamic_weights(accuracies)

        # TASK 2: print weight summary — dynamic vs default, accuracy per factor
        _FACTOR_LABELS_MAP = {
            "bb": "BB Bounce", "rsi_div": "RSI Divergence",
            "stoch": "Stochastic Cross", "cci": "CCI Extreme", "candle": "Candlestick",
        }
        _DEFAULT_W = {"bb": 30, "rsi_div": 25, "stoch": 20, "cci": 15, "candle": 10}
        all_default = all(v is None for v in accuracies.values())
        weights_mode = "ALL DEFAULT (< 10 backtest signals)" if all_default else "DYNAMIC (backtest-weighted)"
        print(f"  → Weights used: {weights_mode}")
        for _fk, _flabel in _FACTOR_LABELS_MAP.items():
            _acc = accuracies.get(_fk)
            _w = dynamic_weights.get(_fk, 0)
            _dw = _DEFAULT_W.get(_fk, 0)
            if _acc is None:
                print(f"     {_flabel:<18}: accuracy=n/a (< 10 signals)  weight={_w:.1f} [default]")
            else:
                tag = "dynamic" if abs(_w - _dw) > 0.01 else "same as default"
                print(f"     {_flabel:<18}: accuracy={_acc:.1f}%  weight={_w:.1f} [{tag}]")

        confluence_result = generate_confluence_signal(df, indicators, dynamic_weights)

        # ── 7. Print report ───────────────────────────────────────────────────
        print_report(
            asset=asset,
            timeframe=timeframe,
            ind=indicators,
            sig=signal_result,
            candle_count=len(df),
            live_prices=live_prices,
            confluence=confluence_result,
            factor_accuracies=accuracies,
        )

        # TASK 4: return result so main() can do multi-tf comparison
        return {"timeframe": timeframe, "confluence": confluence_result}

    except FileNotFoundError as e:
        print(f"\n{e}\n")
        sys.exit(1)
    except ValueError as e:
        print(f"\n{e}\n")
        sys.exit(1)
    except ConnectionError as e:
        print(f"\n[CONNECTION ERROR] {e}\n")
    except Exception as e:
        print(f"\n[ERROR] {e}\n")
        import traceback
        traceback.print_exc()
    finally:
        await fetcher.disconnect()


async def main() -> None:
    """
    Analyze the configured asset across all analysis timeframes.
    The primary timeframe is always analyzed last (most detail).
    """
    print("\n" + "═" * 60)
    print("  QUOTEX MARKET ANALYZER  —  Analysis-only, no trades")
    print("═" * 60)
    print(f"  Asset  : {cfg.DEFAULT_ASSET}")
    print(f"  Account: {'DEMO' if cfg.IS_DEMO else 'LIVE'}")
    print(f"  Session: {cfg.SESSION_FILE}")

    # Run all requested timeframes for the default asset
    # TASK 4: collect results for multi-timeframe confirmation
    tf_results: dict = {}
    for tf in cfg.ANALYSIS_TIMEFRAMES:
        result = await run_single_asset(cfg.DEFAULT_ASSET, tf)
        if result:
            tf_results[tf] = result

    # ── TASK 4: Multi-timeframe confirmation check (1m vs 5m) ────────────────
    tf1 = tf_results.get("1m")
    tf5 = tf_results.get("5m")
    if tf1 and tf5:
        s1 = tf1["confluence"]["signal"]
        s5 = tf5["confluence"]["signal"]
        c1 = tf1["confluence"]["confidence"]
        c5 = tf5["confluence"]["confidence"]
        print("\n" + "─" * 60)
        print("  MULTI-TIMEFRAME CONFIRMATION (1m vs 5m)")
        print("─" * 60)
        print(f"  1m  signal : {s1:<5}  ({c1}% confidence)")
        print(f"  5m  signal : {s5:<5}  ({c5}% confidence)")
        if s1 != "WAIT" and s5 != "WAIT" and s1 == s5:
            boosted = min(95, max(c1, c5) + 10)
            print(f"  ✅ CONFIRMED — both timeframes agree: {s1}")
            print(f"     Boosted confidence: {boosted}% (capped at 95%)")
        elif s1 == "WAIT" or s5 == "WAIT":
            print("  ⏳ PARTIAL — at least one timeframe is WAIT.")
            print("     Hold — wait for clearer signal alignment before acting.")
        else:
            print(f"  ⚠  CONFLICTING — 1m={s1} but 5m={s5}")
            print("     Do NOT act — wait for both timeframes to align.")

    print("\n✓ Analysis complete.\n")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n  Interrupted by user.\n")
