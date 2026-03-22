# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

---

## [3.5.0] - 2026-03-22

### Added

#### Web UI

- **BIF Viewer** — browse and inspect generated thumbnail files for any Plex library item; scrub through frames to verify quality without leaving the web UI
- **Log Viewer** — persistent log viewer with history, live streaming, server log-level badge, filtering, copy support, and auto-scroll with live-follow indicator
- **Job Priority** — set priority (high / normal / low) when starting a job or change it on the fly; pending jobs are dispatched in priority order
- **Schedule Editing** — edit existing schedules directly from the web UI
- **External Authentication** — `AUTH_METHOD=external` bypasses built-in login for users behind a reverse proxy or VPN (Authelia, Authentik, Tailscale, etc.)
- **"What's New" viewer** — see release notes for new versions directly inside the dashboard
- **Per-GPU configuration** — enable/disable individual GPUs, set workers and FFmpeg threads per GPU in Settings
- **Settings migration system** — versioned schema upgrades for settings.json; env vars are migrated once on first start, settings.json is the single source of truth afterward

#### Security

- Rate limiting on auth endpoints via Flask-Limiter
- CSRF protection via Flask-WTF on all state-changing requests
- Path traversal protection, secret file permissions, input sanitization
- Token masking in logs (only last 4 chars shown)

#### Infrastructure

- Full-featured dashboard with real-time progress, worker status cards, and job logs
- 5-step Setup Wizard with Plex OAuth sign-in (no manual token copying)
- Cron and interval-based job scheduling via APScheduler
- Radarr/Sonarr/Custom webhook integration with batching and configurable delay
- Production server: gunicorn with gthread workers for WebSocket support
- CI pipeline: GitHub Actions for linting (ruff), tests (pytest), and Docker builds
- Devcontainer with Python 3.12, FFmpeg, Docker-in-Docker, and Playwright

#### Documentation

- Consolidated docs hub with getting started, reference, and guides
- Unraid Community Applications template with networking guidance

### Changed

- **CLI removed** — the web UI is now the only interface; `--cli` flag, CLI entry point, and all CLI-only code have been removed
- **Dolby Vision tone mapping overhauled** — DV Profile 5 routes through libplacebo (Vulkan); DV Profile 7/8 routes through zscale/tonemap using the HDR10 base layer; hardware decode disabled for all DV content to prevent green/corrupted output
- **HDR tone mapping filter chain corrected** — uses `npl=100` (SDR reference white) instead of MaxCLL, `desat=0` for saturated color, and fixes the `tonemap=tonemap=` double-prefix syntax bug
- **NVIDIA CUDA on Windows** — Windows with NVIDIA GPUs now uses CUDA instead of falling back to D3D11VA
- `PLEX_URL` and `PLEX_TOKEN` environment variables are now optional (configured via UI)
- Configuration priority: settings.json > env vars (seed on first start) > defaults
- Web server uses gunicorn + gthread (replaces Werkzeug dev server)
- GPU detection works in containers without `/sys/class/drm` (TrueNAS Scale, Kubernetes)
- GPU workers reconcile live when `gpu_config` changes in Settings
- Job cancellation propagates to FFmpeg subprocesses for immediate cleanup
- Settings and Webhooks pages load significantly faster
- Progress and worker status now visible during the first file of a job
- GPUs with 0 workers are excluded from the active GPU list
- Broadcaster respects the server log level instead of hardcoding DEBUG

### Fixed

- Dark HDR thumbnails caused by brightness-crushing tonemap curve and incorrect npl values
- Intermittent webhook job failures from FFmpeg validation timeout
- Jobs now correctly use Plex Data Path, URL, and token saved in Settings
- ETA calculation no longer shows misleading "0s" when items are skipped
- WebSocket connections no longer hang or 500 on page refresh
- CORS configured correctly for LAN access
- Settings manager singleton properly reinitializes with config_dir
- Library filtering passes names (not IDs) to processing pipeline
- Plex OAuth prefers `plex.direct` URLs over local connections
- Log viewer filter resets when server log level changes

### Removed

- CLI mode (`--cli`, `plex-generate-previews` CLI entry point, `cli.py`)
- `__main__.py` module (standalone execution)
- `pytest` from pre-push hooks

---

## [3.4.2] - Previous Stable Release

### Features

- GPU-accelerated BIF file generation (NVIDIA, AMD, Intel, macOS)
- Parallel processing with configurable GPU and CPU workers
- Docker image with GPU support
- Web UI dashboard with job management

---

[Unreleased]: https://github.com/stevezau/plex_generate_vid_previews/compare/3.5.0...HEAD
[3.5.0]: https://github.com/stevezau/plex_generate_vid_previews/compare/3.4.2...3.5.0
[3.4.2]: https://github.com/stevezau/plex_generate_vid_previews/releases/tag/3.4.2
