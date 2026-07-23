#!/usr/bin/env python3
"""
Quotex Market Analyzer — Web Dashboard
========================================
Flask front-end for the analysis-only pipeline in market_analyzer/.
Runs the exact same fetch -> indicators -> backtest -> confluence-signal
flow as run_analysis.py and renders the result in a browser.

There is NO trade-placement button or code path anywhere in this app —
only signal generation and display, same as the rest of the project.
"""

from __future__ import annotations

import os
import sys
import json
import asyncio
import threading
import time
from pathlib import Path
from typing import Any, Dict

# ── Path bootstrap (mirrors run_analysis.py) ───────────────────────────────────
_WEBAPP_DIR = Path(__file__).resolve().parent      # market_analyzer/webapp/
_MARKET_DIR = _WEBAPP_DIR.parent                    # market_analyzer/
_ROOT       = _MARKET_DIR.parent                    # workspace root (Quotex/)
_QUOTEX_DIR = _ROOT / "quotex"                      # quotex/

for _p in (_MARKET_DIR, _QUOTEX_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from flask import Flask, render_template, request, jsonify

import config as cfg
from fetch_data import QuotexDataFetcher
from indicators import calculate_all
from analyzer import (
    generate_signal, generate_confluence_signal, load_precomputed_weights,
    calculate_filter_score, DEFAULT_CONFLUENCE_WEIGHTS,
)
from backtest import backtest_factor_accuracy, compute_dynamic_weights
from api_quotex.constants import ASSETS
from scanner import ScannerEngine, ScannerConfig

# Phase 7.4 — Settings Store / Backtest Engine / Indicator Registry.
# Purely additive infrastructure: none of these modules touch analyzer.py,
# scanner.py, backtest.py's internals, or the Quotex API/WebSocket/login code.
from settings_store import SettingsStore
from backtest_engine import (
    BacktestEngine, evaluate_apply_conditions,
    STOPPED as _BT_STOPPED, CANDLE_OPTIONS as BACKTEST_CANDLE_OPTIONS,
)
import indicator_registry as ind_registry

app = Flask(__name__)

TIMEFRAMES = ["30s", "1m", "2m", "3m", "5m", "10m", "15m", "30m", "45m", "1h"]

_FACTOR_LABELS = {
    "bb": "BB Bounce",
    "rsi_div": "RSI Divergence",
    "stoch": "Stochastic Cross",
    "cci": "CCI Extreme",
    "candle": "Candlestick",
    "obv": "OBV Divergence",  # Step 4: new factor, additive only
    "sr": "Support/Resistance Zone",  # Phase 6: new factor, additive only
}
_VOTE_LABELS = {1: "BULLISH", -1: "BEARISH", 0: "NEUTRAL"}


def _asset_choices() -> Dict[str, list]:
    """Split the asset table into OTC / Live groups for the dropdown."""
    otc = sorted(a for a in ASSETS if a.lower().endswith("_otc"))
    live = sorted(a for a in ASSETS if not a.lower().endswith("_otc"))
    return {"otc": otc, "live": live}


# ── Timing helpers ────────────────────────────────────────────────────────────

def _ts() -> float:
    return time.monotonic()

def _log_step(label: str, t0: float, t_prev: float) -> float:
    now = _ts()
    print(f"  [PERF] {label:<40} step={now - t_prev:6.2f}s  total={now - t0:6.2f}s")
    return now


# ── Fix 3: single shared asyncio event loop running in a background thread ───
#
# Flask sync workers call asyncio.run_coroutine_threadsafe() to submit work to
# this loop.  Because all WebSocket I/O runs cooperatively inside one loop,
# only one /api/signal request is ever in flight at the asyncio level — no
# thread-level locking needed.  The loop also owns the persistent fetcher, so
# there is no per-request connect/disconnect overhead.

_BG_LOOP: asyncio.AbstractEventLoop | None = None
_BG_THREAD: threading.Thread | None = None
_BG_LOCK = threading.Lock()


def _get_bg_loop() -> asyncio.AbstractEventLoop:
    """Return the shared background asyncio loop, starting it if needed."""
    global _BG_LOOP, _BG_THREAD
    with _BG_LOCK:
        if _BG_LOOP is not None and _BG_LOOP.is_running():
            return _BG_LOOP
        _BG_LOOP = asyncio.new_event_loop()
        _BG_THREAD = threading.Thread(
            target=_BG_LOOP.run_forever,
            daemon=True,
            name="quotex-ws-loop",
        )
        _BG_THREAD.start()
    return _BG_LOOP


def _run_bg(coro, timeout: float = 110.0):
    """Submit a coroutine to the background loop; block until it finishes."""
    loop = _get_bg_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)


# ── Persistent shared fetcher (lives on _BG_LOOP) ────────────────────────────

_shared_fetcher: "QuotexDataFetcher | None" = None
# asyncio.Lock created lazily inside the bg loop to avoid event-loop binding
# issues at import time.
_fetcher_alock: asyncio.Lock | None = None


def _fetcher_lock() -> asyncio.Lock:
    global _fetcher_alock
    if _fetcher_alock is None:
        _fetcher_alock = asyncio.Lock()
    return _fetcher_alock


def _fetcher_is_alive() -> bool:
    """Quick probe — no I/O, just check the client flag."""
    f = _shared_fetcher
    try:
        return (
            f is not None
            and f._client is not None
            and bool(getattr(f._client, "websocket_is_connected", False))
        )
    except Exception:
        return False


