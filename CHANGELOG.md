# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added

- **Multi-server support (Plex + Emby + Jellyfin)** — any combination of servers can be configured under Settings → Media Servers. The dispatcher runs FFmpeg once per file and publishes the right preview format to every server that owns it: Plex bundle BIF, Emby `-WIDTH-INTERVAL.bif` sidecar, or Jellyfin trickplay tile sheets + manifest. Per-server library filtering, path mappings, exclude rules, and credentials are all stored on the server entry — no global single-Plex assumption left.
- **Universal webhook router** (`POST /api/webhooks/incoming`) auto-detects Plex / Emby / Jellyfin / Radarr / Sonarr / generic `{path: ...}` payloads and dispatches to the right owners. Per-server URLs (`POST /api/webhooks/server/<id>`) pin a webhook to one configured server.
- **Plex Direct Webhook (Plex Pass) registration** — register/unregister this app's webhook URL with plex.tv directly from the Servers page. Per-server registration: each Plex card carries its own webhook URL.
- **Frame reuse cache** — when a webhook for a file fires for a sibling server within the configured TTL, frames extracted by FFmpeg are reused without re-running. Tunable under Settings → Performance (`enabled`, `ttl_minutes`, `max_cache_disk_mb`; defaults: enabled / 60 min / 2 GB).
- **Slow-backoff retry queue** — files where the source server says "not yet indexed" are retried on a geometric backoff (30s → 2m → 5m → 15m → 60m) instead of dropping the webhook.
- **Multi-Plex same-vendor support** — multiple Plex servers can be configured side by side; webhooks are routed by `server_identity` (Plex `clientIdentifier` / Emby/Jellyfin `ServerId`) so the right server's bundle path is updated.
- **Jellyfin trickplay one-click fix** — Jellyfin libraries default `EnableTrickplayImageExtraction` to false, which silently hides this app's published manifests. The Servers page surfaces a "Fix it for me" button when any library is mis-configured.
- **Cross-server BIF / trickplay viewer** — the BIF viewer reads any of the three vendors' formats so you can verify "did Plex get the bundle, did Emby get the sidecar, did Jellyfin get the tile sheets" from one UI.
- **Setup wizard vendor picker (step 1)** — choose Plex (OAuth), Emby (password / API key), or Jellyfin (Quick Connect / password / API key). Plex stays the recommended default; the wizard skips through to the Servers page for non-Plex installs.
- **Schema migrations v7 → v11** (`upgrade.py`) — synthesise `media_servers` from legacy flat `plex_*` keys (v7), move global `path_mappings` / `exclude_paths` into the per-server entry (v8), dedupe rows from the v7+v8 double-copy bug (v9), rewrite legacy Plex Direct webhook URL `/plex` → `/incoming` and drop the per-server `webhook_secret` key (v10), and seed the `frame_reuse` block with sane defaults (v11).
- **Job storage moved to SQLite** (Phase J8) — jobs persist as one row per job in `jobs.db` instead of rewriting the whole `jobs.json` on every change. Per-row upserts keep schema drift on free-form fields (progress, config, publishers) from wiping job history.
- **Schema downgrade refusal** — refuses to start when `settings.json` was written by a newer schema version than the running binary, instead of silently dropping unknown fields on the next save (the failure mode that wiped a tester's job history during a tag-drift incident).

### Changed

- The dedicated CPU-fallback worker pool was removed. When a GPU worker hits `CodecNotSupportedError`, it now retries the same item on CPU in-place inside the GPU worker. Existing `cpu_fallback_threads` settings are folded into `cpu_threads` automatically on upgrade (schema v5).
- The `recently_added_*` settings keys are migrated into real Schedule entries on first start (schema v4); the obsolete `system_recently_added_scan` APScheduler job is removed.

### Fixed

- **Multi-GPU NVIDIA detection in Docker (#221)** — containers running the NVIDIA Container Toolkit deliberately omit `/dev/dri/renderD*` nodes, so DRM enumeration saw zero GPUs and the nvidia-smi fallback only ever registered one card. Even on bare-metal hosts, every NVIDIA GPU collapsed onto a single `"cuda"` device path in `gpu_config`. NVIDIA GPUs are now enumerated primarily via `nvidia-smi` (one entry per card, keyed as `cuda:0`, `cuda:1`, …) and each card is independently CUDA-tested and dispatched with FFmpeg's `-hwaccel_device` flag. Legacy generic `"cuda"` gpu_config entries are stripped automatically on upgrade (schema v6).

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

- CLI mode (`--cli`, `media-preview-generator` CLI entry point, `cli.py`)
- `__main__.py` module (standalone execution)
- `pytest` from pre-push hooks
- Dedicated CPU-fallback worker pool — GPU workers now retry on CPU in-place

### Migrating from 3.4.x

- **`--cli` flag is gone.** 3.5 is web-only. Configure everything through the Setup Wizard and the Settings page. Existing env vars (`PLEX_URL`, `PLEX_TOKEN`, `CPU_THREADS`, …) are migrated into `settings.json` on first start; after that, `settings.json` is the source of truth.
- **`CPU_FALLBACK_WORKERS` / "CPU Fallback Workers" setting is gone.** CPU fallback is now automatic: when a GPU worker hits an unsupported codec or decoder error, the same worker retries on CPU in-place. If you want more dedicated CPU concurrency for files that never decode on the GPU, raise **CPU Workers** (previously you would have configured a separate fallback pool).
- **Plex generation setting.** For best results, set Plex **Library → Generate video preview thumbnails** to **Never** so this tool is the only source of BIFs.
- **First boot after upgrade.** The `CONFIG_DIR` volume is re-used; the upgrade routine will migrate settings on startup. No manual steps required.

---

## [3.4.2] - Previous Stable Release

### Features

- GPU-accelerated BIF file generation (NVIDIA, AMD, Intel, macOS)
- Parallel processing with configurable GPU and CPU workers
- Docker image with GPU support
- Web UI dashboard with job management

---

[Unreleased]: https://github.com/stevezau/media_preview_generator/compare/3.5.0...HEAD
[3.5.0]: https://github.com/stevezau/media_preview_generator/compare/3.4.2...3.5.0
[3.4.2]: https://github.com/stevezau/media_preview_generator/releases/tag/3.4.2
