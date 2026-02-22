# Contributing & Development

Thank you for contributing to Plex Generate Previews!

---

## Code of Conduct

Be respectful and inclusive. We're all here to make Plex better.

---

## How to Contribute

### Reporting Bugs

1. Check [existing issues](https://github.com/stevezau/plex_generate_vid_previews/issues) first
2. Create a new issue with:
   - Clear description of the problem
   - Steps to reproduce
   - Expected vs actual behavior
   - Environment details (OS, GPU, Docker version)
   - Relevant logs

### Suggesting Features

1. Check [existing issues](https://github.com/stevezau/plex_generate_vid_previews/issues) for similar requests
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
git clone https://github.com/stevezau/plex_generate_vid_previews.git
cd plex_generate_vid_previews
python -m venv venv
source venv/bin/activate  # Linux/macOS (use .\venv\Scripts\activate on Windows)
pip install -e ".[dev,test]"

# Verify
pytest
ruff check .
plex-generate-previews --help
```

### Running the Application

```bash
# Web mode with gunicorn (production-like)
gunicorn \
  --bind 0.0.0.0:8080 \
  --worker-class gthread \
  --threads 4 \
  --workers 1 \
  "plex_generate_previews.web.wsgi:app"

# Web mode with dev server
python -m plex_generate_previews

# CLI mode
plex-generate-previews --cli \
  --plex-url http://localhost:32400 \
  --plex-token your-token \
  --plex-config-folder "/path/to/plex/config"
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
pytest --cov=plex_generate_previews             # With coverage
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

---

## Project Structure

```
plex_generate_previews/
├── cli.py                 # CLI argument parsing, Rich progress
├── config.py              # Configuration management
├── worker.py              # Thread pool workers
├── media_processing.py    # FFmpeg, BIF generation
├── plex_client.py         # Plex API client
├── gpu_detection.py       # GPU discovery
├── utils.py               # Path sanitization, Docker detection
├── logging_config.py      # Loguru + Rich console setup
├── version_check.py       # PyPI/GitHub version checking
└── web/                   # Flask web app
    ├── wsgi.py            # Gunicorn entry point
    ├── app.py             # App factory, SocketIO init
    ├── routes.py          # HTTP routes + API endpoints
    ├── auth.py            # Token authentication
    ├── jobs.py            # Job state management + SocketIO events
    ├── settings_manager.py# Persistent settings (JSON)
    └── scheduler.py       # APScheduler cron/interval jobs
```

---

## Key Development Tasks

### Adding a New API Endpoint

1. Add route in `web/routes.py`:
   ```python
   @api.route('/new-endpoint')
   @api_token_required
   def new_endpoint():
       return jsonify({'data': 'value'})
   ```
2. Add test in `tests/`
3. Update API documentation in `docs/reference.md`

### Adding a Configuration Option

1. Add field to `Config` dataclass in `config.py`
2. Add loading logic in `load_config()`
3. Add to `SettingsManager` if web-configurable
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
  -t stevezzau/plex_generate_vid_previews:dev --push .
```

---

## Customizing CI/CD for Forks

### docker-publish.yml

Change the `DOCKER_IMAGE` env var (line 11) to your Docker Hub image name. Set `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` as repository secrets in **GitHub Settings → Secrets and variables → Actions**.

### test.yml

Works as-is for any fork — no changes needed.

> [!NOTE]
> `docker-publish.yml` triggers after the "Tests" workflow succeeds on `main`. The `workflows: [Tests]` name must match the `name:` in `test.yml`. For Codecov coverage reporting, set the `CODECOV_TOKEN` repository secret.

---

## Development Container

The project includes a devcontainer configuration for VS Code and GitHub Codespaces with Python 3.12, FFmpeg, Docker-in-Docker, pre-commit hooks, and Playwright.

See [Development Environment](docs/getting-started.md#development-environment) for full details.

---

## Debugging

```bash
LOG_LEVEL=DEBUG python -m plex_generate_previews  # Debug logging
plex-generate-previews --list-gpus                 # Check GPUs
docker exec -it plex-generate-previews /bin/bash   # Inspect container
```

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

1. Update version in `plex_generate_previews/_version.py`
2. Update `CHANGELOG.md`
3. Create PR and merge to main
4. Tag release: `git tag vX.Y.Z && git push --tags`
5. GitHub Actions builds and pushes Docker image

---

Thank you for contributing! 🎉
