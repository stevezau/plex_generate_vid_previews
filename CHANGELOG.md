# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

> **Status: Beta** — This release is under active development.

### Added

#### Web UI

- Full-featured dashboard with real-time progress, worker status cards, and job logs viewer
- Responsive card layout with radio buttons for job mode
- 5-step Setup Wizard with Plex OAuth sign-in (no manual token copying)
- Settings page with live save to `/config/settings.json`
- Custom access token configuration during setup (Step 5: Security)
- Cron and interval-based job scheduling via APScheduler with SQLAlchemy jobstore
- Multi-library selection for a single job
- Connection status display with server info and detected GPUs
- Browser notifications for job completion/failure
- Radarr/Sonarr webhook integration with debouncing

#### Security

- Rate limiting on auth endpoints via Flask-Limiter
- CSRF protection via Flask-WTF on all state-changing requests
- Path traversal protection, secret file permissions, input sanitization
- Token masking in logs (only last 4 chars shown)

#### Infrastructure

- Production server: gunicorn with gthread workers and simple-websocket for native WebSocket support
- Dedicated `wsgi.py` module for gunicorn deployment
- 530+ tests covering auth, routes, settings, scheduler, workers, media processing, and ETA
- CI pipeline: GitHub Actions for linting (ruff), tests (pytest), and Docker builds
- Pre-commit hooks: ruff check + format run automatically before each commit
- Devcontainer with Python 3.12, FFmpeg, Docker-in-Docker, and Playwright

#### Documentation

- Consolidated end-user documentation around the docs hub, getting started, reference, and guides
- Unraid Community Applications template with networking guidance and icon

### Changed

- `PLEX_URL` and `PLEX_TOKEN` environment variables are now optional (configured via UI)
- Configuration priority: CLI args > settings.json > env vars > defaults
- Web server uses gunicorn + gthread with simple-websocket (replaces Werkzeug dev server)
- SocketIO async mode set to `threading` for compatibility with gthread workers
- Dashboard polls jobs every 5 seconds for real-time updates
- GPU detection results cached to avoid blocking web UI on repeated job starts

### Fixed

- ETA calculation no longer shows misleading "0s" when most items are skipped and remaining items need real processing (stall detection)
- WebSocket connections no longer hang or 500 on page refresh
- CORS configured correctly for LAN access
- Settings manager singleton properly reinitializes with config_dir
- `working_tmp_folder` initialized correctly when running jobs from web UI
- Library filtering passes names (not IDs) to processing pipeline
- Plex OAuth prefers `plex.direct` URLs over local connections
- Dolby Vision / HDR colorspace errors handled gracefully
- FFmpeg version bumped for codec compatibility

---

## [2.7.4] - Previous Release

### Features

- GPU-accelerated BIF file generation (NVIDIA, AMD, Intel, macOS)
- Parallel processing with configurable GPU and CPU workers
- Docker image with GPU support
- Rich console progress display with per-worker FFmpeg stats

---

[Unreleased]: https://github.com/stevezau/plex_generate_vid_previews/compare/v2.7.4...HEAD
```
