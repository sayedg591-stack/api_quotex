"""
Phase 7.3 — Indicator Registry
================================
Single source of truth for indicator METADATA (name, group, enable state,
weight, and — after a backtest run — accuracy/sample_size/dynamic_weight).

This module does NOT define or duplicate any indicator's math, and does NOT
define default weights itself — those remain owned exclusively by
analyzer.DEFAULT_CONFLUENCE_WEIGHTS (imported, never copied). This registry
only layers metadata on top for Settings/Backtest/future-Scanner/future-AI
consumers to share one consistent view.

Confluence-vote indicators (10 existing + 3 new, added in Phase 7.3 Part 3)
share one shape, matching backtest.backtest_factor_accuracy()'s own output
keys (accuracy/sample_size) so registry entries can be populated directly
from real backtest results without any translation layer.

EMA Trend / ADX / ATR are intentionally NOT full registry entries here: they
are Filter Score GATE criteria (analyzer.calculate_filter_score()), not
confluence-vote factors — they don't have a per-factor "weight" in the same
sense (no accuracy/sample_size is computed for them the way
backtest_factor_accuracy() does for confluence factors). Documenting them
under FILTER_GATE_CRITERIA instead of forcing them into the same shape is a
deliberate accuracy choice, not an oversight.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

try:
    from analyzer import DEFAULT_CONFLUENCE_WEIGHTS
except ImportError:
    from market_analyzer.analyzer import DEFAULT_CONFLUENCE_WEIGHTS

# ── Static metadata (id -> display name, group) ────────────────────────────
# group: "core" | "helper" — per Phase 7.3 Part 4. Helper indicators never
# generate BUY/SELL unilaterally; this is already structurally guaranteed by
# generate_confluence_signal()'s existing >=3-agreeing-factors requirement,
# not something this registry needs to separately enforce — the "helper"
# tag here is metadata for Settings/UI display only.
_INDICATOR_META: Dict[str, Dict[str, str]] = {
    # Core
    "rsi_div":          {"name": "RSI Divergence",       "group": "core"},
    "bb":                {"name": "Bollinger Bands",      "group": "core"},
    "obv":               {"name": "OBV Divergence",       "group": "core"},
    "sr":                {"name": "Support/Resistance",   "group": "core"},
    "candle":            {"name": "Candlestick Pattern",  "group": "core"},
    "wick_rejection":    {"name": "Wick Rejection",       "group": "core"},      # Phase 7.3 Part 3, new
    "liquidity_sweep":   {"name": "Liquidity Sweep",      "group": "core"},      # Phase 7.3 Part 3, new
    "false_breakout":    {"name": "False Breakout",       "group": "core"},      # Phase 7.3 Part 3, new
    # Helper — never generate BUY/SELL alone; only improve confidence
    "cci":               {"name": "CCI Extreme",          "group": "helper"},
    "stoch":             {"name": "Stochastic Cross",     "group": "helper"},
    "round_number":      {"name": "Round Number",         "group": "helper"},
    "mean_reversion":    {"name": "Mean Reversion",       "group": "helper"},
    "exhaustion":        {"name": "Candle Exhaustion",    "group": "helper"},
}

# Filter Score gate criteria — a DIFFERENT mechanism (analyzer.
# calculate_filter_score()), listed separately rather than force-fit into
# the confluence-factor shape above. See module docstring.
FILTER_GATE_CRITERIA = {
    "ema_trend": {"name": "EMA Trend", "weight": 20.0},
    "adx":       {"name": "ADX Strength", "weight": 15.0},
    "atr":       {"name": "ATR Volatility", "weight": 15.0},
}

# New indicators from Phase 7.3 Part 3 — registered here, integrated into
# Backtest/Settings/Registry ONLY per the explicit instruction "Only then
# safely connect them to Confluence." They are NOT yet in
# analyzer.DEFAULT_CONFLUENCE_WEIGHTS and NOT yet voted on by
# generate_confluence_signal() — that is a deliberately separate, future step.
NEW_INDICATOR_IDS = ["wick_rejection", "liquidity_sweep", "false_breakout"]


def get_registry() -> List[Dict[str, Any]]:
    """
    Returns the full registry: one entry per confluence-vote-style
    indicator (existing 10 + 3 new), each with the exact fields requested:
    id, name, enabled, helper, weight, dynamic_weight, accuracy,
    sample_size, last_backtest, strength_score, reliability_score.

    `weight` for the 10 EXISTING indicators comes directly from
    analyzer.DEFAULT_CONFLUENCE_WEIGHTS (imported, not duplicated). The 3
    NEW indicators default to 0.0 (not yet part of the confluence weight
    system) until a future step explicitly adds them.

    dynamic_weight/accuracy/sample_size/last_backtest/strength_score/
    reliability_score are all None until populated by a real backtest run
    (see populate_from_backtest() below) — never fabricated here.
    """
    entries = []
    for indicator_id, meta in _INDICATOR_META.items():
        entries.append({
            "id": indicator_id,
            "name": meta["name"],
            "group": meta["group"],
            "helper": meta["group"] == "helper",
            "enabled": True,
            "weight": DEFAULT_CONFLUENCE_WEIGHTS.get(indicator_id, 0.0),
            "in_confluence": indicator_id in DEFAULT_CONFLUENCE_WEIGHTS,
            "dynamic_weight": None,
            "accuracy": None,
            "sample_size": None,
            "last_backtest": None,
            "strength_score": None,
            "reliability_score": None,
        })
    return entries


def populate_from_backtest(entries: List[Dict[str, Any]], accuracies: Dict[str, Any],
                           suggested_weights: Dict[str, float], timestamp: Optional[str]) -> List[Dict[str, Any]]:
    """
    Layers real backtest.backtest_factor_accuracy() / compute_dynamic_weights()
    output onto registry entries. Pure data merge — does not call, wrap, or
    reimplement either function; the caller (backtest_engine.py or a route)
    is expected to have already produced `accuracies`/`suggested_weights`
    via the existing, unmodified backtest functions.
    """
    acc_by_id = accuracies or {}
    weights_by_id = suggested_weights or {}
    updated = []
    for entry in entries:
        e = dict(entry)
        acc_entry = acc_by_id.get(e["id"])
        if acc_entry:
            e["accuracy"] = acc_entry.get("accuracy")
            e["sample_size"] = acc_entry.get("sample_size")
            e["last_backtest"] = timestamp
        if e["id"] in weights_by_id:
            e["dynamic_weight"] = weights_by_id[e["id"]]
        updated.append(e)
    return updated


def apply_settings_overrides(entries: List[Dict[str, Any]],
                             indicator_settings: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Overlays settings_store's per-indicator enabled/weight overrides onto
    the registry's default view — pure merge, no settings_store import here
    (keeps this module decoupled; the caller passes in
    settings_store.get()["indicators"]).
    """
    updated = []
    for entry in entries:
        e = dict(entry)
        override = indicator_settings.get(e["id"])
        if override:
            e["enabled"] = override.get("enabled", e["enabled"])
            if "weight" in override:
                e["weight"] = override["weight"]
        updated.append(e)
    return updated
