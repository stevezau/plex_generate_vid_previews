"""Source-grep tests pinning the Job Modal Overview renderer contract.

The Overview pane (``_renderOverview`` and its sub-renderers in
``static/js/job_modal.js``) is rendered from a global ``jobs`` array
in browser JS, so unit-testing the actual DOM output would need a
jsdom harness this project doesn't run. Instead, lock the critical
string contracts via regex against the source — same idiom as
``test_retry_chain_tooltip_branching.py``.

Coverage focus: arithmetic the architecture review flagged as
high-blast-radius. The off-by-one in the retry-banner "Run X of Y is
queued" headline shipped past the first review pass — pin the exact
expression so a future "looks cleaner as ra + 2" refactor surfaces in
CI instead of in production.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MODAL_JS = REPO_ROOT / "media_preview_generator" / "web" / "static" / "js" / "job_modal.js"


@pytest.fixture(scope="module")
def modal_js() -> str:
    return MODAL_JS.read_text()


class TestRetryBannerRunOrdinalMath:
    """The retry-reason banner headline ("Run X of Y is queued") computes
    X = retry_attempt + 1 because Run 1 is the original dispatch and
    ``retry_attempt`` is 1-indexed against the upcoming retry firing
    (see ``processing/retry_queue.py:schedule`` — the chain's
    ``retry_attempt`` field stores the about-to-fire retry number, not
    the count of completed retries). Architecture review caught an
    initial off-by-one (``ra + 2``) that would have shown "Run 3 of 6
    is queued" while retry #1 was scheduled. This test locks the fix.
    """

    def test_headline_run_ordinal_is_ra_plus_one(self, modal_js: str):
        # Match the exact arithmetic expression inside the headline
        # literal. Tolerant of whitespace + comment changes around it
        # but strict about the multiplier on ``ra``.
        match = re.search(
            r"'Run\s*'\s*\+\s*\(\s*ra\s*\+\s*(\d+)\s*\)\s*\+\s*'\s+of\s+'",
            modal_js,
        )
        assert match is not None, (
            "Could not locate the retry-banner headline run-ordinal "
            "expression `'Run ' + (ra + N) + ' of '` in job_modal.js — "
            "if the headline copy was refactored, update this regex AND "
            "verify the off-by-one fix is still in place."
        )
        addend = int(match.group(1))
        assert addend == 1, (
            f"Retry-banner headline uses `ra + {addend}` for the queued-run "
            "ordinal; must be `ra + 1`. ``retry_attempt`` is the 1-indexed "
            "upcoming retry number; Run 1 is the original (retry_attempt=0), "
            "so retry #1 is Run 2 (ra=1 -> Run 2). Using `ra + 2` would "
            "show 'Run 3 of 6 is queued' for the very first retry — "
            "architecture review HIGH finding, do not revert."
        )

    def test_headline_max_runs_is_rmax_plus_one(self, modal_js: str):
        # Total-runs cap = rmax + 1 (original + N retries).
        match = re.search(
            r"'\s+of\s+'\s*\+\s*\(\s*rmax\s*\+\s*(\d+)\s*\)\s*\+\s*'\s+is queued",
            modal_js,
        )
        assert match is not None, "Could not locate the rmax expression in the banner headline."
        addend = int(match.group(1))
        assert addend == 1, (
            f"Retry-banner uses `rmax + {addend}` for the max-runs cap; must be `rmax + 1`. "
            "``retry_max_attempts`` counts retries only; the original dispatch is one extra run."
        )


class TestRetryBannerBranchGating:
    """The banner should render only for chains in active (pending /
    running) state with at least one stuck publisher. Pin the early-
    return guards so a future refactor doesn't accidentally surface
    the banner on completed/failed chains (where it would read as
    stale UI noise).
    """

    def test_renderer_returns_empty_for_non_chain(self, modal_js: str):
        # The function should early-return empty string when
        # ``isChain`` is false. The check is `if (!isChain) return '';`
        snippet = re.search(
            r"function\s+_renderOverviewReasonBanner\s*\(.*?\)\s*\{.*?if\s*\(\s*!isChain\s*\)\s*return\s*'';",
            modal_js,
            re.DOTALL,
        )
        assert snippet is not None, (
            "Retry-reason banner must early-return '' for non-chain jobs — otherwise "
            "single-dispatch failures would render a banner with no chain context. "
            "Look for the `if (!isChain) return '';` guard in _renderOverviewReasonBanner."
        )

    def test_renderer_returns_empty_for_terminal_status(self, modal_js: str):
        # Terminal-state guard — only render for pending or running.
        snippet = re.search(
            r"if\s*\(\s*status\s*!==\s*'pending'\s*&&\s*status\s*!==\s*'running'\s*\)\s*return\s*'';",
            modal_js,
        )
        assert snippet is not None, (
            "Retry-reason banner must early-return for terminal-status chains "
            "(completed / failed / cancelled). Otherwise a chain that just "
            "completed would still show 'Why this chain is still going' — "
            "stale UI noise. Look for the status-guard pattern in "
            "_renderOverviewReasonBanner."
        )


class TestOverviewTabIsDefault:
    """Operator-grade landing: Overview tab is first in the tab list,
    rendered active by default, and ``_restoreLastTab`` brings users
    back to whichever tab they last picked.
    """

    def test_overview_tab_is_first_in_template(self):
        idx_html = (REPO_ROOT / "media_preview_generator" / "web" / "templates" / "index.html").read_text()
        # Overview tab button literal must appear before logsTab and
        # filesTab in the source — Bootstrap's tab order is rendering
        # order.
        overview_pos = idx_html.find('id="overviewTab"')
        logs_pos = idx_html.find('id="logsTab"')
        files_pos = idx_html.find('id="filesTab"')
        assert overview_pos != -1, "overviewTab button missing from index.html"
        assert logs_pos != -1, "logsTab button missing from index.html"
        assert files_pos != -1, "filesTab button missing from index.html"
        assert overview_pos < logs_pos < files_pos, (
            f"Tab render order must be Overview -> Logs -> Files. "
            f"Got positions: overview={overview_pos}, logs={logs_pos}, files={files_pos}."
        )

    def test_overview_tab_has_active_class_at_render(self):
        idx_html = (REPO_ROOT / "media_preview_generator" / "web" / "templates" / "index.html").read_text()
        # The active class must be on overviewTab's button so SSR
        # renders Overview as the visible pane (then _restoreLastTab
        # can override based on localStorage).
        match = re.search(
            r'<button[^>]*class="[^"]*\bnav-link\b[^"]*\bactive\b[^"]*"[^>]*id="overviewTab"',
            idx_html,
        )
        assert match is not None, (
            "overviewTab button must carry `nav-link active` class as the SSR default. "
            "Without it, the modal opens blank for a frame before _restoreLastTab fires."
        )