async def _get_shared_fetcher() -> "QuotexDataFetcher":
    """
    Return the persistent fetcher, (re-)connecting only when the WebSocket
    is not alive.  Must only be awaited from inside _BG_LOOP.
    NEVER places orders — connection is for data retrieval only.
    """
    global _shared_fetcher
    async with _fetcher_lock():
        if not _fetcher_is_alive():
            if _shared_fetcher is not None:
                try:
                    await _shared_fetcher.disconnect()
                except Exception:
                    pass
            print("[CONN] (Re-)connecting persistent fetcher …")
            t_c = _ts()
            _shared_fetcher = QuotexDataFetcher(asset=cfg.DEFAULT_ASSET, is_demo=cfg.IS_DEMO)
            await _shared_fetcher.connect()
            print(f"[CONN] Persistent fetcher ready — connect took {_ts() - t_c:.2f}s")
    return _shared_fetcher


def _invalidate_shared_fetcher() -> None:
    """Mark the persistent fetcher as dead so the next call reconnects."""
    global _shared_fetcher
    _shared_fetcher = None


async def _run_pipeline(asset: str, timeframe: str) -> Dict[str, Any]:
    """
    Full analysis-only pipeline for one asset/timeframe:
      connect -> fetch candles -> indicators (OTC-tuned if applicable)
      -> backtest factor accuracy -> dynamic weights -> confluence signal.
    NEVER places an order — read-only market data + local computation only.
    Runs on the shared background event loop; reuses the persistent fetcher.
    """
    t0 = t = _ts()
    print(f"\n[PERF] ── pipeline start  asset={asset}  tf={timeframe} ──")

    fetcher = await _get_shared_fetcher()
    t = _log_step("connect() [shared — reused or reconnected]", t0, t)

    try:

        df = await fetcher.get_candles_df(asset=asset, timeframe=timeframe, count=cfg.CANDLE_COUNT)
        t = _log_step(f"get_candles_df({timeframe})", t0, t)

        if df.empty:
            return {"error": f"No candles received for {asset} [{timeframe}]. "
                              "The asset may be closed or unavailable right now."}

        otc_settings = cfg.get_indicator_settings(asset)
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
        t = _log_step("calculate_all(indicators)", t0, t)

        # Task 3: Fetch payout % (best-effort — 0.0 = unavailable / closed)
        payout_pct = await fetcher.get_payout(asset, timeframe)
        t = _log_step("get_payout()", t0, t)

        legacy_signal = generate_signal(indicators)
        accuracies = backtest_factor_accuracy(df, indicators, lookahead=4)
        dynamic_weights = compute_dynamic_weights(accuracies)
        t = _log_step("backtest + dynamic_weights", t0, t)

        # Task 5: Use precomputed deep-backtest weights when available (more stable
        # cross-asset averages vs. per-run single-asset weights)
        _precomputed = load_precomputed_weights()
        if _precomputed:
            dynamic_weights = _precomputed

        # Phase 7.4: apply Settings' indicator enable/disable + weight-scale
        # on top of the dynamic_weights computed above — see
        # _apply_settings_weight_overrides()'s docstring for why this is a
        # scaling layer rather than a direct replacement. No-op at defaults.
        _settings_snapshot = settings_store.get()
        dynamic_weights = _apply_settings_weight_overrides(dynamic_weights, _settings_snapshot)

        confluence = generate_confluence_signal(df, indicators, dynamic_weights)
        t = _log_step("confluence_signal", t0, t)

        # ── TASK 4: Multi-timeframe confirmation (auto-compare with 1m) ────────
        multi_tf_result: Dict[str, Any] | None = None
        compare_tf = "1m" if timeframe != "1m" else "5m"

        # Step 2 (transparency only): always-populated explicit status,
        # separate from `multi_tf_result` above. Does not affect signal,
        # confidence, or the WAIT-override logic below — it only reports on
        # what happened. Default assumes unavailable until proven otherwise.
        multi_tf_status: Dict[str, Any] = {
            "status": "UNAVAILABLE",
            "reason": "Multi-timeframe check did not run",
            "timeframe_checked": compare_tf,
        }
        try:
            df_cmp = await fetcher.get_candles_df(
                asset=asset, timeframe=compare_tf, count=cfg.CANDLE_COUNT
            )
            if not df_cmp.empty:
                try:
                    ind_cmp = calculate_all(
                        df_cmp,
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
                    acc_cmp = backtest_factor_accuracy(df_cmp, ind_cmp, lookahead=4)
                    dw_cmp = compute_dynamic_weights(acc_cmp)
                    conf_cmp = generate_confluence_signal(df_cmp, ind_cmp, dw_cmp)
                except Exception as calc_exc:
                    multi_tf_status = {
                        "status": "UNAVAILABLE",
                        "reason": f"Indicator calculation failed: {calc_exc}",
                        "timeframe_checked": compare_tf,
                    }
                    raise

                s_primary = confluence["signal"]
                s_compare = conf_cmp["signal"]

                if s_primary != "WAIT" and s_compare != "WAIT" and s_primary == s_compare:
                    mt_status = "CONFIRMED"
                    # Boost primary confidence by 10%, capped at 95%
                    confluence = dict(confluence, confidence=min(95, confluence["confidence"] + 10))
                elif s_primary == "WAIT" or s_compare == "WAIT":
                    mt_status = "PARTIAL"
                else:
                    mt_status = "CONFLICTING"
                    # Both timeframes disagree — override to WAIT
                    confluence = dict(confluence, signal="WAIT", confidence=0)

                multi_tf_result = {
                    "status": mt_status,
                    "primary_tf": timeframe,
                    "primary_signal": s_primary,
                    "compare_tf": compare_tf,
                    "compare_signal": s_compare,
                }

                # Step 2: map the existing 3-way internal status onto the
                # explicit CONFIRMED / DISAGREED / UNAVAILABLE vocabulary,
                # purely for reporting — multi_tf_result above is untouched.
                if mt_status == "CONFIRMED":
                    multi_tf_status = {
                        "status": "CONFIRMED",
                        "reason": "Higher timeframe signal agrees with primary timeframe",
                        "timeframe_checked": compare_tf,
                    }
                elif mt_status == "CONFLICTING":
                    multi_tf_status = {
                        "status": "DISAGREED",
                        "reason": "Higher timeframe signal conflict",
                        "timeframe_checked": compare_tf,
                    }
                else:  # PARTIAL — one side had insufficient confirmation
                    multi_tf_status = {
                        "status": "DISAGREED",
                        "reason": "Higher timeframe did not provide confirmation (WAIT)",
                        "timeframe_checked": compare_tf,
                    }
            else:
                multi_tf_status = {
                    "status": "UNAVAILABLE",
                    "reason": "Comparison timeframe data unavailable",
                    "timeframe_checked": compare_tf,
                }
        except Exception as exc:
            # Multi-TF is best-effort; primary analysis always returned.
            # Only update the reason if the inner calc-failure handler above
            # did not already set a more specific one.
            if multi_tf_status.get("reason") in (
                "Multi-timeframe check did not run",
            ):
                multi_tf_status = {
                    "status": "UNAVAILABLE",
                    "reason": f"Exception during MTF analysis: {exc}",
                    "timeframe_checked": compare_tf,
                }
        t = _log_step(f"multi_tf get_candles_df({compare_tf})+analysis", t0, t)



        factors_out = []
        for key, label in _FACTOR_LABELS.items():
            vote = confluence["factors"].get(key, 0)
            acc = accuracies.get(key)
            factors_out.append({
                "key": key,
                "label": label,
                "vote": _VOTE_LABELS.get(vote, "NEUTRAL"),
                "weight": round(dynamic_weights.get(key, 0), 1),
                "accuracy": acc,
            })

        result = {
            "asset": asset,
            "timeframe": timeframe,
            "is_otc": "_otc" in asset.lower(),
            "price": indicators.get("price"),
            "candle_count": len(df),
            "trend": indicators.get("direction"),
            "confluence": {
                "signal": confluence["signal"],
                "confidence": confluence["confidence"],
            },
            "legacy": {
                "signal": legacy_signal["signal"],
                "confidence": legacy_signal["confidence"],
            },
            "agree": confluence["signal"] == legacy_signal["signal"],
            "factors": factors_out,
            "indicators": {
                "rsi": indicators.get("rsi"),
                "macd_histogram": indicators.get("macd_histogram"),
                "cci": indicators.get("cci"),
                "adx": indicators.get("adx"),
                "candlestick_pattern": indicators.get("candlestick_pattern"),
                # Phase 5: new, additive — richer structure with direction/
                # strength/reliability. None for older cached data paths.
                "candlestick_pattern_detail": indicators.get("candlestick_pattern_detail"),
                "support": indicators.get("support"),
                "resistance": indicators.get("resistance"),
                # Phase 6: new, additive — richer zone-based structure. None
                # for older cached data paths.
                "support_resistance_detail": indicators.get("support_resistance_detail"),
                "atr_pct": indicators.get("atr_pct"),
                "volatility_level": indicators.get("level"),
            },
            # Task 3: payout % (None = unavailable or market closed)
            "payout_pct": round(payout_pct, 1) if payout_pct and payout_pct > 0 else None,
            # Task 7: price history for frontend Chart.js chart (last 50 closes)
            "price_history": [round(float(x), 5) for x in df["close"].tail(50).tolist()],
            # TASK 4: multi-timeframe confirmation result (None if unavailable)
            "multi_tf": multi_tf_result,
            # Step 2: always-populated explicit status — CONFIRMED / DISAGREED /
            # UNAVAILABLE — purely additive, does not affect signal/confidence.
            "multi_tf_status": multi_tf_status,
        }

        # Phase 7: Filter Score — computed ONCE here (single source of truth),
        # reusing only fields already produced above. Does not affect
        # confidence, signal, or any existing field. scanner.py reads these
        # same fields straight from this result rather than recomputing them.
        # Phase 7.4: settings-driven overrides (adx_trending, atr_extreme_pct,
        # min_payout) — at settings.json's shipped defaults these are
        # identical to cfg.ADX_TRENDING / analyzer.VOLATILITY_EXTREME_ATR_PCT
        # / cfg.MIN_PAYOUT, so behavior is byte-identical until a user
        # actually edits Settings.
        _filter_overrides = settings_store.get_filter_score_config_overrides()
        filter_result = calculate_filter_score(
            indicators=indicators,
            signal_data={"multi_tf_status": multi_tf_status, "payout_pct": payout_pct},
            config=_filter_overrides,
        )
        result["filter_score"] = filter_result["filter_score"]
        result["mandatory_pass"] = filter_result["mandatory_pass"]
        result["passed_filters"] = filter_result["passed_filters"]
        result["failed_filters"] = filter_result["failed_filters"]
        result["filter_breakdown"] = filter_result["filter_breakdown"]

        print(f"[PERF] ── pipeline done  total={_ts() - t0:.2f}s ──\n")
        return result
    except (ConnectionError, OSError, RuntimeError) as e:
        # Connection died mid-request — invalidate so the next call reconnects.
        _invalidate_shared_fetcher()
        raise


# ─── Phase 7.4: Settings Store ──────────────────────────────────────────────
# Fixed path, gitignored (see repo-root .gitignore's Security section).
# One store instance per process — every read/write goes straight to disk
# (see settings_store.py docstring), so this is safe under Gunicorn workers.
# NOTE: moved above the Smart Scanner block (Phase 8.1) so `settings_store`
# exists in time to be passed into ScannerEngine's now-optional
# `settings_store=` constructor arg below. Nothing about SettingsStore
# itself changed — only its position in this file.
_SETTINGS_PATH = str(_WEBAPP_DIR / "settings.json")
settings_store = SettingsStore(_SETTINGS_PATH)


# ─── Phase 7: Smart Scanner ────────────────────────────────────────────────
# Pure orchestration around _run_pipeline() above — reused exactly, never
# duplicated. Runs as one coroutine on the SAME shared _BG_LOOP, so it never
# introduces a concurrent Quotex fetch alongside a manual /api/signal call.
# Phase 8.1: `settings_store=settings_store` is the only change here — it's
# read defensively inside ScannerEngine (see scanner.py's
# _read_scanner_settings()), so this remains safe even if settings.json is
# ever missing/corrupt.
_scanner = ScannerEngine(
    run_pipeline=_run_pipeline,
    assets=_asset_choices()["otc"],
    invalidate_fetcher=_invalidate_shared_fetcher,
    adx_trending=cfg.ADX_TRENDING,
    config=ScannerConfig(min_payout=cfg.MIN_PAYOUT),
    settings_store=settings_store,
)


# ─── Phase 7.4: Backtest Engine ─────────────────────────────────────────────
# fetch_candles wraps the SAME shared fetcher machinery _run_pipeline() uses
# — this does not open a second Quotex connection path.
async def _backtest_fetch_candles(asset: str, timeframe: str, count: int):
    fetcher = await _get_shared_fetcher()
    return await fetcher.get_candles_df(asset=asset, timeframe=timeframe, count=count)


_backtest_engine = BacktestEngine(fetch_candles=_backtest_fetch_candles)


# ─── Phase 7.4: Quotex session.json path (session mgmt routes, below) ──────
# Unchanged from the pre-existing session-loading convention in
# quotex/api_quotex — QUOTEX_SSID env var still takes priority when set;
# this path is only where the UI-driven "Update SSID" action writes to.
_SESSION_JSON_PATH = _QUOTEX_DIR / "session.json"


def _apply_settings_weight_overrides(dynamic_weights: Dict[str, float],
                                     settings: Dict[str, Any]) -> Dict[str, float]:
    """
    Phase 7.4 — layers Settings' indicator enable/disable + weight-scale on
    top of the ALREADY-COMPUTED per-run dynamic_weights (live backtest
    accuracy, or precomputed weights — untouched, computed exactly as
    before this function is called).

    Deliberate design choice (see PROJECT_MEMORY.md / IMPLEMENTATION_REPORT):
    NEXT_PHASE.md's literal instruction was to pass
    settings_store.get_effective_dynamic_weights() directly as
    generate_confluence_signal()'s dynamic_weights argument. That would
    silently DISCARD the existing live/precomputed dynamic-weight
    computation on every request (replacing it with settings' flat
    per-indicator weights), which conflicts with two harder constraints
    from this project's rules: (1) never modify the Dynamic Weight
    algorithm, and (2) default settings.json / no settings.json must be
    byte-identical to current behavior. A blind replace is NOT
    byte-identical, because the live-computed dynamic_weights are almost
    never uniform 10.0-each in practice.

    Instead: each factor's computed weight is scaled by
    (settings_weight / 10.0) — a pure multiplier, 1.0 for every factor at
    settings' shipped defaults (all weights default to 10.0), which makes
    this a no-op when settings are untouched. Disabled indicators are
    forced to weight 0 regardless of scale, matching the settings_store
    docstring's documented "disabled = weight 0" contract exactly. If
    settings.normalize_weights is on, the result is rescaled to sum to
    100 (same invariant DEFAULT_CONFLUENCE_WEIGHTS already guarantees).
    Never touches analyzer.py, DEFAULT_CONFLUENCE_WEIGHTS, or the vote
    logic itself.
    """
    indicators_cfg = settings.get("indicators", {})
    out: Dict[str, float] = {}
    for key, base_weight in dynamic_weights.items():
        ind_cfg = indicators_cfg.get(key)
        if ind_cfg is None:
            out[key] = base_weight
            continue
        if not ind_cfg.get("enabled", True):
            out[key] = 0.0
            continue
        scale = float(ind_cfg.get("weight", 10.0)) / 10.0
        out[key] = base_weight * scale

    if settings.get("backtest", {}).get("normalize_weights", True):
        total = sum(out.values())
        if total > 0:
            out = {k: round(v * 100.0 / total, 4) for k, v in out.items()}
            # Same rounding-drift fix as settings_store.get_effective_dynamic_weights():
            # assign the residual to the largest weight so the sum is exactly 100.
            drift = round(100.0 - sum(out.values()), 4)
            if drift != 0.0:
                top_key = max(out, key=out.get)
                out[top_key] = round(out[top_key] + drift, 4)
    return out


# ─── Routes ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    choices = _asset_choices()
    return render_template(
        "index.html",
        otc_assets=choices["otc"],
        live_assets=choices["live"],
        timeframes=TIMEFRAMES,
        default_asset=cfg.DEFAULT_ASSET,
        default_timeframe=cfg.PRIMARY_TIMEFRAME,
        is_demo=cfg.IS_DEMO,
    )


@app.route("/api/signal", methods=["POST"])
def api_signal():
    """
    Run the analysis pipeline for the requested asset/timeframe and return
    the confluence + legacy signal as JSON. Analysis only — no order is
    placed by this endpoint or anything it calls.
    """
    data = request.get_json(silent=True) or {}
    asset = str(data.get("asset", cfg.DEFAULT_ASSET))
    timeframe = str(data.get("timeframe", cfg.PRIMARY_TIMEFRAME))

    if timeframe not in TIMEFRAMES:
        return jsonify({"error": f"Invalid timeframe '{timeframe}'. Must be one of {TIMEFRAMES}."}), 400
    if asset not in ASSETS:
        return jsonify({"error": f"Unknown asset '{asset}'."}), 400

    try:
        # Fix 3: submit to the shared background loop so the persistent
        # WebSocket connection is reused across requests.
        # Phase 7: mark this as an in-flight manual request so the scanner
        # (if running) yields between its own asset steps until this finishes.
        _scanner.manual_request_started()
        try:
            result = _run_bg(_run_pipeline(asset, timeframe))
        finally:
            _scanner.manual_request_finished()
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 500
    except ValueError as e:
        return jsonify({"error": str(e)}), 500
    except ConnectionError as e:
        # TASK 5: clearer session-expiry guidance for the user
        err_str = str(e).replace("\n", " ")
        if "session" in err_str.lower() or "failed to connect" in err_str.lower() or "ssid" in err_str.lower():
            user_msg = (
                "Session expired or connection failed. "
                "To renew: open browser DevTools (F12) → Network tab → "
                "WS filter → connect to quotex.io → copy 'ssid' from WebSocket URL → "
                "update quotex/session.json. "
                "Or re-run: bash quotex/run_login.sh"
            )
        else:
            user_msg = f"Connection error: {err_str}"
        return jsonify({"error": user_msg}), 502
    except Exception as e:  # noqa: BLE001 — surface unexpected errors to the UI
        return jsonify({"error": f"Analysis failed: {e}"}), 500

    if "error" in result:
        return jsonify(result), 502
    return jsonify(result)


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


# ── Task 7: Live prices — polling-based (no persistent WebSocket to browser) ──

_LIVE_PRICE_CACHE: dict = {"prices": {}, "ts": 0.0}
_TOP_OTC = ["EURUSD_otc", "GBPUSD_otc", "USDJPY_otc", "AUDUSD_otc", "USDCAD_otc", "EURJPY_otc"]

# Phase 3: bounded concurrency for per-asset candle fetches below.
# AsyncQuotexClient._request_candles() already keys in-flight requests by
# "{asset}_{timeframe}" (see api_quotex/client.py), so concurrent fetches for
# different assets on the same connection are safe by the client's own design.
# The semaphore just caps how many fetches run at once, to stay a well-behaved
# client of the upstream server rather than firing all assets simultaneously.
_LIVE_PRICES_MAX_CONCURRENCY = 5


async def _fetch_current_prices(assets: list) -> dict:
    """
    Connect once, pull the latest 1-minute candle for each asset concurrently
    (bounded by a semaphore), disconnect.
    Returns { asset_sym: { price: float, change_pct: float } }.
    NEVER places orders — read-only candle data only.
    """
    fetcher = QuotexDataFetcher()
    result: dict = {}
    semaphore = asyncio.Semaphore(_LIVE_PRICES_MAX_CONCURRENCY)

    async def _fetch_one(a: str) -> None:
        async with semaphore:
            try:
                df = await fetcher.get_candles_df(asset=a, timeframe="1m", count=3)
                if not df.empty and len(df) >= 2:
                    close = float(df["close"].iloc[-1])
                    prev  = float(df["close"].iloc[-2])
                    chg   = round((close - prev) / prev * 100, 3) if prev else 0.0
                    result[a] = {"price": close, "change_pct": chg}
            except Exception:
                pass  # Skip individual asset errors — each asset writes to its own key

    try:
        await fetcher.connect()
        await asyncio.gather(*(_fetch_one(a) for a in assets))
    finally:
        await fetcher.disconnect()
    return result


@app.route("/api/live-prices")
def api_live_prices():
    """
    Return current prices for the top OTC pairs.
    Server-side 30-second cache avoids hammering Quotex with new connections.
    Frontend polls this endpoint every 15 s for the Markets tab.
    Analysis-only — no order placement.
    """
    import time
    now   = time.time()
    cache = _LIVE_PRICE_CACHE

    # Return cached data if it is fresh enough
    if now - cache["ts"] < 30 and cache["prices"]:
        return jsonify({"prices": cache["prices"], "cached": True})

    try:
        prices = _run_bg(_fetch_current_prices(_TOP_OTC))
        cache["prices"] = prices
        cache["ts"]     = now
        return jsonify({"prices": prices, "cached": False})
    except Exception as exc:
        # Serve stale cache rather than an empty error on transient failures
        if cache["prices"]:
            return jsonify({"prices": cache["prices"], "cached": True, "stale": True})
        return jsonify({"error": f"Price fetch failed: {exc}"}), 500


@app.route("/api/scanner/status")
def api_scanner_status():
    """Scanner lifecycle state, metrics, history, and recent events."""
    return jsonify(_scanner.get_status())


@app.route("/api/scanner/results")
def api_scanner_results():
    """
    Ranked top signals from the scanner's cache. Only assets that passed
    ALL hard gates and have a non-WAIT signal are included — no partial
    credit scoring, per the approved architecture.
    """
    min_confidence = request.args.get("min_confidence", type=float)
    timeframe = request.args.get("timeframe", type=str)
    limit = request.args.get("limit", type=int)
    return jsonify(_scanner.get_results(min_confidence=min_confidence, timeframe=timeframe, limit=limit))


@app.route("/api/scanner/start", methods=["POST"])
def api_scanner_start():
    data = request.get_json(silent=True) or {}
    timeframes = data.get("timeframes")
    if timeframes:
        invalid = [tf for tf in timeframes if tf not in TIMEFRAMES]
        if invalid:
            return jsonify({"error": f"Invalid timeframe(s): {invalid}"}), 400
    result = _scanner.start(
        loop=_get_bg_loop(),
        timeframes=timeframes,
        top_n=data.get("top_n"),
        min_confidence=data.get("min_confidence"),
        refresh_seconds=data.get("refresh_seconds"),
    )
    return jsonify(result), (200 if result["ok"] else 409)


@app.route("/api/scanner/stop", methods=["POST"])
def api_scanner_stop():
    result = _scanner.stop()
    return jsonify(result), (200 if result["ok"] else 409)


@app.route("/api/scanner/pause", methods=["POST"])
def api_scanner_pause():
    result = _scanner.pause()
    return jsonify(result), (200 if result["ok"] else 409)


@app.route("/api/scanner/resume", methods=["POST"])
def api_scanner_resume():
    result = _scanner.resume()
    return jsonify(result), (200 if result["ok"] else 409)


@app.route("/api/scanner/settings/reset", methods=["POST"])
def api_scanner_settings_reset():
    """
    Phase 8.3 — resets ONLY the `scanner` settings section to its
    defaults (scanner_enabled/enabled_assets/enabled_timeframes/
    scan_interval/minimum_filter_score/top_signals), leaving every other
    settings section (indicators, backtest, filters, etc.) untouched.
    Thin wrapper around settings_store.reset_section("scanner") — same
    pattern as every other route in this section. Does NOT touch the
    running scanner in any way (per spec: settings changes only apply on
    the next start()), so no ScannerEngine call happens here at all.
    """
    try:
        return jsonify(settings_store.reset_section("scanner"))
    except KeyError as e:
        return jsonify({"error": str(e)}), 400


# ═══════════════════════════════════════════════════════════════════════════
# Phase 7.4 — Settings Routes
# Thin HTTP wrappers around settings_store.py's already-tested methods.
# Mirrors the exact pattern of the scanner routes above. No new business
# logic lives here — every route is a direct pass-through.
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "GET":
        return jsonify(settings_store.get())
    patch = request.get_json(silent=True) or {}
    try:
        return jsonify(settings_store.update(patch))
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"Settings update failed: {e}"}), 400


