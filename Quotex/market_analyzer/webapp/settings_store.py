"""
Phase 7.2 — Settings Store
===========================
A simple JSON-file-backed persistent settings store. Does NOT touch
indicator calculations, the confluence engine, filter score logic, or the
scanner — it only holds configuration values that _run_pipeline() (and the
new backtest engine) read and pass in through EXISTING extension points:

  - Indicator enable/disable -> overrides the `dynamic_weights` dict already
    accepted by analyzer.generate_confluence_signal() (disabled = weight 0).
  - Thresholds (ADX/ATR/payout/etc.) -> passed through the `config` param
    already accepted by analyzer.calculate_filter_score().

No indicator, confluence, or filter-score function is modified by this
module or anything that imports it.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = "1.0"

# The 10 confluence factor keys — must match analyzer.DEFAULT_CONFLUENCE_WEIGHTS
# / backtest._DEFAULT_8F_WEIGHTS exactly (imported as a plain list of strings,
# not the weight values themselves, to avoid any risk of drifting from the
# single source of truth in analyzer.py).
INDICATOR_KEYS = [
    "rsi_div", "cci", "bb", "stoch", "candle",
    "sr", "round_number", "obv", "mean_reversion", "exhaustion",
]

_DEFAULT_INDICATOR_WEIGHT = 10.0  # mirrors analyzer.DEFAULT_CONFLUENCE_WEIGHTS' default share


def _default_settings() -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "general": {
            "default_timeframe": "5m",
            "default_asset": "EURUSD_otc",
            "refresh_interval_seconds": 30,
            "scanner_speed": "normal",   # "slow" | "normal" | "fast"
            "auto_refresh": True,
        },
        "quotex": {
            "last_validated_at": None,
            "last_validation_status": "unknown",  # "unknown" | "valid" | "invalid" | "error"
        },
        "indicators": {
            key: {"enabled": True, "weight": _DEFAULT_INDICATOR_WEIGHT, "threshold": {}}
            for key in INDICATOR_KEYS
        },
        "filters": {
            "adx_threshold": 25.0,
            "atr_threshold": 1.5,
            "min_payout": 80.0,
            "ema_lengths": [9, 21, 50],
            "min_confidence": 70.0,
            "min_filter_score": 60.0,
        },
        "scanner": {
            "refresh_seconds": 120,
            "asset_gap_seconds": 0.7,
            "top_n": 10,
            "min_confidence": 70.0,
            # Phase 8.1 additions below. Defaults are chosen so that a
            # scanner reading these for the first time behaves EXACTLY as
            # it did before Phase 8.1 — see scanner.py's ScannerEngine.start()
            # for how each is consumed (settings values are a fallback,
            # explicit route-call args always win).
            "scanner_enabled": True,       # False blocks new starts only; never force-stops a running scan
            "enabled_assets": [],          # empty = full OTC asset list (unchanged behavior)
            "enabled_timeframes": [],      # empty = ScannerConfig.timeframes default (["5m"])
            "scan_interval": None,         # None = ScannerConfig.cycle_interval_seconds default (120s)
            "minimum_filter_score": 0.0,   # 0 = no extra gate beyond the existing mandatory_pass check
            "top_signals": None,           # None = ScannerConfig.top_n default (10)
        },
        "backtest": {
            "min_candles": 2000,
            "candle_options": [500, 1000, 1500, 2000, 3000, 5000],
            "min_indicator_sample_size": 100,
            "auto_weight_update": False,
            "normalize_weights": True,
        },
        "ui_preferences": {
            "theme": "dark",
            "active_tab": "dashboard",
        },
    }


class SettingsStore:
    """
    File-backed settings store. Safe to instantiate multiple times (e.g. one
    per Flask worker) — every read/write goes straight to disk, no shared
    in-memory cache that could desync between processes.
    """

    def __init__(self, path: str, backups_path: Optional[str] = None, max_backups: int = 10):
        self.path = Path(path)
        self.backups_path = Path(backups_path or (str(self.path) + ".backups"))
        self.max_backups = max_backups
        if not self.path.exists():
            self._write(_default_settings())
        if not self.backups_path.exists():
            self._write_backups([])

    # ── Low-level I/O ────────────────────────────────────────────────────
    def _read(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = _default_settings()
            self._write(data)
        # Backfill any keys missing from an older settings.json (forward
        # compatibility with future settings additions).
        defaults = _default_settings()
        merged = _deep_merge_missing(data, defaults)
        if merged != data:
            self._write(merged)
        return merged

    def _write(self, data: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(self.path) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, self.path)   # atomic on POSIX — avoids a torn write

    def _read_backups(self) -> List[Dict[str, Any]]:
        try:
            with open(self.backups_path, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _write_backups(self, backups: List[Dict[str, Any]]) -> None:
        self.backups_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(self.backups_path) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(backups, f, indent=2)
        os.replace(tmp, self.backups_path)

    # ── Public API ───────────────────────────────────────────────────────
    def get(self) -> Dict[str, Any]:
        return self._read()

    def update(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        """Deep-merge patch into current settings and persist. Returns the new full settings."""
        current = self._read()
        merged = _deep_merge(current, patch)
        merged["schema_version"] = SCHEMA_VERSION
        self._write(merged)
        return merged

    def reset(self) -> Dict[str, Any]:
        defaults = _default_settings()
        self._write(defaults)
        return defaults

    def reset_section(self, section: str) -> Dict[str, Any]:
        """
        Phase 8.3 — reset ONE top-level section (e.g. "scanner") back to its
        default value, leaving every other section untouched. Additive:
        `reset()` above (full reset) is unchanged and still used by the
        existing global "Reset Settings" action. Raises KeyError if
        `section` isn't a recognized top-level key, so a typo/bad request
        fails loudly instead of silently no-op'ing.
        """
        defaults = _default_settings()
        if section not in defaults:
            raise KeyError(f"Unknown settings section: {section!r}")
        current = self._read()
        current[section] = defaults[section]
        current["schema_version"] = SCHEMA_VERSION
        self._write(current)
        return current

    def export_settings(self) -> Dict[str, Any]:
        return self._read()

    def import_settings(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Validated import — merges onto defaults so a partial/older export
        never leaves required keys missing."""
        if not isinstance(data, dict):
            raise ValueError("Imported settings must be a JSON object")
        merged = _deep_merge(_default_settings(), data)
        merged["schema_version"] = SCHEMA_VERSION
        self._write(merged)
        return merged

    def create_backup(self, reason: str = "manual") -> Dict[str, Any]:
        backups = self._read_backups()
        entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "reason": reason,
            "settings": self._read(),
        }
        backups.append(entry)
        backups = backups[-self.max_backups:]   # keep only the most recent N
        self._write_backups(backups)
        return entry

    def list_backups(self) -> List[Dict[str, Any]]:
        # Return metadata only (timestamp/reason), not the full settings blob,
        # to keep the listing endpoint lightweight.
        return [{"index": i, "timestamp": b["timestamp"], "reason": b["reason"]}
                for i, b in enumerate(self._read_backups())]

    def restore_backup(self, index: int) -> Dict[str, Any]:
        backups = self._read_backups()
        if index < 0 or index >= len(backups):
            raise IndexError(f"No backup at index {index} (have {len(backups)})")
        restored = backups[index]["settings"]
        self._write(restored)
        return restored

    # ── Feature-specific helpers ─────────────────────────────────────────
    def get_effective_dynamic_weights(self) -> Dict[str, float]:
        """
        Builds the weight dict to pass as generate_confluence_signal()'s
        `dynamic_weights` argument — disabled indicators get weight 0,
        enabled ones keep their configured weight. If normalize_weights is
        ON, rescales the enabled indicators' weights to sum to 100 (matching
        the same "always sums to 100" invariant DEFAULT_CONFLUENCE_WEIGHTS
        already guarantees) — a pure arithmetic transform, no confluence
        logic touched.
        """
        settings = self._read()
        indicators = settings["indicators"]
        weights = {}
        for key in INDICATOR_KEYS:
            cfg = indicators.get(key, {"enabled": True, "weight": _DEFAULT_INDICATOR_WEIGHT})
            weights[key] = float(cfg.get("weight", _DEFAULT_INDICATOR_WEIGHT)) if cfg.get("enabled", True) else 0.0

        if settings["backtest"].get("normalize_weights", True):
            total = sum(weights.values())
            if total > 0:
                weights = {k: round(v * 100.0 / total, 4) for k, v in weights.items()}
                # Bug fix (found in Phase 7.4 regression testing): rounding
                # each key independently can leave the sum a few 0.0001
                # short of/over 100 (e.g. 9 x 11.1111 = 99.9999). Assign the
                # residual to the largest weight so the documented "always
                # sums to 100" invariant holds exactly, not just approximately.
                drift = round(100.0 - sum(weights.values()), 4)
                if drift != 0.0:
                    top_key = max(weights, key=weights.get)
                    weights[top_key] = round(weights[top_key] + drift, 4)
        return weights

    def get_filter_score_config_overrides(self) -> Dict[str, Any]:
        """
        Builds the `config` override dict for analyzer.calculate_filter_score(),
        using only the parameter names that function already accepts
        (adx_trending, atr_extreme_pct, min_payout) — no new parameters
        invented, no function signature changes needed.
        """
        f = self._read()["filters"]
        return {
            "adx_trending": f["adx_threshold"],
            "atr_extreme_pct": f["atr_threshold"],
            "min_payout": f["min_payout"],
        }

    def apply_suggested_weights(self, suggested_weights: Dict[str, float], reason: str = "backtest") -> Dict[str, Any]:
        """
        Writes backtest-suggested weights into the indicators section.
        ALWAYS creates a backup first (never optional) — this is the only
        place indicator weights are changed by this store, and it is only
        ever called explicitly (see backtest_engine.py's apply-gate logic;
        this function itself does not evaluate the mandatory conditions —
        that gating lives in the caller, per Phase 7.2's rule that this must
        never happen automatically).
        """
        self.create_backup(reason=f"pre-apply:{reason}")
        current = self._read()
        for key, weight in suggested_weights.items():
            if key in current["indicators"]:
                current["indicators"][key]["weight"] = round(float(weight), 4)
        self._write(current)
        return current

    def clear_scanner_cache_flag(self) -> None:
        """No scanner state lives in settings.json (scanner.py owns its own
        in-memory cache) — this is a no-op placeholder kept for API symmetry
        with clear_backtest_cache; the actual cache clearing happens in
        scanner.py via its own methods, called directly by the route."""
        return None


def _deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge patch into base, patch values win."""
    result = dict(base)
    for k, v in patch.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _deep_merge_missing(data: Dict[str, Any], defaults: Dict[str, Any]) -> Dict[str, Any]:
    """Fill in any keys present in defaults but missing from data (forward
    compatibility for older settings.json files), without overwriting
    anything the user already has set."""
    result = dict(data)
    for k, v in defaults.items():
        if k not in result:
            result[k] = v
        elif isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge_missing(result[k], v)
    return result
