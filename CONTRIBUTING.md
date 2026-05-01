# Contributing & Development

Thank you for contributing to Media Preview Generator!

The supported way to run the app in production is **Docker** and the **web UI**. The setup below is for developing and testing the codebase (run the web app locally).

---

## Code of Conduct

Be respectful and inclusive. We're all here to make Plex better.

---

## How to Contribute

### Reporting Bugs

1. Check [existing issues](https://github.com/stevezau/media_preview_generator/issues) first
2. Create a new issue with:
   - Clear description of the problem
   - Steps to reproduce
   - Expected vs actual behavior
   - Environment details (OS, GPU, Docker version)
   - Relevant logs

### Suggesting Features

1. Check [existing issues](https://github.com/stevezau/media_preview_generator/issues) for similar requests
2. Create an issue with label `enhancement`
3. Describe the use case and proposed solution

---

## Development Setup

### Prerequisites

- Python 3.10+
- FFmpeg installed locally (for testing media processing)
- Docker (for container builds)
- Git

### Quick Start

```bash
# Clone and setup
git clone https://github.com/stevezau/media_preview_generator.git
cd media_preview_generator
python -m venv venv
source venv/bin/activate  # Linux/macOS (use .\venv\Scripts\activate on Windows)
pip install -e ".[dev,test]"

# Verify
pytest
ruff check .
```

### Running the Application

```bash
# Web UI with gunicorn (production-like)
gunicorn \
  --bind 0.0.0.0:8080 \
  --worker-class gthread \
  --workers 1 \
  "media_preview_generator.web.wsgi:app"

# Web UI with dev server (Flask reload)
python -m media_preview_generator.web.app
```

---

## Code Style

### Python

- **Formatter/Linter**: Use `ruff format` and `ruff check`
- **Type hints**: Required on function signatures
- **Docstrings**: Google style with Args, Returns, Raises
- **Logging**: Use `loguru`, not stdlib `logging`

```bash
# Check and fix
ruff check . --fix
ruff format .
```

### Pre-commit Hook (recommended)

Install [pre-commit](https://pre-commit.com/) to auto-format on every commit:

```bash
pip install pre-commit
pre-commit install
```

This runs `ruff check --fix` and `ruff format` automatically before each commit, preventing CI lint failures.

### Commit Messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: Add AMD GPU support
fix: Handle missing BIF directory
docs: Update Docker configuration guide
test: Add tests for SettingsManager
```

---

## Testing

```bash
pytest                                          # All tests
pytest --cov=media_preview_generator             # With coverage
pytest tests/test_config.py -v                  # Specific file
pytest -m "not gpu"                             # Skip GPU tests
pytest tests/ --ignore=tests/e2e -x             # Quick tests only
```

### Writing Tests

- Use pytest fixtures from `tests/conftest.py`
- Mock external dependencies (Plex API, FFmpeg)

```python
def test_config_loads_from_env(monkeypatch, mock_config):
    monkeypatch.setenv('PLEX_URL', 'http://test:32400')
    config = load_config(...)
    assert config.plex_url == 'http://test:32400'
```

### Load Testing

A Locust scenario for stress-testing the web API lives in `tests/load/`:

```bash
# Interactive mode — open http://localhost:8089 to configure and start
locust -f tests/load/locustfile.py

# Headless mode
locust -f tests/load/locustfile.py --headless -u 50 -r 10 -t 60s
```

Locust is a dev dependency — install with `pip install -e ".[dev]"`.

---

## Project Structure

```
media_preview_generator/
├── config/               # Config dataclass, paths, validation
├── gpu/                  # GPU discovery + FFmpeg capability probing
├── jobs/                 # Orchestrator, dispatcher, worker pool
├── processing/           # Multi-server dispatcher, FFmpeg runner, HDR
│   ├── multi_server.py     # Path → publishers fan-out (one FFmpeg pass)
│   ├── frame_cache.py      # Cross-server frame reuse cache
│   ├── retry_queue.py      # Slow-backoff retry for not-yet-indexed items
│   ├── plex.py / emby.py / jellyfin.py  # Per-vendor enumeration
│   └── generator.py / ffmpeg_runner.py  # FFmpeg invocation
├── servers/              # Per-vendor server clients
│   ├── plex.py / emby.py / jellyfin.py  # Live MediaServer adapters
│   ├── ownership.py        # Path → owning servers/libraries
│   └── registry.py         # ServerRegistry (loads media_servers[] from settings)
├── output/               # Per-vendor preview format publishers
│   ├── plex_bundle.py      # Plex bundle BIF
│   ├── emby_sidecar.py     # Emby -WIDTH-INTERVAL.bif sidecar
│   ├── jellyfin_trickplay.py  # Jellyfin tile sheets + manifest
│   └── journal.py          # .meta sidecar — fingerprints last publish
├── plex_client.py        # Legacy Plex API client (still used by Plex enum)
├── bif_reader.py         # BIF parsing (used by the viewer)
├── upgrade.py            # Settings migrations / schema upgrades (v1 → v11)
├── utils.py              # Path sanitization, Docker detection, atomic save
├── logging_config.py     # Loguru + Rich console setup
├── version_check.py      # GitHub release version check
└── web/
    ├── wsgi.py              # Gunicorn entry point
    ├── app.py               # App factory, SocketIO init
    ├── auth.py              # Token authentication
    ├── jobs.py              # Job state + SQLite persistence + SocketIO
    ├── settings_manager.py  # Persistent settings (settings.json)
    ├── scheduler.py         # APScheduler cron/interval jobs
    ├── webhooks.py          # Radarr/Sonarr/Plex/custom webhook handlers
    ├── webhook_router.py    # Universal /api/webhooks/incoming dispatcher
    ├── plex_webhook_registration.py  # plex.tv webhook register/unregister
    ├── recent_added_scanner.py  # Periodic recently-added scan helper
    ├── notifications.py     # In-app notification surface
    └── routes/              # Modular HTTP + REST API blueprints
        ├── api_settings.py     # Settings GET/POST + path validators
        ├── api_servers.py      # media_servers CRUD + per-vendor probes
        ├── api_server_auth.py  # Emby/Jellyfin password + Quick Connect
        ├── api_jobs.py         # Job CRUD + worker scaling
        ├── api_schedules.py    # Schedule CRUD
        ├── api_system.py       # /system/status, browse, log history
        ├── api_libraries.py    # Aggregated library list across servers
        ├── api_plex.py         # Plex OAuth PIN flow
        ├── api_plex_webhook.py # Plex Direct webhook register/test
        ├── api_bif.py          # BIF + Jellyfin trickplay viewer
        ├── api_vulkan.py       # Vulkan ICD probing diagnostics
        ├── job_runner.py       # Background job execution thread
        ├── pages.py            # HTML page routes
        └── socketio_handlers.py  # Socket.IO connect/disconnect
```

---

## Key Development Tasks

### Adding a New API Endpoint

1. Add route in the appropriate `web/routes/api_*.py` module:
   ```python
   @api.route('/new-endpoint')
   @api_token_required
   def new_endpoint():
       return jsonify({'data': 'value'})
   ```
2. Add test in `tests/`
3. Update API documentation in `docs/reference.md`

### Adding a Configuration Option

1. Add field to the `Config` dataclass in `config/__init__.py`
2. Add loading / defaulting logic in `load_config()` (also in `config/__init__.py`); path/validation helpers live in `config/paths.py` and `config/validation.py`
3. Add to `web/settings_manager.py` if the option is web-configurable
4. Update `mock_config` fixture in `tests/conftest.py`
5. Document in `docs/reference.md`

---

## Docker Build

```bash
# Build image
docker build -t plex-previews:dev .

# Run development image
docker run --rm -p 8080:8080 -v $(pwd)/config:/config plex-previews:dev

# Multi-architecture build
docker buildx build --platform linux/amd64,linux/arm64 \
  -t stevezzau/media_preview_generator:dev --push .
```

---

## Customizing CI/CD for Forks

The repository has three GitHub Actions workflows:

| Workflow | Purpose | Fork action |
|---|---|---|
| `.github/workflows/ci.yml` | Lint, test, and build/push Docker images for `main`, `dev`, and tags | Change `DOCKER_IMAGE` (line 30) to your Docker Hub namespace |
| `.github/workflows/docker-pr.yml` | Builds per-PR Docker previews | Works as-is |
| `.github/workflows/docker-pr-cleanup.yml` | Removes PR-preview images when PRs close | Works as-is |

Required repository secrets (**Settings → Secrets and variables → Actions**):

| Secret | Used by | Purpose |
|---|---|---|
| `DOCKER_USERNAME` / `DOCKER_PASSWORD` | `ci.yml`, `docker-pr.yml` | Push to Docker Hub |
| `DOCKERHUB_TOKEN` | `ci.yml` (main only) | Sync `DOCKERHUB_README.md` to the Docker Hub description |
| `CODECOV_TOKEN` | `ci.yml` | Upload test coverage to Codecov (drives the README coverage badge) |

---

## Debugging

```bash
LOG_LEVEL=DEBUG python -m media_preview_generator.web.app  # Debug logging
docker exec -it media-preview-generator /bin/bash   # Inspect container
```

Check detected GPUs in the web UI (**Settings** or **Setup**).

---

## Pull Request Process

1. **Before submitting:**
   - All tests pass: `pytest`
   - Code formatted: `ruff format .`
   - No lint errors: `ruff check .`
   - Documentation updated if needed

2. **PR Description:**
   - Clear description of changes
   - Link to related issues
   - Screenshots for UI changes

3. **After merge:**
   - Delete your feature branch
   - Update your fork

---

## Release Process

1. Update version in `media_preview_generator/_version.py`
2. Update `CHANGELOG.md`
3. Create PR and merge to main
4. Tag release: `git tag vX.Y.Z && git push --tags`
5. GitHub Actions builds and pushes Docker image

---

Thank you for contributing! 🎉
