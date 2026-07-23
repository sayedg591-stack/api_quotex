# Quotex OTC Signal Platform

Manual-trading-assist analysis platform for Quotex OTC pairs. **This tool
never places trades.** It analyzes candle data and produces BUY/SELL/WAIT
signals with a confidence score and a quality "Filter Score" for a human to
act on manually.

**New here?** Read `docs/HANDOFF.md` first for a fast orientation, then
`docs/PROJECT_STATUS.md` for the full current-state snapshot. See
`docs/RELEASE_NOTES.md` for what's new this release.

## Features (as actually built)
- Multi-indicator confluence engine (10 factors: EMA-adjacent trend info,
  RSI divergence, Bollinger Bands, OBV divergence, Support/Resistance
  zones, Candlestick patterns, CCI, Stochastic, Mean Reversion, Candle
  Exhaustion, Round Number).
- Backtest-driven dynamic weighting (`compute_dynamic_weights()`), with a
  minimum-sample-size safeguard against overfitting on thin data.
- Graded Filter Score (0-100), 7 criteria (EMA/ADX/ATR/Support-Resistance/
  Multi-Timeframe/Payout/Candlestick), with a separate binary
  `mandatory_pass` gate for scanner visibility.
- Background Smart Scanner across all 53 OTC assets — state machine
  (STOPPED/RUNNING/PAUSED/YIELDING/DEGRADED/RECOVERING), manual-request
  priority, TTL cache cleanup, metrics, history, event log.
- Background Backtest Engine (`backtest_engine.py`) — separate from the
  scanner, same lifecycle pattern, reuses `backtest.py`'s functions exactly.
  Candle counts: 500/1000/1500/2000/3000/5000.
- Persistent Settings Store (`settings_store.py`) — JSON-file-backed,
  backup/restore, export/import.
- Indicator Registry (`indicator_registry.py`) — single metadata source for
  all indicators, including 3 new (not-yet-confluence-connected) OTC
  indicators: Wick Rejection, Liquidity Sweep, False Breakout.
- **(Phase 7.4)** Full HTTP + UI layer for all of the above: Settings page
  (editable thresholds/scanner config, backup/restore/export/import),
  Backtest page (run/pause/resume/stop with live progress + ETA, results
  table, one-tap Apply Suggested Weights), Indicators page (enable/disable
  toggles, dynamic weight + accuracy display), and a Quotex Session page
  (paste/save SSID, validate connection) — no code editing or redeploy
  needed for day-to-day tuning. See `docs/RELEASE_NOTES.md`.

**Not yet built:** connecting the 3 new indicators (Wick Rejection,
Liquidity Sweep, False Breakout) to the confluence vote — they're fully
implemented and visible in the Indicators page, but deliberately excluded
from `DEFAULT_CONFLUENCE_WEIGHTS` pending a separate, explicit approval
step. A genuine user-facing "Smart Scanner" page (Phase 8) has also not
been started.

## Architecture
See `docs/ARCHITECTURE.md` for full detail. Summary: Quotex WebSocket client →
candle fetcher → indicator engine → confluence engine → filter score →
Flask API. Scanner and Backtest Engine are background async loops sharing
one event loop with the Flask app.

## Folder Tree
```
Quotex/
├── quotex/api_quotex/        # Quotex client/WebSocket/login (untouched)
├── market_analyzer/
│   ├── indicators.py
│   ├── analyzer.py
│   ├── backtest.py
│   ├── config.py
│   ├── fetch_data.py
│   └── webapp/
│       ├── app.py
│       ├── scanner.py
│       ├── backtest_engine.py
│       ├── settings_store.py
│       ├── indicator_registry.py
│       ├── preflight_check.py
│       ├── templates/index.html
│       └── static/{app.js, style.css}
├── docs/                     # PROJECT_STATUS, CHANGELOG, NEXT_PHASE, TEST_REPORT,
│                              # HANDOFF, RELEASE_NOTES, ARCHITECTURE, etc.
```

## Installation
```bash
cd Quotex
pip install -r requirements.txt
playwright install chromium   # if not already present
cp .env.example .env          # fill in QUOTEX_SSID or QX_EMAIL/QX_PASSWORD
```

