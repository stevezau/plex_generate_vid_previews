#!/usr/bin/env python3
"""Refresh ``media_preview_generator/release_notes.json`` from GitHub Releases.

Run as part of release prep, after publishing the new GitHub release page:

    python3 scripts/refresh_release_notes.py

The bundled JSON is what powers the dashboard's "What's new" modal. Shipping
it in the package means upgraders see release notes instantly with zero
network calls — no GitHub-API rate limit risk, no 5s timeout on a flaky
connection, works on LAN-only deployments. The runtime falls back to a live
GitHub fetch only when the bundle is missing (older containers, dev tree),
so forgetting to run this script before tagging just degrades to the old
behaviour, not a crash.

Anonymous GitHub API allows 60 requests/hour per IP. One call per run is
fine — no auth needed.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = "stevezau/media_preview_generator"
LIMIT = 30  # plenty of room for upgraders coming from very old versions
OUT_PATH = Path(__file__).resolve().parent.parent / "media_preview_generator" / "release_notes.json"


def fetch_releases() -> list[dict]:
    url = f"https://api.github.com/repos/{REPO}/releases?per_page={LIMIT}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "media-preview-generator-release-notes-refresh",
            "Accept": "application/vnd.github+json",
        },
    )
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
