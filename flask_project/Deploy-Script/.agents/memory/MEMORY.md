# Memory Index

- [CSS `[hidden]` override bug](css-hidden-attribute-override.md) — components with an explicit `display` value can silently override the `hidden` attribute; always add a global `[hidden]{display:none!important}` guard.
- [Sequential scanning for slow/fragile backends](sequential-scan-pattern.md) — when a backend wraps a slow/rate-limited real-time source, scan lists client-side sequentially with per-request timeout + inter-request delay, not concurrently.
