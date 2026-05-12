"""Regression: a webhook-origin job whose ``webhook_paths`` got lost must
NOT silently fall through to a full library scan.

This guards against the Job e7968486 incident (May 2026): a single Sonarr
webhook for one TV episode triggered eight separate full-library scans
(~128k items each) across eleven container restarts. The root cause was
that the "Job-at-batch-open" refactor created Jobs without persisting
``webhook_paths`` in ``job.config``. Auto-requeue-on-restart revived the
job with no path list, and ``run_processing``'s branch silently
switched modes:

    if getattr(config, "webhook_paths", None):
        ... # webhook phase
    else:
        ... # full library scan  ← attributed to the webhook's Job ID

The webhook fix (persisting ``webhook_paths`` in ``job.config``) closes
the primary hole. This module pins the defense-in-depth: even if some
other code path produces a malformed webhook job, the orchestrator
refuses to fall through to ``_run_full_scan_multi_server`` /
``_run_plex_full_scan_phase``.
"""

from __future__ import annotations

import logging as _std_logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from loguru import logger as _loguru_logger

from media_preview_generator.jobs.orchestrator import _classify_processing_mode, run_processing


@pytest.fixture
def loguru_caplog(caplog):
    """Bridge loguru → pytest caplog so assertions can inspect ``logger.error``
    output. Without this bridge, loguru sinks bypass the stdlib logging
    plumbing that ``caplog`` captures. Pattern lifted from
    ``test_full_scan_multi_server.py``.
    """
    handler_id = _loguru_logger.add(
        lambda msg: caplog.records.append(
            _std_logging.LogRecord(
                name="loguru",
                level=_std_logging.ERROR,
                pathname="",
                lineno=0,
                msg=msg.record["message"],
                args=(),
                exc_info=None,
            )
        ),
        level="ERROR",
    )
    try:
        yield caplog
    finally:
        _loguru_logger.remove(handler_id)


class TestClassifyProcessingMode:
    """Pin the branch decision at ``run_processing``'s mode-select line."""

    def test_webhook_paths_present_returns_webhook_mode(self):
        config = SimpleNamespace(webhook_paths=["/tv/x.mkv"], webhook_source="sonarr")
        assert _classify_processing_mode(config) == "webhook_paths"

    def test_neither_field_returns_full_scan(self):
        """No webhook markers and no paths → legitimate full library scan."""
        config = SimpleNamespace(webhook_paths=None, webhook_source=None)
        assert _classify_processing_mode(config) == "full_scan"

    def test_webhook_source_but_no_paths_returns_refuse(self):
        """The defense in depth: webhook origin + missing paths must not
        silently become a full scan. This is the exact corruption shape
        that produced Job e7968486."""
        config = SimpleNamespace(webhook_paths=None, webhook_source="sonarr")
        assert _classify_processing_mode(config) == "refuse_malformed_webhook"

    def test_webhook_source_with_empty_list_returns_refuse(self):
        """Empty-list webhook_paths (falsy) is treated the same as None."""
        config = SimpleNamespace(webhook_paths=[], webhook_source="sonarr")
        assert _classify_processing_mode(config) == "refuse_malformed_webhook"

    def test_missing_attributes_fall_back_to_full_scan(self):
        """A bare config without either attribute returns full_scan
        (preserves the pre-fix default for scheduled / manual jobs)."""
        config = SimpleNamespace()
        assert _classify_processing_mode(config) == "full_scan"


