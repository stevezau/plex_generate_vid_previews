#!/usr/bin/env python3
"""Regenerate the 5 README screenshots — dark mode, no real data.

Usage:
    python tests/e2e/snapshots/regen_readme.py --out docs/images/

Boots the app in a temp config dir that has been pre-seeded with
fake Plex / Jellyfin / Emby servers (see ``readme_fixture.py``) and a
handful of plausible job rows, then drives Playwright to capture:

    /               -> home.png        (full page)
    /servers        -> servers.png     (full page)
    /settings       -> settings.png    (Processing Options card only)
    /automation     -> automation.png  (Triggers tab default)

All captures are dark-mode + desktop-only — this script targets the
README exclusively. For the visual-regression matrix (light + dark ×
desktop + mobile × 7 surfaces), use ``collect.py`` instead.

Defense-in-depth against leaking real data: we (a) boot against an
isolated temp config dir with fake servers, (b) stub
``/api/system/media-servers`` so the dashboard's "connected" badges
don't depend on actually reaching the fake hosts, (c) override
``window.location.origin`` to a placeholder so the webhook URL panel
renders ``https://your-server.local:8080/...``, and (d) run a
MutationObserver that scrubs any stray IP / ``stevez0`` string that
slips into a text node — belt-and-braces, in case an unseen surface
renders one.

Requires: ``playwright install chromium`` (one-time setup).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

# Put the repo root first so ``media_preview_generator.*`` imports work
# when this file is invoked as an absolute path (the common case: see
# scripts/regen_readme_screenshots.sh). Then the parent dir so siblings
# (collect, readme_fixture) import cleanly.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from collect import (  # noqa: E402
    _get_free_port,
    _login_and_get_cookie,
    _mark_setup_complete,
    _start_app,
)
from playwright.sync_api import BrowserContext, Page, sync_playwright  # noqa: E402
from readme_fixture import FAKE_HOST, FAKE_SERVERS, seed_jobs, write_settings  # noqa: E402

PLACEHOLDER_ORIGIN = f"https://{FAKE_HOST}:8080"

# Full-page captures. Settings is handled separately below because we
# crop to the Processing Options card rather than capturing the whole
# page (the full Settings page is ~4000px tall and unsuitable for a
# README gallery tile).
SURFACES: list[tuple[str, str]] = [
    ("/", "home"),
    ("/servers", "servers"),
    ("/automation", "automation"),
]

# Init script (runs in every page before app JS boots). Two jobs:
# 1. Override window.location.origin so _refreshWebhookUrls() in
#    _automation_triggers.html writes placeholder URLs instead of the
#    capture-host URL.
# 2. Install a MutationObserver that rewrites text nodes + <input>
#    values containing a real IP or "stevez0" to the placeholder host.
INIT_SCRIPT = """
(() => {
  const PLACEHOLDER_ORIGIN = '__PLACEHOLDER_ORIGIN__';
  const PLACEHOLDER_HOST = '__PLACEHOLDER_HOST__';
  window.__scrubRan = true;
  try { localStorage.setItem('theme', 'dark'); } catch (e) {}

  // Any string matching one of these patterns is a leak risk and gets
  // rewritten to the placeholder. ``http://localhost:NNNN`` covers the
  // webhook URL widget (servers.html:35, _automation_triggers.html:495)
  // which builds its value from ``window.location.origin`` — Location.origin
  // is a getter on the prototype, so defineProperty overrides are brittle;
  // text-level scrub is simpler and safer.
  const IP_RX = /\\b(?:\\d{1,3}\\.){3}\\d{1,3}(?::\\d+)?\\b/g;
  const LEAK_RX = /stevez0[a-z0-9.-]*/gi;
  const LOCALHOST_RX = /https?:\\/\\/(?:localhost|127\\.0\\.0\\.1)(?::\\d+)?/gi;

  const scrub = (s) => {
    if (typeof s !== 'string') return s;
    return s
      .replace(LOCALHOST_RX, PLACEHOLDER_ORIGIN)
      .replace(IP_RX, PLACEHOLDER_HOST)
      .replace(LEAK_RX, PLACEHOLDER_HOST);
  };

  const walkNode = (node) => {
    if (!node) return;
    if (node.nodeType === 3) {
      const after = scrub(node.nodeValue);
      if (after !== node.nodeValue) node.nodeValue = after;
      return;
    }
    if (node.nodeType !== 1) return;
    if (node.tagName === 'INPUT' || node.tagName === 'TEXTAREA') {
      const cur = node.value;
      const after = scrub(cur);
      if (after !== cur) node.value = after;
    }
    for (const child of node.childNodes) walkNode(child);
  };

  const start = () => {
    walkNode(document.body);
    const obs = new MutationObserver((muts) => {
      for (const m of muts) {
        if (m.type === 'characterData') walkNode(m.target);
        for (const n of m.addedNodes) walkNode(n);
      }
    });
    obs.observe(document.body, {
      childList: true,
      subtree: true,
      characterData: true,
    });
    // Direct ``.value = ...`` assignments on <input> don't fire a
    // MutationObserver notification (the value property isn't in the
    // DOM tree), so re-sweep inputs every 250ms. Cheap — fewer than
    // a dozen inputs per page.
    setInterval(() => {
      document.querySelectorAll('input, textarea').forEach((el) => {
        const after = scrub(el.value);
        if (after !== el.value) el.value = after;
      });
    }, 250);
  };
  if (document.body) start();
  else document.addEventListener('DOMContentLoaded', start);
})();
""".replace("__PLACEHOLDER_ORIGIN__", PLACEHOLDER_ORIGIN).replace("__PLACEHOLDER_HOST__", FAKE_HOST)


def _install_api_stubs(ctx: BrowserContext) -> None:
    """Stub API responses that would otherwise fail / leak.

    Several endpoints probe the real server over the network — our fake
    hosts don't resolve, so left unstubbed they repaint the cards with
    "Auth failed" / "unreachable" badges. We intercept each relevant
    endpoint and return a synthetic "everything looks great" response.
    """

    def handle_media_servers(route):
        servers = [
            {
                "id": s["id"],
                "name": s["name"],
                "type": s["type"],
                "enabled": s["enabled"],
                "url": s["url"],
                "status": "connected",
                "server_id": s.get("server_identity"),
            }
            for s in FAKE_SERVERS
        ]
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"servers": servers, "cached": False, "ttl": 30}),
        )

    def handle_test_connection(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "message": "Connected"}),
        )

    def handle_health_check(route):
        # Full-shape "everything is fine" response so the Edit modal's
        # unified "Previews readiness" panel renders green. Matches the
        # shape consumed in servers.js:1564+ (trickplay_options,
        # activation, libraries, plugin_ok flags).
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "issue_count": 0,
                    "issues": [],
                    "trickplay_options": {"ok": True},
                    "activation": {"ok": True, "summary": "Next scan"},
                    "libraries": {"ok": True},
                    "plugin": {"ok": True, "installed": True, "version": "1.0.0"},
                }
            ),
        )

    def handle_notifications(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"notifications": []}),
        )

    ctx.route("**/api/system/media-servers", handle_media_servers)
    ctx.route("**/api/servers/*/test-connection", handle_test_connection)
    ctx.route("**/api/servers/*/health-check", handle_health_check)
    ctx.route("**/api/system/notifications", handle_notifications)


def _capture_surface(page: Page, app_url: str, path: str, out_path: Path) -> None:
    page.goto(f"{app_url}{path}", wait_until="domcontentloaded", timeout=15_000)
    # networkidle is unreliable here because the app holds a long-lived
    # socket.io connection, so we wait for the DOM + a settle window
    # instead. 1.5s covers the async dashboard widgets (worker pool,
    # media-server probe, job stats) that paint after first render.
    page.wait_for_timeout(1500)
    page.screenshot(path=str(out_path), full_page=True)
    print(f"[regen_readme] wrote {out_path.name}", file=sys.stderr)


def _capture_settings_processing(page: Page, app_url: str, out_path: Path) -> None:
    """Navigate to /settings and clip to the Processing Options card.

    The full Settings page is ~4000px tall (Processing / Logging / Auth /
    Backups / About) and dwarfs the other README tiles. Clipping to the
    ``#section-processing`` element (the GPU + CPU workers + thumbnail
    + HDR + smart-caching card) yields a tile that sits comfortably next
    to the Dashboard / Servers / Automation shots.

    Uses Playwright's element-level screenshot instead of CSS crop so
    the resulting PNG is exactly the card's bounding box — no empty
    gutters, no guess-the-viewport math.
    """
    page.goto(f"{app_url}/settings", wait_until="domcontentloaded", timeout=15_000)
    # Wait for the GPU detection spinner to be replaced by real GPU
    # rows, otherwise the card captures mid-spin and the PNG is
    # non-deterministic across runs.
    page.wait_for_selector("#gpuDetecting", state="hidden", timeout=10_000)
    page.wait_for_function("() => document.getElementById('gpuConfigList').children.length > 0", timeout=5_000)
    el = page.locator("#section-processing")
    el.scroll_into_view_if_needed()
    page.wait_for_timeout(500)
    # animations='disabled' pauses any still-running CSS transitions
    # (badge pulses, collapse chevrons) so the clip is a clean still.
    el.screenshot(path=str(out_path), animations="disabled")
    print(f"[regen_readme] wrote {out_path.name}", file=sys.stderr)


def regenerate(out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    config_dir = tempfile.mkdtemp(prefix="regen_readme_")
    print(f"[regen_readme] seeding fixture in {config_dir}", file=sys.stderr)
    write_settings(config_dir)
    seeded = seed_jobs(config_dir)
    print(f"[regen_readme] seeded {seeded} jobs", file=sys.stderr)

    port = _get_free_port()
    print(f"[regen_readme] booting app on :{port}", file=sys.stderr)
    proc = _start_app(config_dir, port)
    app_url = f"http://localhost:{port}"

    try:
        # Setup is already complete via settings.json, but the helper is
        # idempotent and the login flow below assumes the setup gate is
        # closed. Safe to call either way.
        try:
            _mark_setup_complete(app_url)
        except Exception:
            # settings.json already sets setup_complete=True so the
            # endpoint may 204 or noop; ignore.
            pass
        cookie = _login_and_get_cookie(app_url)

        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                ctx = browser.new_context(viewport={"width": 1280, "height": 720})
                ctx.add_cookies([cookie])
                ctx.add_init_script(INIT_SCRIPT)
                _install_api_stubs(ctx)
                page = ctx.new_page()

                # Prime localStorage by visiting any page once; init
                # script already sets theme=dark before every page but
                # the initial load needs a rendered DOM first.
                page.goto(f"{app_url}/", wait_until="domcontentloaded", timeout=15_000)
                page.wait_for_timeout(500)

                for path, name in SURFACES:
                    _capture_surface(page, app_url, path, out_dir / f"{name}.png")

                _capture_settings_processing(page, app_url, out_dir / "settings.png")

                ctx.close()
            finally:
                browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("docs/images"),
        help="Directory to write PNGs into (default: docs/images)",
    )
    args = ap.parse_args()
    return regenerate(args.out)


if __name__ == "__main__":
    raise SystemExit(main())
