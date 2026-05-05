#!/usr/bin/env python3
"""Snapshot collector — drops PNGs of every key surface for PR review.

Usage:
    python tests/e2e/snapshots/collect.py --out /tmp/preview-shots/

Boots the app in a temp config dir, captures dashboard / servers /
settings / etc. in both light + dark themes at desktop + mobile
viewports, and writes them to ``--out`` for the reviewer to attach
to a PR description.

This is the helper referenced by the per-PR template in
``tests/VISUAL_REGRESSION_CHECKLIST.md`` (Option D — stay manual).
It deliberately does NOT do diffing; that was the trade-off picked
in the decision doc.

Requires: ``playwright install chromium`` (one-time setup).
"""

from __future__ import annotations

import argparse
import http.cookiejar
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

# Surfaces to capture. Path is relative to app_url; name becomes the file.
SURFACES: list[tuple[str, str]] = [
    ("/", "dashboard"),
    ("/servers", "servers"),
    ("/settings", "settings"),
    ("/automation", "automation"),
    ("/logs", "logs"),
    ("/login", "login"),
    ("/bif-viewer", "bif_viewer"),
]

VIEWPORTS: list[tuple[str, dict[str, int]]] = [
    ("desktop", {"width": 1280, "height": 720}),
    ("mobile", {"width": 390, "height": 844}),  # iPhone 14 Pro size
]

THEMES = ["light", "dark"]
TOKEN = "snapshot-collector-token"


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_port(port: int, timeout: float = 20.0) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("localhost", port)) == 0:
                return True
        time.sleep(0.1)
    return False


def _start_app(config_dir: str, port: int) -> subprocess.Popen:
    env = {
        **os.environ,
        "WEB_PORT": str(port),
        "CONFIG_DIR": config_dir,
        "WEB_AUTH_TOKEN": TOKEN,
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"from media_preview_generator.web.app import run_server; run_server(host='127.0.0.1', port={port})",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if not _wait_for_port(port):
        out, err = proc.communicate(timeout=5)
        proc.kill()
        raise RuntimeError(f"App failed to start.\nstdout: {out.decode()}\nstderr: {err.decode()}")
    return proc


def _login_and_get_cookie(app_url: str) -> dict:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    data = urllib.parse.urlencode({"token": TOKEN}).encode()
    req = urllib.request.Request(
        f"{app_url}/login",
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    opener.open(req, timeout=10)  # noqa: S310 (localhost-only)
    for c in jar:
        if c.name == "session":
            return {
                "name": "session",
                "value": c.value,
                "domain": "localhost",
                "path": "/",
                "httpOnly": True,
                "secure": False,
                "sameSite": "Lax",
            }
    raise RuntimeError("Login produced no session cookie")


def _mark_setup_complete(app_url: str) -> None:
    req = urllib.request.Request(
        f"{app_url}/api/setup/complete",
        method="POST",
        headers={"X-Auth-Token": TOKEN, "Content-Type": "application/json"},
        data=b"{}",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        if resp.status != 200:
            raise RuntimeError(f"setup/complete returned {resp.status}")


def collect(out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    config_dir = tempfile.mkdtemp(prefix="snapshot_collector_")
    port = _get_free_port()
    print(f"[collect] booting app on :{port} (config={config_dir})", file=sys.stderr)
    proc = _start_app(config_dir, port)
    app_url = f"http://localhost:{port}"
    written = 0
    try:
        _mark_setup_complete(app_url)
        cookie = _login_and_get_cookie(app_url)

        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                for vp_name, vp in VIEWPORTS:
                    for theme in THEMES:
                        ctx = browser.new_context(viewport=vp)
                        ctx.add_cookies([cookie])
                        # Force theme by cookie + localStorage; the app reads both.
                        ctx.add_init_script(
                            f"() => {{ try {{ localStorage.setItem('theme', '{theme}'); }} catch (e) {{}} }}"
                        )
                        page = ctx.new_page()
                        for path, name in SURFACES:
                            try:
                                page.goto(f"{app_url}{path}", wait_until="networkidle", timeout=10_000)
                            except Exception as exc:
                                print(f"[collect] skip {path} ({theme}/{vp_name}): {exc}", file=sys.stderr)
                                continue
                            out_path = out_dir / f"{name}_{theme}_{vp_name}.png"
                            page.screenshot(path=str(out_path), full_page=True)
                            written += 1
                            print(f"[collect] wrote {out_path.name}", file=sys.stderr)
                        ctx.close()
            finally:
                browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    print(f"[collect] {written} screenshots written to {out_dir}", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("/tmp/preview-shots"),
        help="Directory to write PNGs into (default: /tmp/preview-shots)",
    )
    args = ap.parse_args()
    return collect(args.out)


if __name__ == "__main__":
    raise SystemExit(main())
