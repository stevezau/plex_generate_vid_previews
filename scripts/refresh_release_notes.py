#!/usr/bin/env python3
"""Refresh ``media_preview_generator/release_notes.json`` from GitHub Releases.

Invoked by CI in the ``build`` job before ``docker build``; the generated
JSON is COPIED into the image by the existing Dockerfile and read at
runtime by the "What's new" popup — no live GitHub API call on user
dashboards. The file is gitignored: source of truth is the GitHub
Releases page, CI is the publisher.

Local invocation for offline dev:

    python3 scripts/refresh_release_notes.py

Authentication:
    Reads ``GITHUB_TOKEN`` from env when set (CI's built-in actions token
    grants 5000 req/hr authenticated vs 60/hr anonymous, removing the
    rate-limit hazard even for shared-IP runners). Falls back to anon
    when unset — fine for one-off local runs.

Failure mode:
    Returns non-zero on HTTPError/URLError so CI can decide whether to
    fail the build or fall through. The runtime already falls back to a
    live GitHub fetch when the bundle is missing, so a CI-side failure
    just degrades to the pre-bundle UX, never a crash.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = "stevezau/media_preview_generator"
LIMIT = 30  # plenty of room for upgraders coming from very old versions
OUT_PATH = Path(__file__).resolve().parent.parent / "media_preview_generator" / "release_notes.json"


def fetch_releases() -> list[dict]:
    url = f"https://api.github.com/repos/{REPO}/releases?per_page={LIMIT}"
    headers = {
        "User-Agent": "media-preview-generator-release-notes-refresh",
        "Accept": "application/vnd.github+json",
    }
    token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if token:
        # 5000 req/hr authenticated vs 60/hr anon — important when CI
        # runners share an IP across an org (per-IP, not per-token, is
        # what GitHub rate-limits anon callers on).
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    # URL is built from the hardcoded REPO constant + LIMIT int — no
    # user input, no file:/ or arbitrary scheme exposure. nosec is
    # appropriate; stdlib is used deliberately so this script runs
    # without the project venv during release prep.
    with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310
        return json.load(resp)


def normalize(raw: list[dict]) -> list[dict]:
    """Project the GitHub Releases payload into the runtime shape.

    Drops drafts and the chunk of GH fields the runtime doesn't render
    (author, assets, mentions, etc.) so the bundled file stays small.
    """
    out: list[dict] = []
    for r in raw:
        if r.get("draft"):
            continue
        out.append(
            {
                "version": (r.get("tag_name") or "").lstrip("v"),
                "name": r.get("name") or r.get("tag_name") or "",
                "date": r.get("published_at") or "",
                "body": r.get("body") or "",
                "url": r.get("html_url") or "",
            }
        )
    return out


def main() -> int:
    try:
        raw = fetch_releases()
    except urllib.error.HTTPError as e:
        print(f"GitHub API returned {e.code}: {e.reason}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"Network error fetching releases: {e.reason}", file=sys.stderr)
        return 1
    entries = normalize(raw)
    OUT_PATH.write_text(json.dumps(entries, indent=2) + "\n")
    print(f"Wrote {len(entries)} release(s) to {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