class TestRunProcessingRefusesMalformedWebhook:
    """Integration: ``run_processing`` itself must refuse early — BEFORE
    reaching either ``_run_full_scan_multi_server`` (the multi-server
    fast path that Job e7968486 actually hit) or
    ``_run_plex_full_scan_phase`` (the legacy single-Plex path)."""

    def _malformed_config(self, tmp_path=None):
        """Webhook-origin job with no paths — the exact shape that
        produced Job e7968486 after revival.

        ``working_tmp_folder`` is required because ``run_processing``'s
        ``finally`` block runs cleanup even when the function returns
        early from the refusal branch.
        """
        return SimpleNamespace(
            webhook_paths=None,
            webhook_source="sonarr",
            server_id_filter=None,
            plex_url="",
            plex_token="",
            plex_library_ids=None,
            gpu_threads=0,
            cpu_threads=1,
            working_tmp_folder="/tmp/test-orchestrator-fallthrough-nonexistent",
        )

    def test_refuses_before_multi_server_full_scan(self, loguru_caplog):
        """The exact regression path: multi-server install + revived
        webhook job → must NOT call ``_run_full_scan_multi_server``.

        Also asserts the operator-facing ERROR log fires — the log line
        IS the fix from the user's perspective (it tells them WHY their
        webhook job didn't process). A silent return would technically
        prevent the runaway scan but leave the operator confused.
        """
        config = self._malformed_config()
        with (
            patch("media_preview_generator.jobs.orchestrator._run_full_scan_multi_server") as mock_multi,
            patch("media_preview_generator.jobs.orchestrator._run_plex_full_scan_phase") as mock_legacy,
            patch("media_preview_generator.jobs.orchestrator._run_webhook_paths_phase") as mock_webhook,
        ):
            result = run_processing(config, selected_gpus=[])

        # Webhook-origin job with no paths must NOT trigger
        # _run_full_scan_multi_server — that's the Job e7968486 regression.
        mock_multi.assert_not_called()
        mock_legacy.assert_not_called()
        mock_webhook.assert_not_called()
        assert result is not None
        assert result.get("error") == "webhook_paths_missing", result
        # Operator-facing log: removing this line means operators see a
        # silently-failing webhook job in the UI with no explanation.
        logged = " ".join(r.msg for r in loguru_caplog.records)
        assert "Refusing to run job as full library scan" in logged, logged
        assert "webhook source" in logged, logged
        # Pin that the SUT actually interpolates the source it received,
        # not a hard-coded literal or None. Without this, a refactor that
        # drops the ``getattr(config, "webhook_source", ...)`` call would
        # leave the test passing while operators lose the source attribution.
        assert "'sonarr'" in logged, logged

    def test_refuses_before_legacy_plex_full_scan(self, loguru_caplog):
        """Same refusal must happen on a pure-Plex install (different
        code path, same refusal)."""
        config = self._malformed_config()
        config.plex_url = "http://plex:32400"
        config.plex_token = "tok"
        with (
            patch("media_preview_generator.jobs.orchestrator._run_full_scan_multi_server") as mock_multi,
            patch("media_preview_generator.jobs.orchestrator._run_plex_full_scan_phase") as mock_legacy,
            patch("media_preview_generator.jobs.orchestrator._run_webhook_paths_phase") as mock_webhook,
        ):
            result = run_processing(config, selected_gpus=[])

        mock_multi.assert_not_called()
        # Webhook-origin job with no paths must NOT fall through to
        # _run_plex_full_scan_phase either.
        mock_legacy.assert_not_called()
        mock_webhook.assert_not_called()
        assert result.get("error") == "webhook_paths_missing", result
        logged = " ".join(r.msg for r in loguru_caplog.records)
        assert "Refusing to run job as full library scan" in logged, logged

    def test_legitimate_full_scan_still_runs(self):
        """Control: a non-webhook job (no webhook_source) with no paths
        is a legitimate full scan and must still proceed."""
        config = SimpleNamespace(
            webhook_paths=None,
            webhook_source=None,
            server_id_filter=None,
            plex_url="",
            plex_token="",
            plex_library_ids=None,
            gpu_threads=0,
            cpu_threads=1,
            working_tmp_folder="/tmp/test-orchestrator-fallthrough-nonexistent",
        )
        with (
            patch(
                "media_preview_generator.jobs.orchestrator._run_full_scan_multi_server",
                return_value={"generated": 0},
            ) as mock_multi,
        ):
            result = run_processing(config, selected_gpus=[])

        # The multi-server path runs (this install path has no Plex, so
        # _should_use_multi_server_full_scan returns True). The refusal
        # branch did NOT fire.
        mock_multi.assert_called_once()
        assert result.get("error") != "webhook_paths_missing"
