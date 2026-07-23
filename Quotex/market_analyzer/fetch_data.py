"""
Fetch market data from Quotex using AsyncQuotexClient.
Handles session loading, connection, candle history, and live streaming.
NEVER places orders — data retrieval only.
"""

from __future__ import annotations
import sys
import os
import json
import asyncio
from pathlib import Path
from typing import List, Optional, Callable, Any

import pandas as pd

# ── Path setup ──────────────────────────────────────────────────────────────
_MARKET_DIR = Path(__file__).resolve().parent       # market_analyzer/
_ROOT       = _MARKET_DIR.parent                    # workspace root
_QUOTEX_DIR = _ROOT / "quotex"                      # quotex/

# Insert quotex/ so that `import api_quotex` works
if str(_QUOTEX_DIR) not in sys.path:
    sys.path.insert(0, str(_QUOTEX_DIR))

# Insert market_analyzer/ so sibling modules (config, indicators, analyzer) resolve
if str(_MARKET_DIR) not in sys.path:
    sys.path.insert(0, str(_MARKET_DIR))

# ── Imports that depend on path setup ────────────────────────────────────────
from api_quotex import AsyncQuotexClient
from api_quotex.models import Candle
from api_quotex.constants import TIMEFRAMES as API_TIMEFRAMES
import config as cfg


# ─── Session loading ─────────────────────────────────────────────────────────

def load_session_ssid() -> str:
    """
    Load the SSID.  Priority order:
      1. QUOTEX_SSID environment variable  (Replit Secrets / deployment env)
      2. quotex/session.json               (written by quotex/login.py)
    Raises FileNotFoundError / ValueError with actionable fix instructions.
    SAFETY: never prints or logs the SSID value — only its length.
    """
    # ── Priority 1: QUOTEX_SSID environment variable ────────────────────────
    env_ssid = os.environ.get("QUOTEX_SSID", "").strip()
    if env_ssid:
        if env_ssid == "[object Storage]" or len(env_ssid) < 10:
            raise ValueError(
                f"QUOTEX_SSID env var is set but looks invalid "
                f"(length={len(env_ssid)}). "
                "Paste the full SSID value from Quotex DevTools."
            )
        print(f"  → SSID loaded from QUOTEX_SSID env var "
              f"(length: {len(env_ssid)})")   # value never printed
        return env_ssid

    # ── Priority 2: session.json fallback ───────────────────────────────────
    session_path = _ROOT / cfg.SESSION_FILE
    if not session_path.exists():
        raise FileNotFoundError(
            f"\n[ERROR] Session file not found: {session_path}\n"
            "  Option A (recommended): Set QUOTEX_SSID in Replit Secrets.\n"
            "  Option B: Run:  python quotex/login.py\n"
            "  Then try again."
        )
    try:
        data = json.loads(session_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"Session file is corrupted ({e}). Re-run login.py.")

    ssid = data.get("ssid", "").strip()
    if not ssid or ssid == "[object Storage]" or len(ssid) < 10:
        raise ValueError(
            f"\n[ERROR] Invalid SSID in {session_path}\n"
            "  Option A (recommended): Set QUOTEX_SSID in Replit Secrets.\n"
            "  Option B: Run:  python quotex/login.py   and log in again."
        )
    print(f"  → SSID loaded from session.json "
          f"(length: {len(ssid)})")          # value never printed
    return ssid


# ─── Data fetcher ────────────────────────────────────────────────────────────

