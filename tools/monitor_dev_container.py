"""Scan the live container's persistent log for the production-bug patterns
the assistant was eyeballing during the May 2026 audit chain.

Patterns watched (one entry → one finding):

* ``"Internal bookkeeping bug"`` warnings — failure_scope coverage gaps
  (commits 75588e2, a48bca4 closed two; this catches a third).
* Resolve times >5s on EmbyTest / JellyTest — Pass-0 short-circuit
  regression (perf #44) or Jellyfin overload-cascade (#51) recurring.
* ``Dispatch complete`` lines with ``failed>0`` — any FAILED publishes.
* ``Media Preview Bridge ResolvePath unreachable`` warnings — the new
  diagnostic line from the #51 fix; firing means Jellyfin was
  overloaded and the fix correctly skipped the wasted second timeout.
  Useful for sizing the upstream-Jellyfin issue.
* ``ERROR`` / ``WARNING`` lines that aren't the pre-existing
  ``schedules.json`` permission noise.

Usage
-----

::

    # Default: last 30 min, exit non-zero on findings.
    python tools/monitor_dev_container.py

    # Wider window (last 6h) and human-readable output.
    python tools/monitor_dev_container.py --since 6h --format text

    # Cron-friendly: write JSON to a file, exit 0 always.
    python tools/monitor_dev_container.py --format json --no-exit-code \\
        > /var/log/plex_previews_monitor.json

    # Different container name.
    python tools/monitor_dev_container.py --container my-previews

Default container is ``plex-generate-previews`` (the dev box convention).

Exit codes
----------

* ``0`` — no findings.
* ``1`` — findings present (any severity).
* ``2`` — script error (e.g. docker not available, container missing).

Wire this into anything: cron + email-on-non-zero-exit, GitHub Actions
matrix, a systemd timer, a Discord webhook, or just tail an output file.
The script is destination-agnostic on purpose.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

DEFAULT_CONTAINER = "plex-generate-previews"
DEFAULT_LOG_PATH = "/config/logs/app.log"

# Slow-resolve threshold. The Pass-0 short-circuit (perf #44) and the
# Jellyfin timeout-cascade fix (#51) together bring all reverse-lookups
# to <2s under normal load. >5s = something regressed or the upstream
# server is overloaded; either way it's a finding.
SLOW_RESOLVE_S = 5.0

# Match exactly the slow-resolve log shape the SUT emits:
#   ``'<basename>' not found on <ServerName> (XX.Xs)``
SLOW_RESOLVE_RE = re.compile(r"not found on \S+? \((\d+(?:\.\d+)?)s\)")

FAILED_DISPATCH_RE = re.compile(r"failed=(\d+)")


@dataclass
class Finding:
    severity: str  # 'error' | 'warning' | 'info'
    pattern: str  # which check fired
    timestamp: str  # ISO8601 from the log line
    message: str  # the raw log message (truncated)


def _parse_since(arg: str) -> timedelta:
    """Parse a ``30m`` / ``6h`` / ``2d`` window into a timedelta."""
    arg = arg.strip().lower()
    if not arg:
        raise ValueError("--since must be non-empty")
    suffix = arg[-1]
    try:
        n = int(arg[:-1])
    except ValueError as exc:
        raise ValueError(f"--since must be like '30m' / '6h' / '2d', got {arg!r}") from exc
    if suffix == "m":
        return timedelta(minutes=n)
    if suffix == "h":
        return timedelta(hours=n)
    if suffix == "d":
        return timedelta(days=n)
    raise ValueError(f"--since suffix must be m/h/d, got {arg!r}")


def _read_log_lines(container: str, log_path: str, tail_lines: int) -> Iterable[dict]:
    """Stream JSON-decoded log records from the container's persistent log.

    Reads from inside the container via ``docker exec`` so we get the
    persistent loguru log (rotated + retained) instead of the
    container's stdout buffer (which flushes on container restart).

    Loguru rotates aggressively — the active ``app.log`` typically
    holds <1h of records on this dev box. To honour ``--since 24h``
    we also walk the rotated ``app.<timestamp>.log.gz`` archives in
    the same directory and zcat their contents. The shell snippet
    runs entirely inside the container in one ``docker exec`` so we
    don't pay multiple round-trips.
    """
    if not shutil.which("docker"):
        print("ERROR: docker CLI not found on PATH", file=sys.stderr)
        sys.exit(2)
    log_dir = log_path.rsplit("/", 1)[0]
    log_name = log_path.rsplit("/", 1)[-1]
    # Loguru rotates ``app.log`` to ``app.<timestamp>.log.gz`` (the
    # ``.log`` suffix is stripped before the timestamp is inserted).
    # Glob must match THAT shape, not ``app.log.*.log.gz``.
    archive_stem = log_name[:-4] if log_name.endswith(".log") else log_name
    archive_glob = f"{archive_stem}.*.log.gz"
    # Concatenate every archive (oldest-first via ``sort``) followed
    # by the active log. ``2>/dev/null`` so missing archives don't
    # error; loguru's archive names sort correctly chronologically.
    inner_cmd = (
        f"cd {log_dir!s} && "
        f'( for f in $(ls -1 {archive_glob} 2>/dev/null | sort) ; do zcat "$f" ; done ; '
        f"tail -n {tail_lines} {log_name} ) 2>/dev/null"
    )
    proc = subprocess.run(
        ["docker", "exec", container, "sh", "-c", inner_cmd],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        print(
            f"ERROR: 'docker exec {container}' failed (rc={proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}",
            file=sys.stderr,
        )
        sys.exit(2)
    for raw in proc.stdout.splitlines():
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            # Loguru's structured handler writes one JSON object per
            # line. Anything else is a logging artefact (banner,
            # stack-frame continuation) we skip.
            continue
        yield record


def _ts_to_dt(ts: str) -> datetime | None:
    """Parse a loguru timestamp ``2026-05-06 13:00:00.000`` to a UTC dt."""
    try:
        # The container's log is in local time per the user's TZ
        # mount; for ``--since`` purposes we treat both sides as
        # naive-local and compare. Conversion to UTC isn't critical
        # for a "last N minutes" window.
        return datetime.strptime(ts.split(".")[0], "%Y-%m-%d %H:%M:%S")
    except (ValueError, AttributeError):
        return None


def _scan(records: Iterable[dict], cutoff: datetime | None) -> list[Finding]:
    """Apply the bug-pattern checks to an iterable of log records."""
    findings: list[Finding] = []
    for r in records:
        ts_str = str(r.get("ts", ""))
        if cutoff is not None:
            ts_dt = _ts_to_dt(ts_str)
            if ts_dt is not None and ts_dt < cutoff:
                continue
        msg = str(r.get("msg", ""))
        lvl = str(r.get("level", "")).upper()

        # Pattern 1: failure_scope gaps
        if "Internal bookkeeping bug" in msg:
            findings.append(
                Finding(
                    severity="error",
                    pattern="bookkeeping_bug",
                    timestamp=ts_str,
                    message=msg[:200],
                )
            )
            continue

        # Pattern 2: slow resolves (Pass-0 / Jellyfin-timeout regression)
        m = SLOW_RESOLVE_RE.search(msg)
        if m:
            try:
                seconds = float(m.group(1))
                if seconds >= SLOW_RESOLVE_S:
                    findings.append(
                        Finding(
                            severity="warning",
                            pattern="slow_resolve",
                            timestamp=ts_str,
                            message=msg[:200],
                        )
                    )
                    continue
            except ValueError:
                pass

        # Pattern 3: FAILED publishes
        if "Dispatch complete" in msg:
            fm = FAILED_DISPATCH_RE.search(msg)
            if fm and int(fm.group(1)) > 0:
                findings.append(
                    Finding(
                        severity="warning",
                        pattern="failed_publish",
                        timestamp=ts_str,
                        message=msg[:200],
                    )
                )
                continue

        # Pattern 4: Jellyfin overload-cascade fix kicked in
        if "Media Preview Bridge ResolvePath unreachable" in msg:
            findings.append(
                Finding(
                    severity="info",
                    pattern="jellyfin_overload_skipped",
                    timestamp=ts_str,
                    message=msg[:200],
                )
            )
            continue

        # Pattern 5: any other ERROR / non-noise WARNING
        if lvl in ("ERROR", "CRITICAL"):
            findings.append(
                Finding(
                    severity="error",
                    pattern="unknown_error",
                    timestamp=ts_str,
                    message=msg[:200],
                )
            )
            continue
        if lvl == "WARNING":
            # Filter the pre-existing schedules.json permission noise —
            # it's known + documented + unrelated to dispatch logic.
            if "schedules.json" in msg:
                continue
            findings.append(
                Finding(
                    severity="warning",
                    pattern="unknown_warning",
                    timestamp=ts_str,
                    message=msg[:200],
                )
            )
    return findings


def _format_text(findings: list[Finding], window: str) -> str:
    if not findings:
        return f"OK — no findings in the last {window}.\n"
    by_pattern: dict[str, list[Finding]] = {}
    for f in findings:
        by_pattern.setdefault(f.pattern, []).append(f)
    lines = [f"FINDINGS in the last {window} ({len(findings)} total):\n"]
    for pattern, group in sorted(by_pattern.items()):
        lines.append(f"  [{group[0].severity.upper()}] {pattern} × {len(group)}")
        for f in group[:5]:  # cap per-pattern listing
            lines.append(f"    {f.timestamp} | {f.message[:140]}")
        if len(group) > 5:
            lines.append(f"    ... and {len(group) - 5} more")
        lines.append("")
    return "\n".join(lines)


def _format_json(findings: list[Finding], window: str) -> str:
    payload: dict[str, Any] = {
        "window": window,
        "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "findings_count": len(findings),
        "findings": [asdict(f) for f in findings],
    }
    return json.dumps(payload, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", default=DEFAULT_CONTAINER, help="container name (default: %(default)s)")
    parser.add_argument("--log-path", default=DEFAULT_LOG_PATH, help="log path inside container (default: %(default)s)")
    parser.add_argument("--since", default="30m", help="time window like '30m' / '6h' / '2d' (default: %(default)s)")
    parser.add_argument(
        "--tail-lines",
        type=int,
        default=10000,
        help="upper bound on tail lines to read (default: %(default)s)",
    )
    parser.add_argument(
        "--format", choices=("text", "json"), default="text", help="output format (default: %(default)s)"
    )
    parser.add_argument(
        "--no-exit-code",
        action="store_true",
        help="always exit 0 (cron-friendly when wrapping with own alerting)",
    )
    args = parser.parse_args()

    # Resolve the window cutoff.
    delta = _parse_since(args.since)
    cutoff = datetime.now() - delta

    t0 = time.perf_counter()
    records = list(_read_log_lines(args.container, args.log_path, args.tail_lines))
    findings = _scan(records, cutoff)
    elapsed = time.perf_counter() - t0

    if args.format == "json":
        out = _format_json(findings, args.since)
    else:
        out = _format_text(findings, args.since)
        out += f"\n(scanned {len(records)} records in {elapsed:.2f}s)\n"
    sys.stdout.write(out)

    if args.no_exit_code:
        return 0
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
