"""TEST_AUDIT Phase 4 — Workers panel render-contract tests.

Closes UI bug classes from recent commits:
  * 75c8da8 — Workers panel rows persist + show stable Device #N labels
  * e46e73c — Workers panel jitter (in-place updates, not wholesale rebuild)
  * 933a26d / 58829b2 — worker card current_phase rendering instead of "0.0%"
  * 1f09c3a — fallback badge / current_phase during slow reverse-lookup

Production at app.js:2087-2148 builds the row container ONCE, then for
each subsequent poll patches text/class on per-worker cached cards by
``data-worker-key`` lookup. Workers that vanish are removed by key;
new ones are appended. The contract this file pins:

1. Re-rendering with same workers DOES NOT recreate the per-worker
   <div data-worker-key="..."> nodes (they're patched in place)
2. Worker with ``current_phase`` set + ``ffmpeg_started`` false shows
   the phase text (NOT "0.0%" or "Working…")
3. Worker with ``fallback_active=true`` shows the CPU-fallback badge
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

from ._mocks import mock_dashboard_defaults


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


@pytest.mark.e2e
class TestWorkersPanelInPlaceUpdate:
    """Workers panel must do per-card in-place updates, not wholesale
    ``container.innerHTML = ...`` rebuild every poll. Without this, the
    user sees panel jitter (cards visibly recreated each second) and
    selection / hover state evaporates mid-interaction.
    """

    def test_re_render_with_same_workers_preserves_card_node_identity(self, authed_page: Page, app_url: str) -> None:
        """Render N workers → tag each card with a sentinel attribute →
        re-render with same workers (different status) → assert sentinel
        SURVIVES. If the panel rebuilt wholesale, the sentinel would be
        gone (innerHTML on the row container would replace all children).

        This is the core contract from app.js:2009-2014: "successive
        polls don't blow away DOM nodes the user might be hovering /
        selecting."
        """
        mock_dashboard_defaults(authed_page)
        authed_page.goto(f"{app_url}/")
        authed_page.wait_for_load_state("domcontentloaded")

        result = authed_page.evaluate(
            """
            () => {
                const container = document.getElementById('workerStatusContainer');
                if (!container) return {error: 'no workerStatusContainer'};

                // First render: 2 workers (one GPU, one CPU).
                const initial = [
                    {worker_id: 'gpu-0', worker_type: 'GPU', worker_name: 'GPU 0',
                     status: 'idle', progress_percent: 0},
                    {worker_id: 'cpu-0', worker_type: 'CPU', worker_name: 'CPU 0',
                     status: 'idle', progress_percent: 0},
                ];
                window.updateWorkerStatuses(initial);

                // Tag each freshly-rendered card with a sentinel.
                const cards = container.querySelectorAll('[data-worker-key]');
                if (cards.length !== 2) {
                    return {error: `expected 2 cards, got ${cards.length}`};
                }
                cards.forEach(c => c.setAttribute('data-test-sentinel', '1'));

                // Re-render with SAME worker ids but DIFFERENT status.
                // In-place update should patch text/class only — sentinel
                // attributes on the existing cards must survive.
                const updated = [
                    {worker_id: 'gpu-0', worker_type: 'GPU', worker_name: 'GPU 0',
                     status: 'processing', progress_percent: 42, current_title: 'Test',
                     ffmpeg_started: true, speed: '5.2x'},
                    {worker_id: 'cpu-0', worker_type: 'CPU', worker_name: 'CPU 0',
                     status: 'processing', progress_percent: 88, current_title: 'Test 2',
                     ffmpeg_started: true, speed: '1.1x'},
                ];
                window.updateWorkerStatuses(updated);

                // Count cards that STILL carry the sentinel.
                const survivingCards = container.querySelectorAll('[data-worker-key][data-test-sentinel="1"]');
                return {
                    initialCount: cards.length,
                    survivingCount: survivingCards.length,
                };
            }
            """
        )

        assert result.get("error") is None, f"Setup failed: {result.get('error')!r}"
        assert result["initialCount"] == 2, f"Initial render count wrong: {result['initialCount']}"
        assert result["survivingCount"] == 2, (
            f"In-place update must preserve card node identity; "
            f"got {result['survivingCount']} of {result['initialCount']} cards still tagged. "
            f"A regression that wholesale-rebuilds the container (innerHTML = ...) would "
            f"strip the sentinel, jitter the panel, and destroy any user selection / hover "
            f"state mid-interaction (commits e46e73c, 75c8da8)."
        )

    def test_vanished_worker_card_is_removed_by_key(self, authed_page: Page, app_url: str) -> None:
        """Worker that disappears from the snapshot (job ended, pool resize)
        is removed from the panel. App.js:2144-2148 walks row.children and
        removes any whose data-worker-key isn't in the current snapshot.

        Pin this so a regression that always-keeps-cards leaves stale rows
        forever; or one that always-rebuilds breaks the in-place contract
        above.
        """
        mock_dashboard_defaults(authed_page)
        authed_page.goto(f"{app_url}/")
        authed_page.wait_for_load_state("domcontentloaded")

        result = authed_page.evaluate(
            """
            () => {
                const container = document.getElementById('workerStatusContainer');
                if (!container) return {error: 'no workerStatusContainer'};

                window.updateWorkerStatuses([
                    {worker_id: 'a', worker_type: 'GPU', worker_name: 'A', status: 'idle', progress_percent: 0},
                    {worker_id: 'b', worker_type: 'GPU', worker_name: 'B', status: 'idle', progress_percent: 0},
                    {worker_id: 'c', worker_type: 'GPU', worker_name: 'C', status: 'idle', progress_percent: 0},
                ]);
                const beforeCount = container.querySelectorAll('[data-worker-key]').length;

                // Re-render with only 'a' and 'c' — 'b' must be removed.
                window.updateWorkerStatuses([
                    {worker_id: 'a', worker_type: 'GPU', worker_name: 'A', status: 'idle', progress_percent: 0},
                    {worker_id: 'c', worker_type: 'GPU', worker_name: 'C', status: 'idle', progress_percent: 0},
                ]);
                const afterCount = container.querySelectorAll('[data-worker-key]').length;
                const hasB = !!container.querySelector('[data-worker-key$="_b"]');
                return {beforeCount, afterCount, hasB};
            }
            """
        )

        assert result.get("error") is None
        assert result["beforeCount"] == 3, f"Initial render: expected 3 cards, got {result['beforeCount']}"
        assert result["afterCount"] == 2, (
            f"After removing worker 'b': expected 2 cards, got {result['afterCount']}. "
            f"Stale cards left behind would mislead the user about pool size."
        )
        assert result["hasB"] is False, "Worker 'b' card was not removed — vanished workers must be cleaned up"


@pytest.mark.e2e
class TestWorkerCardPhaseRendering:
    """Worker card with ``current_phase`` set + ``ffmpeg_started=false``
    must render the phase string (e.g. "Resolving item id on Jellyfin…")
    INSTEAD of the misleading "0.0% / 0.0x". Production at app.js:2263-2271
    swaps the percent label for the phase text in this state.

    Closes commit 933a26d (worker progress stayed at 0.0% during pre-FFmpeg
    phases) + 58829b2 (real sub-phase string in worker card).
    """

    def test_pre_ffmpeg_phase_renders_phase_text_not_zero_percent(self, authed_page: Page, app_url: str) -> None:
        mock_dashboard_defaults(authed_page)
        authed_page.goto(f"{app_url}/")
        authed_page.wait_for_load_state("domcontentloaded")

        result = authed_page.evaluate(
            """
            () => {
                window.updateWorkerStatuses([{
                    worker_id: 'gpu-0', worker_type: 'GPU', worker_name: 'GPU 0',
                    status: 'processing',
                    current_title: 'Some movie',
                    current_phase: 'Resolving item id on Jellyfin…',
                    ffmpeg_started: false,
                    progress_percent: 0,
                    speed: null,
                }]);
                const card = document.querySelector('[data-worker-key$="_gpu-0"]');
                if (!card) return {error: 'no card rendered'};
                const percent = card.querySelector('[data-percent]');
                const speed = card.querySelector('[data-speed]');
                return {
                    percentText: percent ? percent.textContent : null,
                    speedDisplay: speed ? speed.style.display : null,
                };
            }
            """
        )

        assert result.get("error") is None, f"Setup failed: {result.get('error')!r}"
        assert result["percentText"] == "Resolving item id on Jellyfin…", (
            f"Pre-FFmpeg phase must render the phase text in place of the percent label; "
            f"got {result['percentText']!r}. The misleading '0.0%' is what the user reported "
            f"as 'worker is hung' (commit 933a26d) — this rendering distinguishes the two."
        )
        # Speed chip is hidden during pre-FFmpeg phase (no meaningful value).
        assert result["speedDisplay"] == "none", (
            f"Speed chip must be hidden during pre-FFmpeg phase; got display={result['speedDisplay']!r}"
        )

    def test_ffmpeg_started_phase_shows_percent_and_speed_normally(self, authed_page: Page, app_url: str) -> None:
        """Mirror test for the contract floor: when FFmpeg HAS started,
        percent + speed render normally (NOT phase text). Without this,
        a regression that always-shows-phase would mask real FFmpeg
        progress with stale phase text.
        """
        mock_dashboard_defaults(authed_page)
        authed_page.goto(f"{app_url}/")
        authed_page.wait_for_load_state("domcontentloaded")

        result = authed_page.evaluate(
            """
            () => {
                window.updateWorkerStatuses([{
                    worker_id: 'gpu-0', worker_type: 'GPU', worker_name: 'GPU 0',
                    status: 'processing',
                    current_title: 'Some movie',
                    current_phase: 'Resolving item id on Jellyfin…',  // would be stale
                    ffmpeg_started: true,
                    progress_percent: 42.5,
                    speed: '5.2x',
                }]);
                const card = document.querySelector('[data-worker-key$="_gpu-0"]');
                if (!card) return {error: 'no card rendered'};
                const percent = card.querySelector('[data-percent]');
                const speed = card.querySelector('[data-speed]');
                return {
                    percentText: percent ? percent.textContent : null,
                    speedText: speed ? speed.textContent : null,
                    speedDisplay: speed ? speed.style.display : null,
                };
            }
            """
        )

        assert result.get("error") is None
        assert result["percentText"] == "42.5%", (
            f"FFmpeg-started worker must show percent (NOT phase); got {result['percentText']!r}"
        )
        assert result["speedText"] == "5.2x", f"FFmpeg-started worker must show speed; got {result['speedText']!r}"
        assert result["speedDisplay"] != "none", (
            f"Speed chip must be visible during FFmpeg phase; got display={result['speedDisplay']!r}"
        )

    def test_fallback_active_renders_cpu_fallback_badge(self, authed_page: Page, app_url: str) -> None:
        """Worker mid-CPU-fallback must show the warning badge so an op
        scanning the panel can spot it. App.js:2199-2208 toggles the
        d-none class on data-fallback-badge based on worker.fallback_active.
        """
        mock_dashboard_defaults(authed_page)
        authed_page.goto(f"{app_url}/")
        authed_page.wait_for_load_state("domcontentloaded")

        result = authed_page.evaluate(
            """
            () => {
                window.updateWorkerStatuses([{
                    worker_id: 'gpu-0', worker_type: 'GPU', worker_name: 'GPU 0',
                    status: 'processing',
                    current_title: 'HEVC movie',
                    fallback_active: true,
                    fallback_reason: 'GPU rejected HEVC; retrying on CPU',
                    ffmpeg_started: true,
                    progress_percent: 12,
                }]);
                const card = document.querySelector('[data-worker-key$="_gpu-0"]');
                if (!card) return {error: 'no card rendered'};
                const badge = card.querySelector('[data-fallback-badge]');
                const note = card.querySelector('[data-fallback-note]');
                return {
                    badgeHidden: badge ? badge.classList.contains('d-none') : null,
                    noteHidden: note ? note.classList.contains('d-none') : null,
                    noteText: note ? note.textContent.trim() : null,
                };
            }
            """
        )

        assert result.get("error") is None
        assert result["badgeHidden"] is False, (
            f"CPU-fallback badge must be visible (d-none REMOVED) when fallback_active=true; "
            f"got d-none={result['badgeHidden']!r}"
        )
        assert result["noteHidden"] is False, "Fallback reason note must be visible when present"
        assert result["noteText"] and "HEVC" in result["noteText"], (
            f"Fallback note must include the reason text so op can diagnose; got {result['noteText']!r}"
        )
