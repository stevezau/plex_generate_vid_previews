"""Sanitized fixtures for README screenshot regeneration.

Produces a settings.json + jobs.db pair with plausible — but entirely
fake — data for docs/images/ captures. No IPs, no real hostnames, no
real server names. See ``regen_readme.py`` for the capture driver that
consumes these helpers.

The fake host ``your-server.local`` is RFC-6762-reserved (``.local``)
and cannot leak anywhere public. The three vendors (plex / emby /
jellyfin) are seeded so the Servers page renders a multi-vendor card
row, matching the README claim that the tool supports all three.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

FAKE_HOST = "your-server.local"

FAKE_SERVERS: list[dict[str, Any]] = [
    {
        "id": "plex-home",
        "type": "plex",
        "name": "Home Plex",
        "enabled": True,
        "url": f"https://plex.{FAKE_HOST}:32400",
        "auth": {"token": "x" * 20},
        "verify_ssl": False,
        "timeout": 30,
        "libraries": [
            {"id": "1", "title": "Movies", "type": "movie", "enabled": True},
            {"id": "2", "title": "TV Shows", "type": "show", "enabled": True},
            {"id": "3", "title": "Kids", "type": "movie", "enabled": False},
        ],
        "path_mappings": [
            {"local": "/media/movies", "remote": "/data/movies"},
            {"local": "/media/tv", "remote": "/data/tv"},
        ],
        "exclude_paths": [],
        "output": {"plex_config_folder": "/plex"},
        "server_identity": "plex-identity-fake",
    },
    {
        "id": "jellyfin-home",
        "type": "jellyfin",
        "name": "Home Jellyfin",
        "enabled": True,
        "url": f"https://jellyfin.{FAKE_HOST}:8096",
        "auth": {"api_key": "y" * 32},
        "verify_ssl": True,
        "timeout": 30,
        "libraries": [
            {"id": "a1", "title": "Movies", "type": "movie", "enabled": True},
            {"id": "a2", "title": "Shows", "type": "show", "enabled": True},
        ],
        "path_mappings": [],
        "exclude_paths": [],
        "output": {},
        "server_identity": "jellyfin-identity-fake",
    },
    {
        "id": "emby-home",
        "type": "emby",
        "name": "Home Emby",
        "enabled": True,
        "url": f"https://emby.{FAKE_HOST}:8096",
        "auth": {"api_key": "z" * 32},
        "verify_ssl": True,
        "timeout": 30,
        "libraries": [
            {"id": "b1", "title": "Films", "type": "movie", "enabled": True},
        ],
        "path_mappings": [],
        "exclude_paths": [],
        "output": {},
        "server_identity": "emby-identity-fake",
    },
]


def _base_settings() -> dict[str, Any]:
    return {
        "setup_complete": True,
        "media_servers": FAKE_SERVERS,
        # Legacy Plex fast-path keys still inspected by is_configured().
        # Kept aligned with media_servers[0] so the old code path matches.
        "plex_url": FAKE_SERVERS[0]["url"],
        "plex_token": FAKE_SERVERS[0]["auth"]["token"],
        "plex_config_folder": "/plex",
        "plex_verify_ssl": False,
        "thumbnail_interval": 10,
        "thumbnail_quality": 4,
        "regenerate_thumbnails": False,
        "cpu_threads": 4,
        "gpu_config": [
            {
                "index": 0,
                "vendor": "NVIDIA",
                "model": "NVIDIA TITAN RTX",
                "enabled": True,
                "workers": 3,
                "ffmpeg_threads": 2,
            },
            {
                "index": 1,
                "vendor": "Intel",
                "model": "Intel UHD Graphics 770",
                "enabled": True,
                "workers": 1,
                "ffmpeg_threads": 2,
            },
        ],
        "webhook_enabled": True,
        "webhook_delay": 60,
        "webhook_retry_count": 3,
        "webhook_secret": "",
        "dismissed_notifications": [],
    }


def write_settings(config_dir: str | Path) -> Path:
    """Write a sanitized settings.json into ``config_dir``.

    Returns the path of the written file. The caller is responsible for
    creating ``config_dir`` first if it does not exist.
    """
    config_path = Path(config_dir)
    config_path.mkdir(parents=True, exist_ok=True)
    target = config_path / "settings.json"
    with open(target, "w") as fh:
        json.dump(_base_settings(), fh, indent=2)
    return target


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def seed_jobs(config_dir: str | Path) -> int:
    """Seed fake job rows into the ``jobs.db`` under ``config_dir``.

    Uses ``JobStorage.upsert`` so the schema stays in lockstep with
    whatever the app currently expects. Safe to call on a fresh config
    directory — JobStorage creates the DB if missing.

    Returns the number of rows seeded.
    """
    # Imported lazily so importing this module doesn't drag in the whole
    # web app just to read fixture data.
    from media_preview_generator.web.jobs import (  # noqa: PLC0415
        Job,
        JobProgress,
        JobStatus,
        JobStorage,
    )

    db_path = str(Path(config_dir) / "jobs.db")
    storage = JobStorage(db_path)
    try:
        now = datetime.now(timezone.utc)
        rows: list[Job] = []

        # 6 completed jobs spread across the last two days + both vendors.
        completed_fixtures = [
            ("Movies", "plex-home", "Home Plex", "plex", 842, 842),
            ("TV Shows", "plex-home", "Home Plex", "plex", 124, 124),
            ("Movies", "jellyfin-home", "Home Jellyfin", "jellyfin", 312, 312),
            ("Shows", "jellyfin-home", "Home Jellyfin", "jellyfin", 58, 58),
            ("Films", "emby-home", "Home Emby", "emby", 401, 401),
            ("Kids", "plex-home", "Home Plex", "plex", 47, 47),
        ]
        for i, (lib, sid, sname, stype, total, processed) in enumerate(completed_fixtures):
            created = now - timedelta(hours=(i + 1) * 3, minutes=7 * i)
            started = created + timedelta(seconds=4)
            finished = started + timedelta(minutes=6 + i * 2)
            rows.append(
                Job(
                    id=str(uuid.uuid4()),
                    status=JobStatus.COMPLETED,
                    created_at=_iso(created),
                    started_at=_iso(started),
                    completed_at=_iso(finished),
                    library_id=f"lib-{i}",
                    library_name=lib,
                    server_id=sid,
                    server_name=sname,
                    server_type=stype,
                    progress=JobProgress(
                        percent=100.0,
                        total_items=total,
                        processed_items=processed,
                        outcome={"created": processed, "skipped": 0, "failed": 0},
                    ),
                    config={"trigger": "manual", "path_count": total},
                )
            )

        # Three more completed jobs to bring the list to 9. All seeded
        # rows MUST be COMPLETED — ``JobManager._load_from_disk`` flips
        # any RUNNING row to FAILED on startup (jobs.py:557-564) and
        # PENDING rows sit in the "interrupted jobs" list. Both outcomes
        # muddy the marketing shot; COMPLETED is the only status that
        # survives the boot flip cleanly.
        extra_completed = [
            ("Stand-up Comedy", "jellyfin-home", "Home Jellyfin", "jellyfin", 61, 61),
            ("Documentaries", "emby-home", "Home Emby", "emby", 138, 138),
            ("Anime", "plex-home", "Home Plex", "plex", 224, 224),
        ]
        for j, (lib, sid, sname, stype, total, processed) in enumerate(extra_completed):
            created = now - timedelta(hours=(6 + j), minutes=11 * j)
            started = created + timedelta(seconds=3)
            finished = started + timedelta(minutes=4 + j)
            rows.append(
                Job(
                    id=str(uuid.uuid4()),
                    status=JobStatus.COMPLETED,
                    created_at=_iso(created),
                    started_at=_iso(started),
                    completed_at=_iso(finished),
                    library_id=f"lib-extra-{j}",
                    library_name=lib,
                    server_id=sid,
                    server_name=sname,
                    server_type=stype,
                    progress=JobProgress(
                        percent=100.0,
                        total_items=total,
                        processed_items=processed,
                        outcome={"created": processed, "skipped": 0, "failed": 0},
                    ),
                    config={"trigger": "manual", "path_count": total},
                )
            )

        for row in rows:
            storage.upsert(row)

        return len(rows)
    finally:
        storage.close()


__all__ = ["FAKE_HOST", "FAKE_SERVERS", "write_settings", "seed_jobs"]