@app.route("/api/settings/reset", methods=["POST"])
def api_settings_reset():
    return jsonify(settings_store.reset())


@app.route("/api/settings/backups", methods=["GET"])
def api_settings_backups():
    return jsonify({"backups": settings_store.list_backups()})


@app.route("/api/settings/backups/restore", methods=["POST"])
def api_settings_backups_restore():
    data = request.get_json(silent=True) or {}
    index = data.get("index")
    if index is None:
        return jsonify({"error": "Missing required field 'index'"}), 400
    try:
        return jsonify(settings_store.restore_backup(int(index)))
    except IndexError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"Restore failed: {e}"}), 400


@app.route("/api/settings/export", methods=["GET"])
def api_settings_export():
    return jsonify(settings_store.export_settings())


@app.route("/api/settings/import", methods=["POST"])
def api_settings_import():
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(settings_store.import_settings(data))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"Import failed: {e}"}), 400


@app.route("/api/settings/backup", methods=["POST"])
def api_settings_backup():
    """Manual 'create a backup now' action (separate from the automatic
    backup apply_suggested_weights() already creates on every apply)."""
    data = request.get_json(silent=True) or {}
    reason = str(data.get("reason", "manual"))
    return jsonify(settings_store.create_backup(reason=reason))


# ═══════════════════════════════════════════════════════════════════════════
# Phase 7.4 — Indicator Registry Route
# Read-only metadata view: the 13-entry registry (10 confluence-connected +
# 3 not-yet-connected, per indicator_registry.py), overlaid with the user's
# Settings enable/disable + weight overrides. Does not compute or duplicate
# any indicator math.
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/indicators", methods=["GET"])
def api_indicators():
    entries = ind_registry.get_registry()
    settings = settings_store.get()
    entries = ind_registry.apply_settings_overrides(entries, settings.get("indicators", {}))

    # If a backtest has completed, layer real accuracy/dynamic_weight data on
    # top — pure read of _backtest_engine's already-computed public
    # attributes (.results / .summary), no re-computation, no
    # backtest_engine.py changes. Averages each factor's accuracy across all
    # successfully-backtested assets (same "simple averaging" approach
    # _build_summary() already uses for suggested_weights).
    if _backtest_engine.summary and _backtest_engine.summary.get("assets_backtested", 0) > 0:
        valid = {a: r for a, r in _backtest_engine.results.items() if "error" not in r}
        if valid:
            keys = next(iter(valid.values()))["accuracies"].keys()
            avg_accuracies = {
                k: {
                    "accuracy": round(sum(r["accuracies"][k]["accuracy"] for r in valid.values()) / len(valid), 4),
                    "sample_size": min(r["accuracies"][k]["sample_size"] for r in valid.values()),
                }
                for k in keys
            }
            timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_backtest_engine.finished_at or time.time()))
            entries = ind_registry.populate_from_backtest(
                entries, avg_accuracies, _backtest_engine.summary["suggested_weights"], timestamp
            )

    return jsonify({
        "indicators": entries,
        "filter_gate_criteria": ind_registry.FILTER_GATE_CRITERIA,
        "new_indicator_ids": ind_registry.NEW_INDICATOR_IDS,
    })


