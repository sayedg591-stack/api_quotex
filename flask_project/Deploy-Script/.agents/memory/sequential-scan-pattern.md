---
name: Sequential scanning for slow/fragile backends
description: Client-side scan-list pattern for backends wrapping slow or rate-limited real-time sources
---

When a backend endpoint wraps a slow, rate-limited, or stateful real-time upstream (e.g. a
single shared websocket session to a third-party trading/data platform), don't fan out
concurrent client requests across a list of items (assets, pairs, records) to "scan" them.
Concurrent requests against a small worker pool backed by one shared upstream connection can
saturate or wedge the backend for minutes.

**Why:** Observed a multi-minute wedge when a scanner-style feature fired many requests at once
against a 2-worker backend proxying a single third-party websocket session.

**How to apply:** Implement scanning as a client-side sequential loop: one request at a time,
per-request timeout via `AbortController`, a fixed small delay between requests (several hundred
ms), a capped list size (don't scan everything, curate the most relevant subset), and pause the
loop when the consuming view/tab isn't active. Treat individual item failures (e.g. "no data for
this asset") as skippable, not fatal to the whole scan.
