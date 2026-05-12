"""Tests for server-type branched retry-chain info tooltip.

The retry-chain ⓘ tooltip used to be a single template (``infoRetryChainTpl``)
that explained Jellyfin's 45s LibraryMonitor delay + the "Generate Trickplay
Images" scheduled task — neither of which applies to Plex. Since the same
``PUBLISHED_PENDING_REGISTRATION`` status can also fire for Plex (via the
skip-if-exists branch in ``multi_server.py:1030-1046``), Plex users could
land on the tooltip and read a Jellyfin-only explanation.

These tests pin the contract:

* Two templates exist: ``infoRetryChainPlexTpl`` and ``infoRetryChainJellyfinTpl``.
* Each template's wording matches its server type.
* The JS picker chooses the right one based on ``job.server_type``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TPL_FILE = REPO_ROOT / "media_preview_generator" / "web" / "templates" / "_shared_info_templates.html"
APP_JS = REPO_ROOT / "media_preview_generator" / "web" / "static" / "js" / "app.js"
MODAL_JS = REPO_ROOT / "media_preview_generator" / "web" / "static" / "js" / "job_modal.js"


@pytest.fixture(scope="module")
def template_html() -> str:
    return TPL_FILE.read_text()


@pytest.fixture(scope="module")
def app_js() -> str:
    return APP_JS.read_text()


@pytest.fixture(scope="module")
def modal_js() -> str:
    return MODAL_JS.read_text()


# ---------------------------------------------------------------------------
# Template existence + content contracts
# ---------------------------------------------------------------------------


def test_both_retry_chain_templates_exist(template_html: str):
    assert 'id="infoRetryChainPlexTpl"' in template_html
    assert 'id="infoRetryChainJellyfinTpl"' in template_html
    # The unified (pre-branching) template name must NOT exist anymore — if
    # it does, some call site is still pointing at the dead id.
    assert 'id="infoRetryChainTpl"' not in template_html


def test_plex_template_avoids_jellyfin_specific_language(template_html: str):
    """The Plex variant must NOT cite Jellyfin's 45s LibraryMonitor or the
    "Generate Trickplay Images" scheduled task — those concepts only apply
    to Jellyfin and confused Plex-only users in the original unified copy."""
    plex_section = _extract_template(template_html, "infoRetryChainPlexTpl")
    assert "LibraryMonitor" not in plex_section
    assert "Generate Trickplay Images" not in plex_section
    # And it should reference Plex's actual root cause.
    assert "Plex" in plex_section
    assert "library scan" in plex_section.lower() or "library" in plex_section.lower()


def test_jellyfin_template_keeps_existing_explanation(template_html: str):
    """The Jellyfin variant keeps the LibraryMonitor + Generate-Trickplay
    explanation — those are real Jellyfin behaviors and the Setup Health
    card already cross-references them."""
    jellyfin_section = _extract_template(template_html, "infoRetryChainJellyfinTpl")
    assert "LibraryMonitor" in jellyfin_section
    assert "Generate Trickplay Images" in jellyfin_section


# ---------------------------------------------------------------------------
# JS picker contract
# ---------------------------------------------------------------------------


def test_picker_function_defined_in_app_js(app_js: str):
    """``_pickRetryInfoTpl(job)`` lives in app.js so job_modal.js can call it
    (app.js loads first per base.html)."""
    assert "function _pickRetryInfoTpl(" in app_js


def test_picker_returns_plex_template_for_plex_server_type(app_js: str):
    """The picker must select the Plex template when job.server_type is 'plex'."""
    # Test by string-asserting both template ids and the 'plex' guard appear
    # in the picker's body. The function is small enough that a regex scan is
    # robust to whitespace.
    plex_branch_idx = app_js.find("function _pickRetryInfoTpl(")
    assert plex_branch_idx >= 0
    body = app_js[plex_branch_idx : plex_branch_idx + 500]
    assert "'plex'" in body or '"plex"' in body
    assert "infoRetryChainPlexTpl" in body
    assert "infoRetryChainJellyfinTpl" in body


def test_dashboard_retry_chip_uses_picker(app_js: str):
    """``_renderRetryChip`` must pass the picker's result as the template id,
    not hard-code the old unified ``infoRetryChainTpl``."""
    chip_idx = app_js.find("function _renderRetryChip(")
    assert chip_idx >= 0
    body = app_js[chip_idx : chip_idx + 2000]
    assert "_pickRetryInfoTpl(job)" in body
    assert 'data-explain-template="infoRetryChainTpl"' not in body


def test_modal_attempts_chip_uses_picker(modal_js: str):
    """Job-modal's attempts-state chip must also use the picker. Falls back
    to the Jellyfin template if for some reason the picker isn't available
    (defensive only — app.js loads first per base.html)."""
    assert "_pickRetryInfoTpl(job)" in modal_js
    # The old unified template id MUST be gone from this file.
    assert 'data-explain-template="infoRetryChainTpl"' not in modal_js


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_template(html: str, template_id: str) -> str:
    """Return the inner body of the ``<template id="...">`` block."""
    start_tag = f'<template id="{template_id}">'
    idx = html.find(start_tag)
    assert idx >= 0, f"template {template_id!r} not found"
    end_idx = html.find("</template>", idx)
    assert end_idx >= 0, f"unclosed template {template_id!r}"
    return html[idx + len(start_tag) : end_idx]
