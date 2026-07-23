#!/usr/bin/env python3
"""
Pre-flight health check — run before Gunicorn starts.

Verifies the environment is sane enough to serve traffic. This is a
read-only diagnostic script: it does not modify any files, does not touch
trading logic/indicators/Quotex API/WebSocket code, and does not attempt
to log in to Quotex or place any trade.

Exit codes:
  0 = OK to start (CRITICAL checks passed; WARNINGs are printed but non-fatal)
  1 = CRITICAL failure — startup should be aborted

Usage:
  python3 preflight_check.py
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

# ── Paths (mirrors the bootstrap in app.py) ─────────────────────────────────
_WEBAPP_DIR = Path(__file__).resolve().parent          # market_analyzer/webapp/
_MARKET_DIR = _WEBAPP_DIR.parent                        # market_analyzer/
_ROOT       = _MARKET_DIR.parent                        # Quotex/
_QUOTEX_DIR = _ROOT / "quotex"                           # quotex/

CRITICAL_FAILURES: list[str] = []
WARNINGS: list[str] = []


def check(label: str, ok: bool, detail: str = "", critical: bool = True) -> None:
    status = "PASS" if ok else ("FAIL" if critical else "WARN")
    line = f"[{status}] {label}"
    if detail:
        line += f" — {detail}"
    print(line)
    if not ok:
        (CRITICAL_FAILURES if critical else WARNINGS).append(label)


def main() -> int:
    print("=" * 70)
    print("Quotex Signal Platform — Pre-flight Health Check")
    print("=" * 70)

    # ── 1. Python environment ───────────────────────────────────────────────
    py_ok = sys.version_info >= (3, 10)
    check(
        "Python environment",
        py_ok,
        f"running {sys.version.split()[0]} (need >= 3.10)",
        critical=True,
    )

    # ── 2. Required dependency imports ──────────────────────────────────────
    # Only checks that packages import successfully — does not exercise any
    # trading/indicator/WebSocket code paths.
    required_packages = [
        "flask",
        "loguru",
        "cloudscraper",
        "bs4",
        "requests",
        "websockets",
        "pandas",
        "numpy",
        "pydantic",
        "playwright",
    ]
    for pkg in required_packages:
        try:
            importlib.import_module(pkg)
            check(f"Dependency importable: {pkg}", True)
        except Exception as exc:  # noqa: BLE001
            check(f"Dependency importable: {pkg}", False, str(exc), critical=True)

    # ── 3. Playwright / Chromium availability ──────────────────────────────
    # Non-fatal: login will fail later if missing, but the analysis-only
    # dashboard routes that don't need a live browser can still serve.
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            exe_path = Path(p.chromium.executable_path)
            chromium_ok = exe_path.exists()
            check(
                "Playwright Chromium binary present",
                chromium_ok,
                str(exe_path) if chromium_ok else (
                    f"not found at {exe_path} — run `playwright install chromium`"
                ),
                critical=False,
            )
    except Exception as exc:  # noqa: BLE001
        check(
            "Playwright Chromium binary present",
            False,
            f"could not verify: {exc} — run `playwright install chromium`",
            critical=False,
        )

    # ── 4. Configuration availability ───────────────────────────────────────
    config_json = _QUOTEX_DIR / "config.json"
    check(
        "quotex/config.json present",
        config_json.exists(),
        str(config_json),
        critical=False,
    )

    has_ssid_env = bool(os.environ.get("QUOTEX_SSID", "").strip())
    session_json = _QUOTEX_DIR / "session.json"
    has_session_file = session_json.exists()
    check(
        "Quotex session available (QUOTEX_SSID env or session.json)",
        has_ssid_env or has_session_file,
        "found" if (has_ssid_env or has_session_file) else (
            "none found — login will be required before live analysis works"
        ),
        critical=False,
    )

    # ── 5. Required folders/files ───────────────────────────────────────────
    required_paths = [
        (_WEBAPP_DIR / "templates" / "index.html", "templates/index.html"),
        (_WEBAPP_DIR / "static", "static/"),
        (_MARKET_DIR / "indicators.py", "market_analyzer/indicators.py"),
        (_MARKET_DIR / "analyzer.py", "market_analyzer/analyzer.py"),
        (_MARKET_DIR / "fetch_data.py", "market_analyzer/fetch_data.py"),
        (_MARKET_DIR / "config.py", "market_analyzer/config.py"),
        (_QUOTEX_DIR / "api_quotex", "quotex/api_quotex/"),
    ]
    for path_obj, label in required_paths:
        check(f"Required path exists: {label}", path_obj.exists(), str(path_obj), critical=True)

    # ── Summary ──────────────────────────────────────────────────────────────
    print("=" * 70)
    if CRITICAL_FAILURES:
        print(f"RESULT: {len(CRITICAL_FAILURES)} CRITICAL failure(s) — aborting startup.")
        for f in CRITICAL_FAILURES:
            print(f"  - {f}")
        print("=" * 70)
        return 1

    if WARNINGS:
        print(f"RESULT: OK to start, with {len(WARNINGS)} warning(s):")
        for w in WARNINGS:
            print(f"  - {w}")
    else:
        print("RESULT: All checks passed.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
