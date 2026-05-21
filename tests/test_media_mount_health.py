"""Proactive detection of unusable media mounts.

The reactive logging (orchestrator + multi_server) tells you a job failed.
This catches the cause *before* any job runs: a configured ``local_prefix``
that's missing or — the stale-bind-mount signature — exists but is empty
(the container captured the empty local underlay before the network share
was mounted). See ``project_stale_bindmount_missing_on_disk``.
"""

from __future__ import annotations

from media_preview_generator.config.paths import detect_unhealthy_media_mounts


def _mapping(local: str) -> dict:
    return {"plex_prefix": local, "local_prefix": local, "webhook_prefixes": ["/data"]}


class TestDetectUnhealthyMediaMounts:
    def test_flags_empty_mount_as_likely_unmounted(self, tmp_path):
        """A configured prefix that exists but is empty is the stale-bind-mount
        symptom — the bug that cost hours on job be0151d2."""
        empty = tmp_path / "data_16tb3"
        empty.mkdir()
        issues = detect_unhealthy_media_mounts([_mapping(str(empty))])
        assert len(issues) == 1
        assert issues[0]["path"] == str(empty)
        assert issues[0]["issue"] == "empty"

    def test_flags_missing_directory(self, tmp_path):
        missing = tmp_path / "not_mounted"
        issues = detect_unhealthy_media_mounts([_mapping(str(missing))])
        assert [i["issue"] for i in issues] == ["missing"]

    def test_healthy_mount_with_content_is_not_flagged(self, tmp_path):
        good = tmp_path / "data_16tb"
        good.mkdir()
        (good / "TV Shows").mkdir()
        issues = detect_unhealthy_media_mounts([_mapping(str(good))])
        assert issues == []

    def test_dedupes_repeated_local_prefix(self, tmp_path):
        empty = tmp_path / "shared"
        empty.mkdir()
        # Same local_prefix appears in two mapping rows — report once.
        issues = detect_unhealthy_media_mounts([_mapping(str(empty)), _mapping(str(empty))])
        assert len(issues) == 1

    def test_mixed_health_reports_only_the_bad_ones(self, tmp_path):
        good = tmp_path / "good"
        good.mkdir()
        (good / "x").write_text("hi")
        empty = tmp_path / "empty"
        empty.mkdir()
        missing = tmp_path / "gone"
        issues = detect_unhealthy_media_mounts([_mapping(str(good)), _mapping(str(empty)), _mapping(str(missing))])
        by_path = {i["path"]: i["issue"] for i in issues}
        assert by_path == {str(empty): "empty", str(missing): "missing"}

    def test_blank_local_prefix_is_ignored(self):
        issues = detect_unhealthy_media_mounts([{"plex_prefix": "/x", "local_prefix": ""}])
        assert issues == []


class TestStartupMediaMountWarning:
    """The startup glue: aggregate across every server's path_mappings and
    log a WARNING per issue so a stale mount is screaming in the log the
    moment the container boots — not discovered job-by-job hours later."""

    def test_logs_warning_per_unhealthy_mount_across_servers(self, tmp_path):
        from loguru import logger as _loguru_logger

        from media_preview_generator.web.app import _warn_unhealthy_media_mounts

        empty = tmp_path / "data_16tb3"
        empty.mkdir()
        good = tmp_path / "data_16tb"
        good.mkdir()
        (good / "x").write_text("hi")

        media_servers = [
            {"name": "Plex", "path_mappings": [_mapping(str(good)), _mapping(str(empty))]},
            {"name": "Emby", "path_mappings": [_mapping(str(empty))]},  # same empty disk
        ]

        captured: list[str] = []
        sink = _loguru_logger.add(lambda m: captured.append(m.record["message"]), level="WARNING")
        try:
            issues = _warn_unhealthy_media_mounts(media_servers)
        finally:
            _loguru_logger.remove(sink)

        # Deduped across servers → the empty disk reported once.
        assert [i["path"] for i in issues] == [str(empty)]
        logged = " ".join(captured)
        assert str(empty) in logged
        assert "mounted" in logged.lower()

    def test_no_warning_when_all_mounts_healthy(self, tmp_path):
        from media_preview_generator.web.app import _warn_unhealthy_media_mounts

        good = tmp_path / "data_16tb"
        good.mkdir()
        (good / "TV Shows").mkdir()
        assert _warn_unhealthy_media_mounts([{"path_mappings": [_mapping(str(good))]}]) == []
