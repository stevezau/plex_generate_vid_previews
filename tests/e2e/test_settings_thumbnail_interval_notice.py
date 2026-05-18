"""E2E tests for the issue-#238 slow-path notice on /settings.

When the user picks a Thumbnail Interval below the 10s default, an inline
alert appears explaining that some videos will take longer to process —
in plain English, no FFmpeg jargon.  These tests verify the show/hide
toggling and the value-substitution into the visible text.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from ._mocks import (
    mock_settings_backups,
    mock_settings_get,
    mock_setup_status,
    mock_system_status,
)


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


def _settings_page(authed_page: Page, app_url: str, *, interval: int) -> Page:
    """Open /settings with a mocked thumbnail_interval value."""
    mock_settings_get(authed_page, settings=_settings_payload(interval))
    mock_setup_status(authed_page, complete=True, plex_authenticated=True)
    mock_system_status(authed_page)
    mock_settings_backups(authed_page)
    authed_page.goto(f"{app_url}/settings")
    authed_page.wait_for_load_state("domcontentloaded")
    # Settings load is async (loadSettings()); wait for it to populate.
    expect(authed_page.locator("#thumbnailInterval")).to_have_value(str(interval))
    return authed_page


def _settings_payload(interval: int) -> dict:
    return {
        "cpu_threads": 1,
        "thumbnail_interval": interval,
        "thumbnail_quality": 4,
        "tonemap_algorithm": "hable",
        "log_level": "INFO",
        "log_rotation_size": "10 MB",
        "log_retention_count": 5,
        "job_history_days": 30,
        "gpu_config": [],
        "path_mappings": [],
        "exclude_paths": [],
        "media_servers": [],
        "plex_verify_ssl": True,
    }


@pytest.mark.e2e
class TestThumbnailIntervalNotice:
    def test_notice_visible_at_low_interval_on_load(self, authed_page: Page, app_url: str) -> None:
        """Page loaded with saved value <10 must render the slow-path notice."""
        page = _settings_page(authed_page, app_url, interval=2)
        notice = page.locator("#thumbnailIntervalSlowPathNotice")
        expect(notice).to_be_visible()
        # The exact "2 seconds" value is interpolated into the alert text.
        expect(page.locator("#thumbnailIntervalNoticeValue")).to_have_text("2")

    def test_notice_hidden_at_default_interval_on_load(self, authed_page: Page, app_url: str) -> None:
        """Page loaded with the 10s default must NOT show the notice."""
        page = _settings_page(authed_page, app_url, interval=10)
        expect(page.locator("#thumbnailIntervalSlowPathNotice")).to_be_hidden()

    def test_notice_toggles_when_user_changes_value(self, authed_page: Page, app_url: str) -> None:
        """Typing a value <10 shows the notice; restoring 10 hides it again."""
        page = _settings_page(authed_page, app_url, interval=10)
        inp = page.locator("#thumbnailInterval")
        notice = page.locator("#thumbnailIntervalSlowPathNotice")

        expect(notice).to_be_hidden()

        inp.fill("5")
        # `fill` dispatches `input`, which our handler is wired to.
        expect(notice).to_be_visible()
        expect(page.locator("#thumbnailIntervalNoticeValue")).to_have_text("5")

        inp.fill("10")
        expect(notice).to_be_hidden()

        inp.fill("3")
        expect(notice).to_be_visible()
        expect(page.locator("#thumbnailIntervalNoticeValue")).to_have_text("3")

    def test_notice_explains_in_plain_language(self, authed_page: Page, app_url: str) -> None:
        """Sanity check on the user-facing copy — no FFmpeg jargon must leak."""
        page = _settings_page(authed_page, app_url, interval=2)
        notice = page.locator("#thumbnailIntervalSlowPathNotice")
        text = notice.inner_text()
        # Plain-English signals we explicitly want present.
        assert "snapshot" in text.lower()
        assert "fast" in text.lower() and "slow" in text.lower()
        assert "10 seconds" in text
        # Jargon that must NOT appear in this user-facing copy.
        forbidden = ["keyframe", "GOP", "skip_frame", "fps filter", "BIF", "Plex"]
        for term in forbidden:
            assert term not in text, f"User-facing notice should not mention '{term}'"