class QuotexDataFetcher:
    """
    Connects to Quotex, downloads historical candles, and optionally streams
    live price updates.  Does NOT place any orders.
    """

    def __init__(self, asset: str = cfg.DEFAULT_ASSET,
                 is_demo: bool = cfg.IS_DEMO):
        self.asset   = asset
        self.is_demo = is_demo
        self._client: Optional[AsyncQuotexClient] = None
        self._live_prices: List[dict] = []

    # ── Connection ───────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Load session SSID and open the WebSocket connection."""
        ssid = load_session_ssid()   # prints source + length (never the value)

        self._client = AsyncQuotexClient(
            ssid=ssid,
            is_demo=self.is_demo,
            enable_logging=False,   # suppress library logs; we print our own
        )
        print("  → Connecting to Quotex WebSocket …")
        connected = await self._client.connect()
        if not connected:
            raise ConnectionError(
                # TASK 5: clear session-expiry guidance
                "Failed to connect to Quotex — session may be expired.\n"
                "  Fix option 1 (DevTools): Open browser → quotex.io → DevTools (F12) → "
                "Network tab → WS filter → find WebSocket request → "
                "copy 'ssid' value from URL or payload → paste into quotex/session.json\n"
                "  Fix option 2 (auto-login): Re-run:  bash quotex/run_login.sh"
            )
        print("  → Connected ✓")
        # Give the server a brief moment to push the initial instruments/list frame.
        # 0.5 s is enough over a normal WebSocket round-trip; the old 2 s was
        # a conservative guess and added unnecessary latency to every request.
        await asyncio.sleep(0.5)

    async def disconnect(self) -> None:
        """Close the WebSocket connection cleanly."""
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None

    # ── Candles (history) ────────────────────────────────────────────────────

    async def get_candles(self, asset: str | None = None,
                          timeframe: str | None = None,
                          count: int | None = None) -> List[Candle]:
        """
        Fetch historical OHLCV candles from Quotex.
        Uses AsyncQuotexClient.get_candles() which reuses the existing
        WebSocket protocol (instruments/follow + instruments/update +
        chart_notification/get) and internal _on_candles_received handler.
        """
        asset     = asset     or self.asset
        timeframe = timeframe or cfg.PRIMARY_TIMEFRAME
        count     = count     or cfg.CANDLE_COUNT

        if not self._client:
            raise RuntimeError("Not connected. Call connect() first.")

        print(f"  → Fetching {count} candles [{timeframe}] for {asset} …")
        candles = await self._client.get_candles(asset, timeframe, count)
        if candles:
            print(f"  → Received {len(candles)} candles ✓")
        else:
            print("  ⚠  No candles returned — server may not have this asset/timeframe open.")
        return candles

    async def get_candles_df(self, asset: str | None = None,
                             timeframe: str | None = None,
                             count: int | None = None) -> pd.DataFrame:
        """
        Fetch historical candles and return them as a pandas DataFrame.
        Index = timestamp (datetime).  Columns: open, high, low, close, volume.
        """
        candles = await self.get_candles(asset, timeframe, count)
        if not candles:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        rows = [{
            "timestamp": c.timestamp,
            "open":   c.open,
            "high":   c.high,
            "low":    c.low,
            "close":  c.close,
            "volume": float(c.volume) if c.volume is not None else 0.0,
        } for c in candles]

        df = pd.DataFrame(rows)
        df.set_index("timestamp", inplace=True)
        df.sort_index(inplace=True)
        return df

    # ── Live streaming ────────────────────────────────────────────────────────

    async def subscribe_live(self, asset: str | None = None,
                             timeframe: str | None = None,
                             duration: float = cfg.LIVE_STREAM_SECONDS,
                             on_price: Optional[Callable[[dict], None]] = None,
                             ) -> List[dict]:
        """
        Collect live price ticks for `duration` seconds using the Quotex
        instruments/history protocol that AsyncQuotexClient already implements.

        How it works
        ────────────
        get_candles() (called before this method) sends:
          42["instruments/follow","<asset>"]
          42["instruments/update",{"asset":"<asset>","period":<tf>}]
          42["chart_notification/get",{"asset":"<asset>","version":"1.0.0"}]

        After those messages the server keeps streaming live price ticks via
        quote_stream events → _on_quote_stream() → price_update events.
        This method registers callbacks on those two events, calls
        request_chart_notifications() to re-activate the stream, waits
        `duration` seconds, then unregisters.

        subscribe_symbol / unsubscribe_symbol are NOT used — they are not
        part of the Quotex WebSocket protocol.

        Returns a list of {"symbol", "timestamp", "price"} dicts.
        NEVER places orders.
        """
        if not self._client:
            raise RuntimeError("Not connected. Call connect() first.")

        asset     = asset     or self.asset
        timeframe = timeframe or cfg.PRIMARY_TIMEFRAME

        self._live_prices.clear()
        seen_timestamps: set = set()

        def _on_price_update(data: Any) -> None:
            """Collect normalised price_update dicts for this asset only."""
            if not isinstance(data, dict):
                return
            if data.get("symbol") != asset:
                return
            ts = data.get("timestamp")
            if ts in seen_timestamps:
                return
            seen_timestamps.add(ts)
            entry = {
                "symbol":    asset,
                "timestamp": ts,
                "price":     data.get("price"),
            }
            self._live_prices.append(entry)
            if on_price:
                try:
                    on_price(entry)
                except Exception:
                    pass

        def _on_quote_stream(data: Any) -> None:
            """
            Belt-and-suspenders: catch raw quote_stream payloads in case
            price_update hasn't fired for a tick yet.
            quote_stream payload is a list of [symbol, timestamp, price, …].
            """
            if not isinstance(data, list):
                return
            for quote in data:
                if (not isinstance(quote, (list, tuple))
                        or len(quote) < 3
                        or quote[0] != asset):
                    continue
                ts = quote[1]
                if ts in seen_timestamps:
                    continue
                seen_timestamps.add(ts)
                try:
                    price = float(quote[2])
                except (TypeError, ValueError):
                    continue
                entry = {"symbol": asset, "timestamp": ts, "price": price}
                self._live_prices.append(entry)
                if on_price:
                    try:
                        on_price(entry)
                    except Exception:
                        pass

        self._client.add_event_callback("price_update",  _on_price_update)
        self._client.add_event_callback("quote_stream",  _on_quote_stream)

        # Re-send chart_notification/get to ensure the server keeps the stream
        # active.  instruments/follow was already sent by get_candles().
        try:
            await self._client.request_chart_notifications(asset)
        except Exception as exc:
            print(f"  ⚠  chart_notification/get failed (non-fatal): {exc}")

        print(f"  → Listening for live prices [{timeframe}] ({duration}s) …")
        await asyncio.sleep(duration)

        try:
            self._client.remove_event_callback("price_update", _on_price_update)
        except Exception:
            pass
        try:
            self._client.remove_event_callback("quote_stream", _on_quote_stream)
        except Exception:
            pass

        print(f"  → Captured {len(self._live_prices)} live price ticks ✓")
        return list(self._live_prices)

    # ── Payout ───────────────────────────────────────────────────────────────

    async def get_payout(self, asset: str, timeframe: str) -> float:
        """
        Task 3: Fetch payout % for asset/timeframe via the Quotex client.
        Returns 0.0 if the client is not connected or the call fails.
        NEVER places orders — read-only metadata query.
        """
        if not self._client:
            return 0.0
        try:
            result = await self._client.get_payout(asset, timeframe)
            return float(result) if result is not None else 0.0
        except Exception:
            return 0.0

    # ── CSV export ────────────────────────────────────────────────────────────

    @staticmethod
    def save_candles_csv(df: pd.DataFrame, path: str | None = None) -> str:
        """
        Save a candle DataFrame to CSV.
        Default path: market_analyzer/output/candles.csv
        Returns the absolute path that was written.
        """
        if path is None:
            out_dir = _ROOT / cfg.OUTPUT_DIR
            out_dir.mkdir(parents=True, exist_ok=True)
            path = str(out_dir / cfg.CSV_FILENAME)
        df.to_csv(path)
        return path
