"""Logging breadcrumbs for webhook path → canonical resolution.

Born from job ``be0151d2`` (2026-05-21): a Sonarr webhook for a file that
existed on disk failed as "missing on disk" for hours. The forensic dig was
slow precisely because the resolver was SILENT — it expanded ``/data/...``
into the three backing-disk candidates, found none on disk (the media volume
was a stale bind-mount showing the empty local underlay), and quietly fell
back to ``matching_candidates[0]`` with no log line. The operator saw only
the wrong resolved path, never the candidate set nor the "none existed"
signal that points at a mount problem rather than a mapping typo.

See ``project_stale_bindmount_missing_on_disk`` memory.
"""

from __future__ import annotations

import logging as _std_logging
from unittest.mock import patch

import pytest
from loguru import logger as _loguru_logger

from media_preview_generator.jobs.orchestrator import _resolve_webhook_path_to_canonical
from media_preview_generator.servers.registry import server_config_from_dict


@pytest.fixture
def loguru_caplog(caplog):
    """Bridge loguru → pytest caplog at DEBUG so both the WARNING fallback
    line and the DEBUG resolved line are inspectable."""
    handler_id = _loguru_logger.add(
        lambda msg: caplog.records.append(
            _std_logging.LogRecord(
                name="loguru",
                level=msg.record["level"].no,
                pathname="",
                lineno=0,
                msg=msg.record["message"],
                args=(),
                exc_info=None,
            )
        ),
        level="DEBUG",
    )
    try:
        yield caplog
    finally:
        _loguru_logger.remove(handler_id)


def _plex_with_tv_library() -> object:
    """Plex config that owns ``/data_16tb*/TV Shows`` and aliases webhook ``/data``."""
    return server_config_from_dict(
        {
            "id": "plex-1",
            "type": "plex",
            "name": "Plex",
            "enabled": True,
            "url": "http://plex:32400",
            "auth": {},
            "libraries": [
                {
                    "id": "2",
                    "name": "TV Shows",
                    "enabled": True,
                    "remote_paths": [
                        "/data_16tb/TV Shows",
                        "/data_16tb2/TV Shows",
                        "/data_16tb3/TV Shows",
                    ],
                }
            ],
            "path_mappings": [
                {"plex_prefix": "/data_16tb", "local_prefix": "/data_16tb", "webhook_prefixes": ["/data"]},
                {"plex_prefix": "/data_16tb2", "local_prefix": "/data_16tb2", "webhook_prefixes": ["/data"]},
                {"plex_prefix": "/data_16tb3", "local_prefix": "/data_16tb3", "webhook_prefixes": ["/data"]},
            ],
        }
    )


WEBHOOK_PATH = "/data/TV Shows/Show (2024)/Season 01/Show - S01E01.mkv"


class TestCanonicalFallbackLogging:
    def test_warns_with_candidate_set_when_no_candidate_exists_on_disk(self, loguru_caplog):
        """Owners match every backing disk, but NONE of the mapped candidates
        exist on disk → must emit a WARNING that names the candidates checked
        and the source path, so the operator can see the app looked at every
        disk before falling back (the stale-bind-mount signature)."""
        configs = [_plex_with_tv_library()]

        with patch("media_preview_generator.jobs.orchestrator.os.path.exists", return_value=False):
            canonical, owners = _resolve_webhook_path_to_canonical(WEBHOOK_PATH, configs)

        # Behaviour unchanged: still resolves to the first owned candidate.
        assert canonical == "/data_16tb/TV Shows/Show (2024)/Season 01/Show - S01E01.mkv"
        assert [m.server_id for m in owners] == ["plex-1"]

        logged = " ".join(r.msg for r in loguru_caplog.records if r.levelno >= _std_logging.WARNING)
        assert "NONE of the mapped disks" in logged, logged
        # The source path the operator recognises (what Sonarr sent).
        assert WEBHOOK_PATH in logged, logged
        # Names the owning server, not its opaque id, so the per-job log is readable.
        assert "Plex" in logged, logged
        # Proof every backing disk was checked — the bit that screams "mount",
        # not "mapping typo".
        assert "/data_16tb2/TV Shows" in logged, logged
        assert "/data_16tb3/TV Shows" in logged, logged

    def test_info_breadcrumb_when_a_candidate_exists_and_path_translated(self, loguru_caplog):
        """When a candidate exists on disk the picker resolves to it and emits a
        visible INFO ``webhook X → resolved Y`` breadcrumb (never the WARNING).

        This INFO line — not a hidden DEBUG — is what surfaces the applied path
        mapping into the per-job log so operators can see ``/data/...`` was
        translated to ``/data_16tb3/...`` without enabling debug logging."""
        configs = [_plex_with_tv_library()]

        def _only_disk3_exists(p):
            return p == "/data_16tb3/TV Shows/Show (2024)/Season 01/Show - S01E01.mkv"

        with patch("media_preview_generator.jobs.orchestrator.os.path.exists", side_effect=_only_disk3_exists):
            canonical, _ = _resolve_webhook_path_to_canonical(WEBHOOK_PATH, configs)

        assert canonical == "/data_16tb3/TV Shows/Show (2024)/Season 01/Show - S01E01.mkv"
        warnings = " ".join(r.msg for r in loguru_caplog.records if r.levelno >= _std_logging.WARNING)
        assert "NONE of the mapped disks" not in warnings, warnings
        # The mapping must be visible at INFO (captured into job logs), not DEBUG.
        infos = " ".join(r.msg for r in loguru_caplog.records if r.levelno == _std_logging.INFO)
        assert WEBHOOK_PATH in infos and canonical in infos, infos
        # Owner name (not opaque id) must appear on the INFO breadcrumb too.
        assert "Plex" in infos, infos

    def test_no_breadcrumb_logged_when_log_resolution_false(self, loguru_caplog):
        """Count-only callers (the owning-servers summary) pass
        ``log_resolution=False`` so the per-path mapping line isn't logged
        twice per path per job — only the real dispatch pass logs it.

        Uses the candidate-exists path (the INFO branch) — the cell that
        matches the summary caller on a healthy mount — so a regression that
        dropped the gate from the ``elif`` INFO branch would be caught."""
        configs = [_plex_with_tv_library()]

        def _only_disk3_exists(p):
            return p == "/data_16tb3/TV Shows/Show (2024)/Season 01/Show - S01E01.mkv"

        with patch("media_preview_generator.jobs.orchestrator.os.path.exists", side_effect=_only_disk3_exists):
            canonical, owners = _resolve_webhook_path_to_canonical(WEBHOOK_PATH, configs, log_resolution=False)

        assert canonical == "/data_16tb3/TV Shows/Show (2024)/Season 01/Show - S01E01.mkv"
        assert [m.server_id for m in owners] == ["plex-1"]
        emitted = " ".join(r.msg for r in loguru_caplog.records if r.levelno >= _std_logging.INFO)
        assert "Path mapping:" not in emitted, emitted