# ═══════════════════════════════════════════════════════════════════════════
# Phase 7.4 — Backtest Routes
# Thin HTTP wrappers around backtest_engine.py's already-tested lifecycle.
# _backtest_engine is a single module-level instance (one backtest at a
# time — start() itself already rejects a second concurrent run).
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/backtest/run", methods=["POST"])
def api_backtest_run():
    data = request.get_json(silent=True) or {}

    # 400 — malformed client input (bad types), caught before touching the engine.
    try:
        assets = data.get("assets") or _asset_choices()["otc"]
        if not isinstance(assets, list) or not all(isinstance(a, str) for a in assets):
            return jsonify({"ok": False, "error": "'assets' must be a list of asset symbol strings."}), 400
        timeframe = str(data.get("timeframe", cfg.PRIMARY_TIMEFRAME))
        candle_count = int(data.get("candle_count", settings_store.get()["backtest"]["min_candles"]))
        lookahead = int(data.get("lookahead", 4))
    except (TypeError, ValueError) as e:
        return jsonify({"ok": False, "error": f"Invalid request body: {e}"}), 400

    # 400 — semantically invalid but well-typed input.
    if timeframe not in TIMEFRAMES:
        return jsonify({"ok": False, "error": f"Invalid timeframe '{timeframe}'. Must be one of {TIMEFRAMES}."}), 400
    invalid_assets = [a for a in assets if a not in ASSETS]
    if invalid_assets:
        return jsonify({"ok": False, "error": f"Unknown asset(s): {invalid_assets}"}), 400
    if candle_count not in BACKTEST_CANDLE_OPTIONS:
        return jsonify({"ok": False, "error": f"candle_count must be one of {BACKTEST_CANDLE_OPTIONS}, got {candle_count}"}), 400
    if not assets:
        return jsonify({"ok": False, "error": "No assets specified"}), 400

    # 409 — genuine conflict: a backtest is already in progress.
    # Bug fix (found in Phase 7.4 API regression testing): BacktestEngine.start()
    # returns the same {"ok": False, "message": ...} shape whether the reason
    # is "already running" (a real 409 conflict) or "invalid candle_count" /
    # "no assets" (a 400 client input error, both pre-checked above already)
    # — see backtest_engine.py's start() docstring/body, unmodified. Checking
    # the conflict condition ourselves first means every case now gets the
    # right HTTP status without needing to touch backtest_engine.py at all.
    if _backtest_engine.state != _BT_STOPPED:
        return jsonify({"ok": False, "error": f"Backtest already {_backtest_engine.state} — ignoring duplicate start request"}), 409

    # 500 — something the checks above didn't anticipate (e.g. the shared
    # background event loop isn't available). Genuinely unexpected only.
    try:
        result = _backtest_engine.start(
            loop=_get_bg_loop(), assets=assets, timeframe=timeframe,
            candle_count=candle_count, lookahead=lookahead,
        )
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"Internal error starting backtest: {e}"}), 500

    if not result["ok"]:
        # Defensive fallback only — every documented start() failure reason
        # is already intercepted by the checks above, so this path should be
        # unreachable in practice; kept as a safety net rather than a 500.
        return jsonify(result), 400
    return jsonify(result), 200


