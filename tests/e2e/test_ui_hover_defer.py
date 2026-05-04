"""TEST_AUDIT Phase 4 (subset) — UI render-contract hover-defer regression test.

Closes commit 5028fb6 + 0df1cc3 incident class: the dashboard's
``loadJobs()`` and ``updateActiveJobs()`` rebuild their tbody/container
via wholesale ``innerHTML = html`` every poll. Without the
``:hover``-aware defer guard, a user hovering the Cancel button at the
moment the poll fires loses the click — the rebuild lands between
mousedown and mouseup, and the click registers on a stale node.

The fix at app.js:1531 + app.js:1786 is:

    if (container.matches(':hover') || container.querySelector(':hover')) {
        _jobQueueUpdatePending = true;  // (or just early return for active jobs)
        return;
    }

This file pins that early-return so a refactor that drops the guard is
caught loudly. Drives the page in a real browser and verifies the
rebuild defers when hovered.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

from ._mocks import mock_dashboard_defaults


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


@pytest.mark.e2e
class TestActiveJobsHoverDefer:
    """The Active Jobs container (#activeJobsContainer) wholesale-rebuilds
    on every poll. Production guards this with a :hover check so the
    Cancel button stays click-receptive while the cursor's on it.
    """

    def test_active_jobs_render_defers_when_container_is_hovered(self, authed_page: Page, app_url: str) -> None:
        """Render with one running job → simulate hover → invoke
        ``updateActiveJobs(...)`` directly → assert the function early-returned
        WITHOUT touching the container's innerHTML.

        The verification is structural: we tag the existing DOM with a
        sentinel attribute, then trigger the update, then assert the
        sentinel is STILL there. If the function rebuilt the container,
        the sentinel would be gone (innerHTML replaces all children).
        """
        mock_dashboard_defaults(authed_page)
        authed_page.goto(f"{app_url}/")
        authed_page.wait_for_load_state("domcontentloaded")

        # Inject a known-shape running job via JS, render it, tag it
        # with a sentinel, then re-trigger render while hovered.
        result = authed_page.evaluate(
            """
            () => {
                const container = document.getElementById('activeJobsContainer');
                if (!container) return {error: 'no activeJobsContainer'};

                // First render: populate with a job so there's actual DOM.
                window.updateActiveJobs([{
                    id: 'job-test-1',
                    status: 'running',
                    library_name: 'Test Library',
                    progress: {percent: 50, processed_items: 5, total_items: 10}
                }]);

                // Mark the freshly-rendered DOM with a sentinel so we can
                // detect a wholesale rebuild (innerHTML wipes children).
                const sentinel = document.createElement('div');
                sentinel.id = 'hover-defer-sentinel';
                sentinel.style.display = 'none';
                container.appendChild(sentinel);

                // Force the container into a hover state by injecting a
                // CSS selector match. Playwright's hover() is timing-fragile;
                // production uses .matches(':hover') OR querySelector(':hover')
                // — we drive the second branch by simulating that the inner
                // sentinel is hovered. To do that without a real cursor,
                // monkeypatch matches/querySelector to lie about :hover state.
                const origMatches = container.matches.bind(container);
                container.matches = (sel) => sel === ':hover' || origMatches(sel);

                // Now re-trigger update with a DIFFERENT job. If the guard
                // works, the sentinel stays. If not, sentinel is wiped.
                window.updateActiveJobs([{
                    id: 'job-test-2',
                    status: 'running',
                    library_name: 'Different Library',
                    progress: {percent: 80, processed_items: 8, total_items: 10}
                }]);

                const sentinelStillThere = !!document.getElementById('hover-defer-sentinel');
                // Restore for sanity.
                container.matches = origMatches;
                return {sentinelStillThere};
            }
            """
        )

        assert result.get("error") is None, f"Setup failed: {result.get('error')!r}"
        assert result.get("sentinelStillThere") is True, (
            "Active Jobs container was rebuilt despite container being :hovered. "
            "Production guard at app.js:1786 must early-return when "
            "container.matches(':hover') OR container.querySelector(':hover') "
            "is true. Bug class: kill button click eaten by mid-hover DOM rebuild "
            "(commits 5028fb6, 0df1cc3)."
        )

    def test_active_jobs_render_DOES_rebuild_when_NOT_hovered(self, authed_page: Page, app_url: str) -> None:
        """Mirror test for the contract floor: when nothing is hovered, the
        rebuild MUST happen (otherwise the user never sees fresh state).

        Without this assertion, a regression that ALWAYS deferred (never
        rebuilt) would pass the hover-defer test but break the dashboard
        entirely. Pin both edges of the contract.
        """
        mock_dashboard_defaults(authed_page)
        authed_page.goto(f"{app_url}/")
        authed_page.wait_for_load_state("domcontentloaded")

        result = authed_page.evaluate(
            """
            () => {
                const container = document.getElementById('activeJobsContainer');
                if (!container) return {error: 'no activeJobsContainer'};

                // First render with one job.
                window.updateActiveJobs([{
                    id: 'job-1',
                    status: 'running',
                    library_name: 'Library A',
                    progress: {percent: 30, processed_items: 3, total_items: 10}
                }]);
                const firstHtml = container.innerHTML;

                // No hover simulation — rebuild SHOULD happen.
                window.updateActiveJobs([{
                    id: 'job-2',
                    status: 'running',
                    library_name: 'Library B',
                    progress: {percent: 60, processed_items: 6, total_items: 10}
                }]);
                const secondHtml = container.innerHTML;

                return {
                    firstContainsLibraryA: firstHtml.includes('Library A'),
                    secondContainsLibraryB: secondHtml.includes('Library B'),
                    secondDoesNotContainLibraryA: !secondHtml.includes('Library A'),
                };
            }
            """
        )

        assert result.get("error") is None
        assert result["firstContainsLibraryA"], "Initial render didn't include the seeded job"
        assert result["secondContainsLibraryB"], (
            "Second render (not hovered) must rebuild and show the new job. "
            "A regression that always-defers would fail this — UI would freeze on stale state."
        )
        assert result["secondDoesNotContainLibraryA"], (
            "Second render must REPLACE the stale Library A — a regression that appended "
            "instead of replacing would also fail this and create a growing stale-jobs list."
        )