## Requirements
See `requirements.txt` / `pyproject.toml`. Key packages: Flask, gunicorn,
loguru, cloudscraper, playwright, pydantic, pandas, numpy, websockets.

## Environment Variables
| Var | Purpose |
|---|---|
| `QUOTEX_SSID` | Preferred auth method — session ID from an authenticated browser session |
| `QX_EMAIL` / `QX_PASSWORD` | Fallback login, only used if `QUOTEX_SSID` unset |
| `PORT` | Flask/Gunicorn port (default 5000) |
| `WEB_CONCURRENCY` | Gunicorn worker count |
| `MIN_PAYOUT` *(code constant, not env)* | see `config.py` |

## Run Locally
```bash
cd Quotex/market_analyzer/webapp
python3 app.py
```

## Run on Replit
`.replit` → `start-prod.sh` → runs `preflight_check.py` then
`gunicorn -c gunicorn.conf.py app:app`.

## Deployment
See `docs/PROJECT_STATUS.md`'s Deployment Decisions section. No changes to the
deployment pipeline in Phase 7.x.

## Troubleshooting
- If `preflight_check.py` fails: check the specific CRITICAL failure it
  prints (missing package / missing required file). Warnings (Chromium
  missing, no Quotex session) do not block startup.
- If the scanner never returns results: check `mandatory_pass` in
  `/api/scanner/results` cached entries — a passing `filter_score` alone no
  longer implies visibility (Phase 7.1 change).
- Any unexpected server-side error on an `/api/*` route now returns a
  structured JSON `500` (`{"ok": false, "error": ..., "detail": ...}`)
  instead of an HTML error page (Phase 7.4's generic error handler) —
  check the `detail` field first.

## Backtesting
`POST /api/backtest/run` with `{"assets": [...], "timeframe": "5m",
"candle_count": 2000}` (candle_count must be one of
500/1000/1500/2000/3000/5000). Monitor with `GET /api/backtest/status`
(includes ETA), review `GET /api/backtest/results`, and optionally
`POST /api/backtest/apply-weights` once enough data has been gathered.
Or just use the Backtest page in the UI. See `docs/API_DOCUMENTATION.md`.

## Scanner
`POST /api/scanner/start` with `{"timeframes": ["5m"], "top_n": 10,
"min_confidence": 70, "refresh_seconds": 120}`. See `docs/API_DOCUMENTATION.md`.
Note: the UI's "Auto Scanner" tab runs a separate client-side polling loop
and does not call these routes — see `docs/HANDOFF.md` for that
architecture split. It never starts on its own; press "Enable Auto
Scanner" to start it, and it's always off again on your next visit.

## Settings
Backend (`settings_store.py`) and a full Settings page in the UI: edit
thresholds and scanner config, save, reset to defaults, backup/restore
(last 10 kept), export/import. `GET/POST /api/settings` and friends — see
`docs/API_DOCUMENTATION.md` / `docs/SETTINGS_REFERENCE.md`.

## Quotex Session
Existing `QUOTEX_SSID`/`session.json` mechanism, unchanged. A Quotex
Session page in the UI lets you paste/save an SSID (written only to
`session.json`, never an env var) and validate the connection —
`GET/POST /api/session/status|update|validate`.

## Future Roadmap
See `docs/NEXT_PHASE.md` for the detailed, task-level breakdown. Phase 7.4
(Settings/Backtest/Indicators/Session routes + UI) is complete; Phase 8
(a genuine user-facing Scanner page) has not been started and needs scope
confirmation first.

## FAQ
**Does this place trades automatically?** No — analysis only, by design,
throughout the entire project (see docs/PROJECT_MEMORY.md).

**Why do some Filter Score buckets show 0 trades in the backtest report?**
By design — see docs/PROJECT_MEMORY.md's "why graded scoring replaced binary
scoring" entry.

**Can I trust the performance numbers in this repo?** They're real
measurements, but only ever against **synthetic data in a sandboxed
environment with no network access** — never against live Quotex traffic.
Treat them as relative/structural indicators, not production guarantees.