@app.route("/api/backtest/pause", methods=["POST"])
def api_backtest_pause():
    result = _backtest_engine.pause()
    return jsonify(result), (200 if result["ok"] else 409)


@app.route("/api/backtest/resume", methods=["POST"])
def api_backtest_resume():
    result = _backtest_engine.resume()
    return jsonify(result), (200 if result["ok"] else 409)


@app.route("/api/backtest/stop", methods=["POST"])
def api_backtest_stop():
    result = _backtest_engine.stop()
    return jsonify(result), (200 if result["ok"] else 409)


@app.route("/api/backtest/status", methods=["GET"])
def api_backtest_status():
    return jsonify(_backtest_engine.status())


@app.route("/api/backtest/results", methods=["GET"])
def api_backtest_results():
    return jsonify(_backtest_engine.get_results())


@app.route("/api/backtest/apply-weights", methods=["POST"])
def api_backtest_apply_weights():
    """
    Explicit, user-triggered only (this route IS that explicit action —
    never called by anything automatic/scheduled in this codebase).
    Gate-checks via evaluate_apply_conditions() first; only writes to
    settings.json if can_apply=True. apply_suggested_weights() itself
    already creates a backup — never duplicated here.
    """
    backtest_cfg = settings_store.get()["backtest"]
    check = evaluate_apply_conditions(
        _backtest_engine,
        min_candles=backtest_cfg["min_candles"],
        min_indicator_sample_size=backtest_cfg["min_indicator_sample_size"],
    )
    if not check["can_apply"]:
        return jsonify({"ok": False, "reasons": check["reasons"]}), 409

    updated = settings_store.apply_suggested_weights(
        check["summary"]["suggested_weights"], reason="backtest"
    )
    return jsonify({"ok": True, "settings": updated})


