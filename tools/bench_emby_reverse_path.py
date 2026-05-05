"""Compare current vs proposed Emby reverse-path-to-item-id strategies.

Runs each path through:
  A) Current strategy — searchTerm=<full filename stem>, ParentId scoped.
     Falls back to enumerate without searchTerm when zero matches.
  B) Proposed strategy — extract show name from path components,
     NameStartsWith=<show> + ParentId=<lib>, then per-Series enumerate
     and locally match by basename + path tail.

Reports: per-path timing for each strategy, final item id (or None),
and whether they agree. No code is patched — read-only benchmark.

Usage:
    EMBY_URL=http://your-emby:8096 \\
    EMBY_TOKEN=<your-x-emby-token> \\
    EMBY_USER_ID=<your-user-id> \\
        python tools/bench_emby_reverse_path.py

Edit ``LIB_PREFIX_MAP`` and ``PATHS`` to match your library layout.
"""

import os
import re
import sys
import time

import requests

EMBY = os.environ.get("EMBY_URL", "")
TOKEN = os.environ.get("EMBY_TOKEN", "")
USER_ID = os.environ.get("EMBY_USER_ID", "")
if not (EMBY and TOKEN and USER_ID):
    sys.stderr.write(
        "Set EMBY_URL, EMBY_TOKEN, and EMBY_USER_ID before running.\n"
        "  EMBY_URL=http://emby:8096 EMBY_TOKEN=... EMBY_USER_ID=... \\\n"
        "      python tools/bench_emby_reverse_path.py\n"
    )
    sys.exit(2)

# Library remote_path prefix → library Id. Replace with your own.
# Inspect via ``GET /Users/<id>/Views`` to enumerate libraries; the
# ``Path`` of any item in each library tells you the prefix.
LIB_PREFIX_MAP = [
    # ("/your/tv/path", "7"),
    # ("/your/movies/path", "3"),
]

# Paths to benchmark. Mix in:
#   * webhook-style paths the dispatcher would dispatch to this server
#   * known-present items (run /Users/<id>/Items?ParentId=<lib>&Limit=5 to grab a few)
#   * path-mapping mismatches (a /data/... path vs server's /mnt/...)
# so the bench covers the negative, positive, and short-circuit cases.
PATHS = [
    # "/your/tv/Some Show (2024) [imdb-x]/Season 01/Some Show - S01E01 - Title.mkv",
    # "/your/movies/Some Movie (2020)/Some Movie (2020) [imdb-y][Bluray-1080p].mkv",
]


def find_owning_library_id(path: str) -> str | None:
    p = path.replace("\\", "/").rstrip("/")
    for prefix, lib_id in LIB_PREFIX_MAP:
        n = prefix.rstrip("/")
        if p == n or p.startswith(n + "/"):
            return lib_id
    return None


def extract_show_name(path: str) -> str:
    """Pull the show/movie folder name, strip year/brackets/leading article.

    Mirror of EmbyApiClient._extract_title_prefix so the bench predicts
    real-code behaviour. NameStartsWith matches SortName, which has
    leading articles stripped (e.g. "The 'Burbs" → SortName "'Burbs").
    """
    parts = path.replace("\\", "/").split("/")
    candidate = ""
    for i, comp in enumerate(parts):
        if re.match(r"^season\b", comp, re.I):
            if i > 0:
                candidate = parts[i - 1]
            break
    if not candidate and len(parts) >= 2:
        candidate = parts[-2]
    if not candidate:
        return ""
    cleaned = re.sub(r"\s*\([0-9]{4}\)\s*", " ", candidate)
    cleaned = re.sub(r"\s*\[[^]]+\]\s*", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r"^(?:the|a|an)\s+", "", cleaned, count=1, flags=re.IGNORECASE).strip()
    return cleaned if len(cleaned) >= 2 else ""


def _get(endpoint: str, params: dict, timeout: float = 90.0) -> tuple[float, dict]:
    url = f"{EMBY}/Users/{USER_ID}/{endpoint.lstrip('/')}"
    headers = {"X-Emby-Token": TOKEN}
    t0 = time.monotonic()
    r = requests.get(url, params=params, headers=headers, timeout=timeout)
    elapsed = time.monotonic() - t0
    r.raise_for_status()
    return elapsed, r.json()


def match_in_items(items: list, basename: str, target_tail: str) -> str | None:
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        path = str(raw.get("Path") or "")
        if not path:
            continue
        if os.path.basename(path) == basename and path.replace("\\", "/").endswith(target_tail):
            iid = str(raw.get("Id") or "")
            if iid:
                return iid
    return None


