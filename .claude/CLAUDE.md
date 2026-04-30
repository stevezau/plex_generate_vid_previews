# Media Preview Generator

GPU-accelerated video preview thumbnail (BIF) generation for Plex Media Server. Uses FFmpeg hardware decoding (CUDA, VAAPI, QSV, VideoToolbox) and parallel workers. Docker-only deployment with a web UI as the sole interface (no CLI).

## Commands

```bash
# Install
pip install -e ".[dev]"

# Run (web UI)
gunicorn media_preview_generator.web.wsgi:app --bind 0.0.0.0:8080 --worker-class gthread --workers 1

# Test — default runs parallel (xdist), excludes gpu + e2e, keeps coverage
pytest                                          # ~5s, 1321 tests, ~79% cov
pytest --no-cov tests/test_config.py            # Single file, skip coverage
pytest -m e2e -n 0 --no-cov                     # Run Playwright e2e serially
pytest -n 0                                     # Serial mode (for debugging)

# Lint and format
ruff check . --fix
ruff format .

# Docker
docker build -t plex-previews:dev .
```

## Architecture

```
media_preview_generator/
├── config.py              # @dataclass Config, loads from settings.json
├── plex_client.py         # Plex API: library queries, path resolution, retry
├── worker.py              # ThreadPool workers, GPU task assignment
├── media_processing.py    # FFmpeg execution, BIF generation, HDR detection
├── processing.py          # Job orchestration
├── gpu_detection.py       # GPU discovery (NVIDIA/AMD/Intel/Apple)
├── bif_reader.py          # BIF file parsing for web viewer
├── utils.py               # Path sanitization, Docker detection
├── logging_config.py      # Loguru + Rich console setup
├── version_check.py       # GitHub release version checking
├── upgrade.py             # Settings migration / schema upgrades
└── web/
    ├── wsgi.py            # Gunicorn entry point
    ├── app.py             # App factory, SocketIO init (async_mode=threading)
    ├── auth.py            # Token authentication (@login_required, @api_token_required)
    ├── jobs.py            # Job state management + SocketIO events
    ├── settings_manager.py# settings.json persistence, env migration, gpu_config
    ├── scheduler.py       # APScheduler with SQLAlchemy jobstore
    ├── webhooks.py        # Radarr/Sonarr/Tdarr webhook handlers
    ├── routes/            # Modular API routes (api_bif, api_jobs, api_plex, api_schedules, api_settings, api_system, job_runner, pages)
    ├── templates/         # Jinja2 HTML (base, index, settings, setup, login, logs, bif_viewer, webhooks)
    └── static/            # CSS, JS, images
```

**Data flow**: Web UI -> `settings_manager` (settings.json) -> `load_config()` -> `job_runner` builds workers from `gpu_config` -> `WorkerPool` -> `process_item()` -> FFmpeg -> BIF

## Code Style

- **Formatter/Linter**: `ruff format` and `ruff check` (config in pyproject.toml)
- **Imports**: stdlib -> third-party -> local (relative imports within package)
- **Type hints**: Required on function parameters and return types
- **Docstrings**: Google-style with Args, Returns, Raises sections
- **Logging**: `from loguru import logger` (never stdlib `logging`)
- **Max line length**: 120 chars

## Conventions

- **Configuration**: `settings.json` is the sole source of truth. Env vars are one-time seed values migrated on first start. Infrastructure vars (`CONFIG_DIR`, `WEB_PORT`, `PUID`, `PGID`, `TZ`, `CORS_ORIGINS`) remain active.
- **GPU config**: Per-GPU in settings (`gpu_config`: enabled, workers, ffmpeg_threads per device).
- **Error handling**: Custom exceptions + `retry_plex_call()` with backoff for Plex API. `CodecNotSupportedError` for FFmpeg fallback.
- **Commits**: Follow Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).
- **Docker awareness**: Check `utils.is_docker_environment()` for container-specific behavior.

## Security

- Never log Plex tokens. Tokens come via `PLEX_TOKEN` env var.
- Web endpoints use `@login_required` or `@api_token_required` decorators.
- File paths sanitized via `utils.sanitize_path()` and `_safe_resolve_within()`.
- Media paths are read-only; only write to Plex config directories.

## BIF File Format

BIF (Base Index Frame) is Roku's format for video preview thumbnails, also used by Plex.

```
Header (64 bytes): Magic (8) + Version uint32 (4) + Image count uint32 (4) + Frame interval ms uint32 (4) + Reserved (44)
Index table: 8 bytes per image (timestamp uint32 + offset uint32) + 8-byte terminator (0xffffffff + final offset)
Image data: Concatenated JPEG files
```

Generation: FFmpeg extracts frames -> numbered `.jpg` files -> `generate_bif()` packs into `.bif`
Output: `{plex_config}/Media/localhost/{hash}/Indexes/index-sd.bif`

## Key Dependencies

Python >=3.10 | Flask 3.x | Flask-SocketIO | plexapi | loguru | APScheduler 3.x | SQLAlchemy 2.x | gunicorn | pymediainfo | requests

## Test Fixtures

Use mocks from `tests/conftest.py`: `mock_config`, `mock_plex_server`, `tmp_path` (pytest built-in). External dependencies (Plex API, FFmpeg, filesystem) must be mocked. See `.claude/rules/testing.md` for patterns.