# ═══════════════════════════════════════════════════════════════════════════
# Phase 7.4 — Quotex Session Management Routes
# Reuses the EXISTING connect()/load_session_ssid() path exactly as-is —
# nothing in quotex/api_quotex/ (WebSocket, login, config) is touched.
# "Update SSID" writes only to session.json, never to an env var.
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/session/status", methods=["GET"])
def api_session_status():
    env_ssid_set = bool(os.environ.get("QUOTEX_SSID", "").strip())
    file_ssid_present = False
    file_ssid_length = None
    if _SESSION_JSON_PATH.exists():
        try:
            data = json.loads(_SESSION_JSON_PATH.read_text(encoding="utf-8"))
            ssid = str(data.get("ssid", "")).strip()
            file_ssid_present = bool(ssid)
            file_ssid_length = len(ssid) if ssid else None
        except Exception:
            pass
    quotex_settings = settings_store.get()["quotex"]
    return jsonify({
        "env_ssid_set": env_ssid_set,
        "session_file_exists": _SESSION_JSON_PATH.exists(),
        "session_file_ssid_present": file_ssid_present,
        "session_file_ssid_length": file_ssid_length,  # length only — value never exposed
        "active_source": "env" if env_ssid_set else ("session.json" if file_ssid_present else "none"),
        "last_validated_at": quotex_settings.get("last_validated_at"),
        "last_validation_status": quotex_settings.get("last_validation_status"),
        "fetcher_connected": _fetcher_is_alive(),
    })


