"""Backend-real E2E: boot the app against an older settings.json schema.

Audit gap #9: ``upgrade.py`` has unit tests, but no e2e test boots the
real Flask subprocess with a known-old settings file and asserts:

* Boot succeeds (no crash, schema migration runs cleanly)
* Migrated keys are present with the expected shape
* Old/legacy keys that v7 promotes are reflected in the new structure

Bug class caught: migration silently dropping fields, infinite migration
loop, "settings corrupted" lockout when an existing user upgrades the
container image. This is the upgrade path users actually walk.

We seed a v7-era settings.json (the schema before frame_reuse was added)
with the legacy plex_* flat keys that v7 promotes into media_servers, and
assert v8/v9/v10/v11 all run without dropping data.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from media_preview_generator.upgrade import _CURRENT_SCHEMA_VERSION
from tests.e2e.conftest import _build_fake_ffmpeg_path, _capture_session_cookie, get_free_port, wait_for_port


def _write_legacy_settings(config_dir: Path, schema_version: int) -> None:
    """Write a settings.json shaped like an older release.

    Pre-v8 settings carried path_mappings and exclude_paths at the
    top level; v8 promoted them under media_servers[0]. Pre-v11 had
    no frame_reuse block; v11 seeds it with sane defaults. The
    setup_complete flag bypasses the wizard so the dashboard loads.
    """
    legacy = {
        "_schema_version": schema_version,
        "setup_complete": True,
        # Legacy flat plex_* keys — v7 synthesises media_servers from these.
        "plex_url": "http://plex.invalid:32400",
        "plex_token": "x" * 20,
        "plex_verify_ssl": True,
        "plex_timeout": 60,
        "plex_config_folder": "/tmp/plex-config",
        "thumbnail_interval": 5,
        # Top-level path_mappings — v8 should promote into media_servers[0].
        "path_mappings": [
            {"plex_prefix": "/data/movies", "local_prefix": "/local/movies"},
        ],
        "exclude_paths": [],
        "cpu_threads": 0,
        "gpu_threads": 0,
        "tonemap_algorithm": "hable",
        "log_level": "INFO",
        "log_rotation_size": "10 MB",
        "log_retention_count": 5,
        "job_history_days": 30,
        "gpu_config": [],
        "media_servers": None,  # forces v7 to synthesise from plex_* keys
        "auto_requeue_on_restart": False,
        "webhook_delay": 1,
    }
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "settings.json").write_text(json.dumps(legacy))


def _start_app_for_migration(config_dir: Path, port: int) -> subprocess.Popen:
    """Start a real Flask subprocess just like the conftest helper does,
    but without going through the parametrize indirection (which would
    seed our values away).
    """
    fake_bin = _build_fake_ffmpeg_path(str(config_dir))
    env = {
        **os.environ,
        "WEB_PORT": str(port),
        "CONFIG_DIR": str(config_dir),
        "WEB_AUTH_TOKEN": "e2e-test-token",
        "PATH": fake_bin + os.pathsep + os.environ.get("PATH", ""),
        "CORS_ORIGINS": "*",
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"from media_preview_generator.web.app import run_server; run_server(host='0.0.0.0', port={port})",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if not wait_for_port(port, timeout=20):
        stdout, stderr = proc.communicate(timeout=5)
        proc.kill()
        raise RuntimeError(
            f"App failed to boot against legacy schema. \nstdout: {stdout.decode()}\nstderr: {stderr.decode()}"
        )
    return proc


@pytest.mark.e2e
class TestSchemaMigrationBoot:
    def test_boot_with_v7_settings_completes_migration_chain(
        self,
        tmp_path_factory,
    ) -> None:
        """A v7 settings.json must boot cleanly and migrate to current.

        Without this test, the upgrade path is exercised only in unit
        tests against an in-process SettingsManager — which can mask
        bugs in the boot ordering (e.g. SettingsManager's ``_load`` migrating
        legacy plex_* keys before run_migrations gets to v7's chain).
        """
        config_dir = tmp_path_factory.mktemp("legacy_settings")
        _write_legacy_settings(config_dir, schema_version=7)

        port = get_free_port()
        proc = _start_app_for_migration(config_dir, port)
        try:
            # The boot itself succeeded (port is open). Now verify the
            # migration ran by reading the on-disk settings.json.
            with open(config_dir / "settings.json") as f:
                migrated = json.load(f)

            # The schema version must have advanced to current.
            assert migrated.get("_schema_version") == _CURRENT_SCHEMA_VERSION, (
                f"Schema version did not advance: got {migrated.get('_schema_version')!r}, "
                f"expected {_CURRENT_SCHEMA_VERSION}. The migration chain didn't complete."
            )

            # v11 must have seeded the frame_reuse block.
            frame_reuse = migrated.get("frame_reuse")
            assert isinstance(frame_reuse, dict), (
                f"v11 migration didn't seed frame_reuse — got {frame_reuse!r}. "
                "Users who upgrade from v10 won't get cross-server frame caching."
            )
            assert frame_reuse.get("enabled") is True, f"frame_reuse seeded with wrong default: {frame_reuse}"
            assert frame_reuse.get("ttl_minutes") == 60
            assert frame_reuse.get("max_cache_disk_mb") == 2048

            # And the test endpoint must serve up the migrated state.
            cookie = _capture_session_cookie(f"http://localhost:{port}")
            assert cookie is not None

        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def test_boot_with_v6_settings_promotes_legacy_plex_keys_into_media_servers(
        self,
        tmp_path_factory,
    ) -> None:
        """v6→current must synthesise media_servers from plex_* flat keys.

        This is the v7 contract: a long-running single-Plex deployment
        with no media_servers array at all must end up with one Plex
        entry after upgrade. If v7 silently drops the data, the user's
        existing Plex disappears from the Servers page on upgrade.
        """
        config_dir = tmp_path_factory.mktemp("legacy_v6_settings")
        _write_legacy_settings(config_dir, schema_version=6)

        port = get_free_port()
        proc = _start_app_for_migration(config_dir, port)
        try:
            with open(config_dir / "settings.json") as f:
                migrated = json.load(f)

            servers = migrated.get("media_servers", [])
            assert isinstance(servers, list), f"media_servers not a list after v6→v7 migration: {servers!r}"
            assert len(servers) == 1, (
                f"v7 should have synthesised exactly 1 server from legacy plex_* keys, got {len(servers)}: {servers}"
            )
            plex = servers[0]
            assert plex.get("type") == "plex", f"Migrated server has wrong type: {plex}"
            assert plex.get("url") == "http://plex.invalid:32400", (
                f"Migrated server has wrong URL — v7 dropped or mangled plex_url: {plex}"
            )
            # v8 should have promoted the top-level path_mappings into the server.
            mapped = plex.get("path_mappings") or []
            assert any(isinstance(m, dict) and m.get("plex_prefix") == "/data/movies" for m in mapped), (
                f"v8 migration didn't promote top-level path_mappings into media_servers[0]: "
                f"{mapped}. The user's path mappings vanished on upgrade."
            )

            # The boot also serves /api/servers via the real registry.
            cookie = _capture_session_cookie(f"http://localhost:{port}")
            import http.cookiejar
            import urllib.request

            jar = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
            req = urllib.request.Request(
                f"http://localhost:{port}/api/servers",
                headers={
                    "Cookie": f"{cookie['name']}={cookie['value']}",
                    "X-Auth-Token": "e2e-test-token",
                },
            )
            with opener.open(req, timeout=10) as resp:  # noqa: S310 (test-only localhost)
                body = json.loads(resp.read().decode())
            assert any(s.get("type") == "plex" for s in body.get("servers", [])), (
                f"GET /api/servers returned no Plex server after v6→current migration: "
                f"{body}. The migration ran on disk but the API doesn't see it — likely "
                "a load-order bug."
            )

        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def test_boot_with_already_current_schema_is_a_noop(
        self,
        tmp_path_factory,
    ) -> None:
        """Booting with current schema must not re-run migrations.

        Bug class: an off-by-one in the version comparison would re-run
        v11 every boot, which would clobber any user-tuned frame_reuse
        settings every restart. The check is `current == _CURRENT_SCHEMA_VERSION
        → return` in upgrade.py.
        """
        config_dir = tmp_path_factory.mktemp("current_schema_settings")
        _write_legacy_settings(config_dir, schema_version=_CURRENT_SCHEMA_VERSION)
        # Seed a user-tuned frame_reuse value — if the migration re-fires
        # this will get overwritten back to defaults (the bug we're catching).
        existing = json.loads((config_dir / "settings.json").read_text())
        existing["frame_reuse"] = {"enabled": False, "ttl_minutes": 999, "max_cache_disk_mb": 4096}
        existing["media_servers"] = []
        (config_dir / "settings.json").write_text(json.dumps(existing))

        port = get_free_port()
        proc = _start_app_for_migration(config_dir, port)
        try:
            # Give the app a moment to settle (any rogue re-migration would
            # have written by now).
            time.sleep(1.0)
            with open(config_dir / "settings.json") as f:
                after = json.load(f)
            assert after.get("frame_reuse", {}).get("ttl_minutes") == 999, (
                f"Booting with already-current schema clobbered the user's tuned "
                f"frame_reuse.ttl_minutes (now {after.get('frame_reuse')}). "
                "The migration is re-running every boot."
            )
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
