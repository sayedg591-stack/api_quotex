"""
Diagnostic: open qxbroker sign-in, wait for JS hydration, then dump
every input/button/frame so we know exactly what selectors to target.
Run from workspace root: python3 quotex/diagnose_login.py
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from playwright.async_api import async_playwright

# ── reuse the LD_LIBRARY_PATH already exported by run_login.sh ──────────────
PLAYWRIGHT_BROWSERS_PATH = str(Path(__file__).parent / ".playwright-browsers")
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", PLAYWRIGHT_BROWSERS_PATH)
os.environ.setdefault("PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS", "true")


async def dump_inputs(frame, label: str) -> None:
    try:
        result = await frame.evaluate("""() => {
            const inputs = Array.from(document.querySelectorAll('input'));
            return inputs.map(el => ({
                tag:          el.tagName,
                type:         el.type || '',
                name:         el.name || '',
                id:           el.id   || '',
                placeholder:  el.placeholder || '',
                autocomplete: el.getAttribute('autocomplete') || '',
                ariaLabel:    el.getAttribute('aria-label') || '',
                className:    el.className || '',
                visible:      el.offsetParent !== null,
                parentForm:   el.form ? (el.form.id || el.form.action || '') : '',
            }));
        }""")
        print(f"\n  [{label}] — {len(result)} input(s) found")
        for r in result:
            print(f"    type={r['type']!r:10s} name={r['name']!r:20s} id={r['id']!r:20s} "
                  f"placeholder={r['placeholder']!r:30s} autocomplete={r['autocomplete']!r:20s} "
                  f"aria-label={r['ariaLabel']!r:20s} visible={r['visible']} "
                  f"class={r['className'][:60]!r}")
    except Exception as e:
        print(f"  [{label}] — error evaluating inputs: {e}")


async def dump_buttons(frame, label: str) -> None:
    try:
        result = await frame.evaluate("""() => {
            const btns = Array.from(document.querySelectorAll('button, input[type=submit], a[href*=sign]'));
            return btns.slice(0, 20).map(el => ({
                tag:     el.tagName,
                type:    el.type || '',
                text:    (el.innerText || el.value || '').trim().slice(0, 60),
                id:      el.id || '',
                className: el.className.slice(0, 80) || '',
                visible: el.offsetParent !== null,
            }));
        }""")
        print(f"\n  [{label}] — {len(result)} button/link(s)")
        for r in result:
            if r['text'] or r['id']:
                print(f"    tag={r['tag']:8s} type={r['type']:10s} text={r['text']!r:40s} "
                      f"id={r['id']!r:15s} visible={r['visible']}")
    except Exception as e:
        print(f"  [{label}] — error evaluating buttons: {e}")


async def dump_forms(frame, label: str) -> None:
    try:
        result = await frame.evaluate("""() => {
            return Array.from(document.querySelectorAll('form')).map(f => ({
                id:     f.id || '',
                action: f.action || '',
                method: f.method || '',
                className: f.className.slice(0, 80) || '',
            }));
        }""")
        print(f"\n  [{label}] — {len(result)} form(s)")
        for r in result:
            print(f"    id={r['id']!r:20s} action={r['action']!r:60s} class={r['className']!r}")
    except Exception as e:
        print(f"  [{label}] — error evaluating forms: {e}")


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--ignore-certificate-errors",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            viewport={"width": 1366, "height": 768},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = await context.new_page()

        url = "https://qxbroker.com/en/sign-in/"
        print(f"[*] Navigating to {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)

        # Wait progressively: try to see any input appear
        for wait_ms in (1000, 2000, 3000, 4000):
            try:
                await page.wait_for_selector("input", state="visible", timeout=wait_ms)
                print(f"[*] Found <input> after ~{wait_ms}ms cumulative wait")
                break
            except Exception:
                print(f"[*] No <input> visible yet at {wait_ms}ms — still waiting...")

        # Extra settle time for JS rendering
        await page.wait_for_timeout(2000)

        # ── Page URL & title ─────────────────────────────────────────────────
        print(f"\n[*] Final URL : {page.url}")
        print(f"[*] Page title: {await page.title()}")

        # ── Screenshot ───────────────────────────────────────────────────────
        ss_path = Path(__file__).parent / "diagnose_screenshot.png"
        await page.screenshot(path=str(ss_path), full_page=True)
        print(f"[*] Screenshot saved: {ss_path}")

        # ── All tab/panel IDs ────────────────────────────────────────────────
        tab_info = await page.evaluate("""() => {
            const els = document.querySelectorAll('[id*=tab], [class*=tab], [role=tab], [role=tabpanel]');
            return Array.from(els).slice(0, 20).map(e => ({
                tag: e.tagName, id: e.id, role: e.getAttribute('role') || '',
                className: e.className.slice(0, 80), text: (e.innerText||'').trim().slice(0,40)
            }));
        }""")
        print(f"\n[*] Tab/panel elements ({len(tab_info)}):")
        for t in tab_info:
            print(f"    tag={t['tag']:8s} id={t['id']!r:20s} role={t['role']:12s} "
                  f"class={t['className'][:50]!r} text={t['text']!r}")

        # ── Main frame ───────────────────────────────────────────────────────
        print("\n[*] === MAIN FRAME ===")
        await dump_forms(page, "main frame")
        await dump_inputs(page, "main frame")
        await dump_buttons(page, "main frame")

        # ── All child frames ─────────────────────────────────────────────────
        frames = page.frames
        print(f"\n[*] Total frames: {len(frames)}")
        for i, fr in enumerate(frames):
            if fr == page.main_frame:
                continue
            print(f"\n[*] === FRAME {i}: url={fr.url!r} ===")
            await dump_forms(fr, f"frame[{i}]")
            await dump_inputs(fr, f"frame[{i}]")
            await dump_buttons(fr, f"frame[{i}]")

        # ── Shadow DOM scan ──────────────────────────────────────────────────
        print("\n[*] Shadow DOM scan (up to 5 levels):")
        shadow_inputs = await page.evaluate("""() => {
            const results = [];
            function walk(root, depth) {
                if (depth > 5) return;
                const nodes = root.querySelectorAll ? Array.from(root.querySelectorAll('*')) : [];
                for (const n of nodes) {
                    if (n.tagName === 'INPUT') {
                        results.push({
                            type: n.type, name: n.name, id: n.id,
                            placeholder: n.placeholder,
                            depth: depth, host: root.nodeName || 'document'
                        });
                    }
                    if (n.shadowRoot) walk(n.shadowRoot, depth + 1);
                }
            }
            walk(document, 0);
            return results;
        }""")
        if shadow_inputs:
            print(f"  Found {len(shadow_inputs)} input(s) in shadow DOM:")
            for s in shadow_inputs:
                print(f"    depth={s['depth']} host={s['host']} type={s['type']!r} "
                      f"name={s['name']!r} id={s['id']!r} placeholder={s['placeholder']!r}")
        else:
            print("  None found in shadow DOM.")

        # ── Raw relevant HTML snippet ─────────────────────────────────────────
        print("\n[*] Relevant HTML (forms + inputs + sign-* elements):")
        html_snippet = await page.evaluate("""() => {
            const clone = document.cloneNode(true);
            // grab all form containers
            const out = [];
            document.querySelectorAll('form, [class*=sign], [class*=login], [class*=auth]').forEach(el => {
                out.push(el.outerHTML.slice(0, 800));
            });
            return out.join('\n---\n').slice(0, 8000);
        }""")
        print(html_snippet[:6000])

        await browser.close()
        print("\n[*] Done.")


if __name__ == "__main__":
    asyncio.run(main())