@app.route("/api/session/update", methods=["POST"])
def api_session_update():
    """Writes the pasted SSID to quotex/session.json (never an env var).
    Preserves any other keys already in the file. Does not connect —
    call /api/session/validate afterwards to test it."""
    data = request.get_json(silent=True) or {}
    ssid = str(data.get("ssid", "")).strip()
    if not ssid or len(ssid) < 10:
        return jsonify({"error": "SSID looks too short/invalid — paste the full value."}), 400

    existing: Dict[str, Any] = {}
    if _SESSION_JSON_PATH.exists():
        try:
            existing = json.loads(_SESSION_JSON_PATH.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except Exception:
            existing = {}

    existing["ssid"] = ssid
    _SESSION_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = str(_SESSION_JSON_PATH) + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(existing, f, indent=2)
    os.replace(tmp_path, _SESSION_JSON_PATH)  # atomic — same safety pattern as settings_store

    # A newly-written SSID means the persistent fetcher (if any) is stale.
    _invalidate_shared_fetcher()
    return jsonify({"ok": True, "message": "SSID saved to session.json.", "ssid_length": len(ssid)})


@app.route("/api/session/validate", methods=["POST"])
def api_session_validate():
    """
    Forces a fresh connection attempt via the EXISTING connect() path
    (same code _run_pipeline() uses) and reports success/failure.

    Honesty note: AsyncQuotexClient's connect() performs authentication and
    WebSocket-gateway attachment as a single handshake — it does not expose
    separate "server" vs "gateway" stage events. So "Connected to Server" /
    "Connected to Gateway" / "Session Valid" are reported together as one
    outcome here, not as independently-verified stages; this endpoint does
    not fabricate finer-grained status than the underlying client provides.
    """
    _invalidate_shared_fetcher()  # force a real reconnect, not a cached "alive" check
    try:
        _run_bg(_get_shared_fetcher(), timeout=30.0)
        settings_store.update({"quotex": {
            "last_validated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "last_validation_status": "valid",
        }})
        return jsonify({
            "ok": True,
            "connected_to_server": True,
            "connected_to_gateway": True,
            "session_valid": True,
            "message": "Session validated — connected successfully.",
        })
    except Exception as e:  # noqa: BLE001
        settings_store.update({"quotex": {
            "last_validated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "last_validation_status": "invalid",
        }})
        return jsonify({
            "ok": False,
            "connected_to_server": False,
            "connected_to_gateway": False,
            "session_valid": False,
            "error": str(e),
        }), 502


# ═══════════════════════════════════════════════════════════════════════════
# Phase 7.4 — Generic API error handler
# Safety net only: every route added this phase already handles its own
# expected failure modes with a specific status code (400/404/409, and the
# backtest/run route's own explicit 500 wrapper) — this exists purely to
# turn any genuinely unanticipated exception into a structured JSON 500
# instead of Flask's default HTML error page, for every /api/* route.
# Does not change behavior for any request that was already handled.
# ═══════════════════════════════════════════════════════════════════════════

@app.errorhandler(500)
def _handle_internal_error(e):
    return jsonify({"ok": False, "error": "Internal server error", "detail": str(e)}), 500


@app.errorhandler(Exception)
def _handle_unexpected_exception(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e  # let Flask's normal handling of 404/405/etc. through unchanged
    return jsonify({"ok": False, "error": "Internal server error", "detail": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
