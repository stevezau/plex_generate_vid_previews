"""
Static guards on web/static/js/app.js.

These regressions exist because SocketIO `connect`/`disconnect` events
fire on every page (settings, servers, automation, logs, bif-viewer),
not only the dashboard. The shared loader functions called from those
handlers — ``loadJobs``/``loadJobStats``/``loadWorkerStatuses`` — must
bail when their target DOM is absent, otherwise reconnect throws
``TypeError: Cannot set properties of null`` and the user sees a noisy
stack trace in the console.

Pure string-match guards rather than a JS test runner because the
project has no JS test infra yet — fast, and the bug shape (null deref
on a known element ID) is exactly what these tests catch.
"""

from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parent.parent / "media_preview_generator" / "web" / "static" / "js" / "app.js"


@pytest.fixture(scope="module")
def app_js() -> str:
    return APP_JS.read_text(encoding="utf-8")


class TestSocketReconnectNullGuards:
    def test_update_job_queue_guards_missing_tbody(self, app_js):
        """``updateJobQueue`` must early-return when ``#jobQueue`` is absent."""
        snippet = app_js.split("function updateJobQueue()", 1)[1].split("\n}", 1)[0]
        assert "if (!tbody)" in snippet, (
            "updateJobQueue must short-circuit when document.getElementById('jobQueue') is null. "
            "Without the guard, SocketIO reconnect from /settings or /servers crashes with "
            "'Cannot set properties of null (setting innerHTML)' on the first tbody.innerHTML write."
        )

    def test_update_worker_statuses_guards_missing_container(self, app_js):
        """``updateWorkerStatuses`` must early-return when ``#workerStatusContainer`` is absent."""
        snippet = app_js.split("function updateWorkerStatuses(", 1)[1].split("\n}", 1)[0]
        assert "if (!container)" in snippet, (
            "updateWorkerStatuses must short-circuit when document.getElementById"
            "('workerStatusContainer') is null. Same reconnect-from-non-dashboard crash as updateJobQueue."
        )

    def test_load_job_stats_guards_missing_stat_elements(self, app_js):
        """``loadJobStats`` must early-return when stat elements are absent."""
        snippet = app_js.split("async function loadJobStats()", 1)[1].split("\n}", 1)[0]
        assert "getElementById('statPending')" in snippet and "return" in snippet, (
            "loadJobStats must short-circuit when document.getElementById('statPending') is null. "
            "Same reconnect-from-non-dashboard crash as updateJobQueue."
        )
