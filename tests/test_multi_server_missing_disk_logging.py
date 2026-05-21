"""Diagnostic breadcrumbs for the "Source video file is missing on disk" path.

Companion to ``test_orchestrator_canonical_fallback_logging``. When dispatch
lands on a path that ``os.path.isfile`` rejects, ``_probe_sibling_mounts``
tries the same suffix on every sibling local mount. Before this change it
returned a bare ``str | None`` and probed silently, so a failed probe left no
trace — the operator couldn't tell whether the file was checked on the other
disks at all. Now it returns the list of sibling paths it stat'd, the caller
folds that into the warning, and a DEBUG line records each attempt.

See ``project_stale_bindmount_missing_on_disk`` memory.
"""

from __future__ import annotations

import logging as _std_logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from loguru import logger as _loguru_logger

from media_preview_generator.processing.multi_server import (
    _missing_on_disk_message,
    _probe_sibling_mounts,
)


@pytest.fixture
def loguru_debug_caplog(caplog):
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


def _registry_with_three_disks():
    cfg = SimpleNamespace(
        enabled=True,
        path_mappings=[
            {"local_prefix": "/data_16tb"},
            {"local_prefix": "/data_16tb2"},
            {"local_prefix": "/data_16tb3"},
        ],
    )
    return SimpleNamespace(configs=lambda: [cfg])


CANON = "/data_16tb/TV Shows/Show/Season 01/Show - S01E01.mkv"
ON_DISK3 = "/data_16tb3/TV Shows/Show/Season 01/Show - S01E01.mkv"


class TestProbeSiblingMountsContract:
    def test_returns_rebound_path_and_the_siblings_tried(self):
        registry = _registry_with_three_disks()
        with patch(
            "media_preview_generator.processing.multi_server.os.path.isfile",
            side_effect=lambda p: p == ON_DISK3,
        ):
            rebound, tried = _probe_sibling_mounts(CANON, registry)
        assert rebound == ON_DISK3
        # Every sibling stat'd up to and including the hit is reported.
        assert ON_DISK3 in tried

    def test_returns_none_and_full_tried_list_when_no_sibling_holds_it(self, loguru_debug_caplog):
        registry = _registry_with_three_disks()
        with patch(
            "media_preview_generator.processing.multi_server.os.path.isfile",
            return_value=False,
        ):
            rebound, tried = _probe_sibling_mounts(CANON, registry)
        assert rebound is None
        # Both siblings (not the matched /data_16tb) were probed and reported.
        # Order between equal-length prefixes isn't contractual, so compare sets.
        assert sorted(tried) == [
            "/data_16tb2/TV Shows/Show/Season 01/Show - S01E01.mkv",
            "/data_16tb3/TV Shows/Show/Season 01/Show - S01E01.mkv",
        ]
        debugs = " ".join(r.msg for r in loguru_debug_caplog.records)
        assert "/data_16tb3/TV Shows/Show/Season 01/Show - S01E01.mkv" in debugs, debugs

    def test_returns_empty_tried_list_when_no_siblings_to_probe(self):
        """Single-mount install → nothing to probe, empty tried list."""
        cfg = SimpleNamespace(enabled=True, path_mappings=[{"local_prefix": "/data_16tb"}])
        registry = SimpleNamespace(configs=lambda: [cfg])
        rebound, tried = _probe_sibling_mounts(CANON, registry)
        assert rebound is None
        assert tried == []


class TestMissingOnDiskMessage:
    def test_lists_probed_siblings_and_mount_hint(self):
        msg = _missing_on_disk_message(CANON, ["/data_16tb2/x.mkv", "/data_16tb3/x.mkv"])
        assert CANON in msg
        assert "/data_16tb2/x.mkv" in msg and "/data_16tb3/x.mkv" in msg
        # The diagnosis that would have saved hours: point at the mount.
        assert "mounted inside this container" in msg

    def test_base_message_when_no_siblings_probed(self):
        msg = _missing_on_disk_message(CANON, [])
        assert CANON in msg
        # No phantom "also checked" clause when there was nothing to probe.
        assert "Also checked" not in msg