def strategy_current(path: str) -> dict:
    """searchTerm=<stem>, ParentId scoping; fall back to enumerate."""
    basename = os.path.basename(path)
    stem = os.path.splitext(basename)[0]
    target_tail = "/".join(path.rstrip("/").split("/")[-2:])
    parent_id = find_owning_library_id(path)
    if parent_id is None:
        return {"id": None, "elapsed": 0.0, "passes": ["library-scope-skip"]}

    total = 0.0
    passes = []
    # Pass 1
    p1 = {
        "searchTerm": stem,
        "Recursive": "true",
        "IncludeItemTypes": "Movie,Episode",
        "Fields": "Path",
        "Limit": 50,
        "ParentId": parent_id,
    }
    try:
        e1, body1 = _get("Items", p1)
        total += e1
        hit = match_in_items(body1.get("Items"), basename, target_tail)
        passes.append(
            {
                "name": "pass1-searchTerm",
                "elapsed_s": round(e1, 3),
                "items": len(body1.get("Items") or []),
                "hit": bool(hit),
            }
        )
        if hit:
            return {"id": hit, "elapsed": round(total, 3), "passes": passes}
    except Exception as exc:
        passes.append({"name": "pass1-searchTerm", "error": str(exc)})

    # Pass 2 — enumerate
    p2 = {
        "Recursive": "true",
        "IncludeItemTypes": "Movie,Episode",
        "Fields": "Path",
        "Limit": 1000,
        "ParentId": parent_id,
    }
    try:
        e2, body2 = _get("Items", p2)
        total += e2
        hit = match_in_items(body2.get("Items"), basename, target_tail)
        passes.append(
            {
                "name": "pass2-enumerate",
                "elapsed_s": round(e2, 3),
                "items": len(body2.get("Items") or []),
                "hit": bool(hit),
            }
        )
        return {"id": hit, "elapsed": round(total, 3), "passes": passes}
    except Exception as exc:
        passes.append({"name": "pass2-enumerate", "error": str(exc)})
        return {"id": None, "elapsed": round(total, 3), "passes": passes}


def strategy_proposed(path: str) -> dict:
    """NameStartsWith=<show> + ParentId=<lib>, then per-Series enumerate."""
    basename = os.path.basename(path)
    target_tail = "/".join(path.rstrip("/").split("/")[-2:])
    parent_id = find_owning_library_id(path)
    if parent_id is None:
        return {"id": None, "elapsed": 0.0, "passes": ["library-scope-skip"]}

    show = extract_show_name(path)
    if not show:
        return {"id": None, "elapsed": 0.0, "passes": ["no-show-name"]}

    total = 0.0
    passes = []
    # Step 1: find Series (TV) or candidate Movie titles by NameStartsWith.
    is_tv = parent_id == "7"
    s1 = {
        "Recursive": "true",
        "IncludeItemTypes": "Series" if is_tv else "Movie",
        "NameStartsWith": show,
        "Fields": "Path",
        "Limit": 50,
        "ParentId": parent_id,
    }
    try:
        e1, body1 = _get("Items", s1)
        total += e1
        candidates = body1.get("Items") or []
        passes.append(
            {"name": "step1-NameStartsWith", "show": show, "elapsed_s": round(e1, 3), "items": len(candidates)}
        )
    except Exception as exc:
        passes.append({"name": "step1-NameStartsWith", "error": str(exc)})
        return {"id": None, "elapsed": round(total, 3), "passes": passes}

    if not candidates:
        return {"id": None, "elapsed": round(total, 3), "passes": passes}

    # Step 2: enumerate within each candidate. For movies, the candidate
    # IS the item — try matching directly first.
    if not is_tv:
        hit = match_in_items(candidates, basename, target_tail)
        passes.append({"name": "step2-movie-direct-match", "hit": bool(hit)})
        return {"id": hit, "elapsed": round(total, 3), "passes": passes}

    # TV: each candidate is a Series — enumerate its episodes.
    for series in candidates:
        sid = str(series.get("Id") or "")
        if not sid:
            continue
        s2 = {
            "Recursive": "true",
            "IncludeItemTypes": "Episode",
            "Fields": "Path",
            "Limit": 500,
            "ParentId": sid,
        }
        try:
            e2, body2 = _get("Items", s2)
            total += e2
            eps = body2.get("Items") or []
            hit = match_in_items(eps, basename, target_tail)
            passes.append(
                {
                    "name": "step2-series-enum",
                    "series_id": sid,
                    "elapsed_s": round(e2, 3),
                    "items": len(eps),
                    "hit": bool(hit),
                }
            )
            if hit:
                return {"id": hit, "elapsed": round(total, 3), "passes": passes}
        except Exception as exc:
            passes.append({"name": "step2-series-enum", "series_id": sid, "error": str(exc)})

    return {"id": None, "elapsed": round(total, 3), "passes": passes}


def main():
    import sys

    print(f"{'PATH':<90}  {'CUR(s)':>8}  {'NEW(s)':>8}  {'speedup':>8}  agree?  ids", flush=True)
    print("-" * 160, flush=True)
    cur_total = 0.0
    new_total = 0.0
    misses = 0
    for path in PATHS:
        a = strategy_current(path)
        b = strategy_proposed(path)
        cur_total += a["elapsed"]
        new_total += b["elapsed"]
        agree = (a["id"] or None) == (b["id"] or None)
        if not agree:
            misses += 1
        speedup = (a["elapsed"] / b["elapsed"]) if b["elapsed"] > 0 else float("inf")
        speedup_s = f"{speedup:.0f}x" if speedup != float("inf") else "(∞)"
        disp = path.replace("/data_16tb", "…").replace("/data", "/d")
        if len(disp) > 88:
            disp = disp[:85] + "…"
        print(
            f"{disp:<90}  {a['elapsed']:>8.3f}  {b['elapsed']:>8.3f}  {speedup_s:>8}  {'✓' if agree else '✗'}     cur={a['id']} new={b['id']}",
            flush=True,
        )
    print("-" * 160, flush=True)
    overall = (cur_total / new_total) if new_total > 0 else float("inf")
    print(
        f"TOTAL                                                                                      {cur_total:>8.3f}  {new_total:>8.3f}  {overall:>7.0f}x",
        flush=True,
    )
    print(f"MISSES: {misses}/{len(PATHS)}", flush=True)
    sys.exit(1 if misses else 0)


if __name__ == "__main__":
    main()
