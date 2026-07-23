"""
Deep Backtest — Task 5
======================
Runs factor accuracy tests across multiple assets and timeframes,
saves averaged weights to backtest_results.json.

Usage:
    cd Market-Signal-Generator/Quotex
    python -m market_analyzer.deep_backtest
        OR
    python market_analyzer/deep_backtest.py

Output: market_analyzer/backtest_results.json
  → generate_confluence_signal() in analyzer.py automatically loads this
    file on each signal run (precomputed weights override per-run weights).

NEVER places orders — analysis only.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── Path setup ───────────────────────────────────────────────────────────────
_SELF      = Path(__file__).resolve()
_MARKET_DIR = _SELF.parent          # market_analyzer/
_ROOT       = _MARKET_DIR.parent    # Quotex/
_QUOTEX_DIR = _ROOT / "quotex"

for _p in (_MARKET_DIR, _QUOTEX_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from fetch_data import QuotexDataFetcher
from indicators import calculate_all
from backtest import backtest_factor_accuracy, compute_dynamic_weights
import config as cfg

# ── Settings ─────────────────────────────────────────────────────────────────

# 3 representative OTC pairs covering different session characteristics
ASSETS_TO_TEST: List[str] = [
    "EURUSD_otc",
    "GBPUSD_otc",
    "USDJPY_otc",
]

# Core intraday timeframes (short enough to be practical in a single run)
TIMEFRAMES_TO_TEST: List[str] = ["1m", "5m", "15m"]

FACTOR_KEYS: List[str] = ["bb", "rsi_div", "stoch", "cci", "candle"]

OUTPUT_FILE: Path = _MARKET_DIR / "backtest_results.json"


# ── Runner ───────────────────────────────────────────────────────────────────

async def _run_deep_backtest() -> Optional[Dict[str, float]]:
    """
    Connect once, iterate over all (asset, timeframe) pairs, collect per-run
    accuracy + weight dicts, then compute cross-run averages and save to JSON.

    Returns the average_weights dict on success, None on total failure.
    """
    run_results: List[Dict[str, Any]] = []
    fetcher = QuotexDataFetcher()

    try:
        await fetcher.connect()

        for asset in ASSETS_TO_TEST:
            otc_settings = cfg.get_indicator_settings(asset)
            for tf in TIMEFRAMES_TO_TEST:
                print(f"  ▶ {asset} [{tf}] …", end=" ", flush=True)
                try:
                    df = await fetcher.get_candles_df(
                        asset=asset, timeframe=tf, count=cfg.CANDLE_COUNT
                    )
                    if df.empty:
                        print("⚠ no candles — skipped")
                        continue

                    ind = calculate_all(
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
                    accuracies = backtest_factor_accuracy(df, ind, lookahead=4)
                    weights    = compute_dynamic_weights(accuracies)

                    run_results.append({
                        "asset":      asset,
                        "timeframe":  tf,
                        "candles":    len(df),
                        "accuracies": accuracies,
                        "weights":    weights,
                    })
                    print(f"✓  ({len(df)} candles)")

                except Exception as exc:
                    print(f"✗ error: {exc}")

    finally:
        await fetcher.disconnect()

    if not run_results:
        print("\n⚠  No runs completed — backtest_results.json NOT updated.")
        return None

    # ── Compute cross-run average weights ────────────────────────────────────
    avg_weights: Dict[str, float] = {}
    for k in FACTOR_KEYS:
        vals = [r["weights"].get(k, 0.0) for r in run_results]
        avg_weights[k] = round(sum(vals) / len(vals), 2)

    # Normalise to exactly 100
    total = sum(avg_weights.values())
    if total > 0:
        avg_weights = {k: round(v / total * 100, 2) for k, v in avg_weights.items()}
    diff = round(100.0 - sum(avg_weights.values()), 2)
    if abs(diff) >= 0.01 and avg_weights:
        best = max(avg_weights, key=avg_weights.get)
        avg_weights[best] = round(avg_weights[best] + diff, 2)

    # ── Compute cross-run average accuracies (for summary table) ─────────────
    avg_accuracies: Dict[str, Optional[float]] = {}
    for k in FACTOR_KEYS:
        vals = [r["accuracies"].get(k) for r in run_results if r["accuracies"].get(k) is not None]
        avg_accuracies[k] = round(sum(vals) / len(vals), 1) if vals else None

    output = {
        "runs":               len(run_results),
        "assets_tested":      ASSETS_TO_TEST,
        "timeframes_tested":  TIMEFRAMES_TO_TEST,
        "average_weights":    avg_weights,
        "average_accuracies": avg_accuracies,
        "per_run_results":    run_results,
    }
    OUTPUT_FILE.write_text(json.dumps(output, indent=2, default=str))

    # ── Print summary ─────────────────────────────────────────────────────────
    print(f"\n✅  Saved {OUTPUT_FILE.name}  ({len(run_results)} runs across "
          f"{len(ASSETS_TO_TEST)} assets × {len(TIMEFRAMES_TO_TEST)} timeframes)")
    print(f"\n  {'Factor':<14} {'Avg Accuracy':>13} {'Avg Weight':>11}")
    print(f"  {'-'*14}-+-{'-'*13}-+-{'-'*11}")
    for k in FACTOR_KEYS:
        acc = avg_accuracies.get(k)
        acc_s = f"{acc}%" if acc is not None else "N/A"
        print(f"  {k:<14} {acc_s:>13} {avg_weights.get(k, 0):>10}%")

    best = max(avg_weights, key=avg_weights.get)
    print(f"\n  🏆 Best factor: {best}  (weight {avg_weights[best]}%)")
    print(f"\n  Note: Run again after market hours for more stable results.")

    return avg_weights


def run_deep_backtest() -> Optional[Dict[str, float]]:
    """Synchronous entry point."""
    return asyncio.run(_run_deep_backtest())


if __name__ == "__main__":
    print("\n══ Deep Backtest — Quotex Market Analyzer ══")
    print(f"  Assets    : {ASSETS_TO_TEST}")
    print(f"  Timeframes: {TIMEFRAMES_TO_TEST}")
    print(f"  Output    : {OUTPUT_FILE}\n")
    run_deep_backtest()
