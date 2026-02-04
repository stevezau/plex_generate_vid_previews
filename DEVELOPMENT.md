# Development Guide

> [Back to Docs](docs/README.md)

Guide for setting up a development environment and contributing to the project.

---

## Prerequisites

- Python 3.10+
- FFmpeg installed locally (for testing media processing)
- Docker (for container builds)
- Git

---

## Development Setup

### 1. Clone the Repository

```bash
git clone https://github.com/stevezau/plex_generate_vid_previews.git
cd plex_generate_vid_previews
```

### 2. Create Virtual Environment

```bash
python -m venv venv
source venv/bin/activate  # Linux/macOS
# or
.\venv\Scripts\activate   # Windows
```

### 3. Install Development Dependencies

```bash
pip install -e ".[dev,test]"
```

### 4. Verify Installation

```bash
# Run tests
pytest

# Check code style
ruff check .

# Run the CLI
plex-generate-previews --help
```

---

## Running the Application

### Web Mode (Default)

```bash
# Start web server on port 8080
python -m plex_generate_previews

# Or with environment variables
PLEX_URL=http://localhost:32400 PLEX_TOKEN=your-token python -m plex_generate_previews
```

### CLI Mode

```bash
plex-generate-previews --cli \
  --plex-url http://localhost:32400 \
  --plex-token your-token \
  --plex-config-folder "/path/to/plex/config"
```

---

## Running Tests

### All Tests

```bash
pytest
```

### With Coverage

```bash
pytest --cov=plex_generate_previews --cov-report=html
# Open htmlcov/index.html in browser
```

### Specific Test File

```bash
pytest tests/test_config.py -v
```

### Skip GPU Tests (CI/No GPU)

```bash
pytest -m "not gpu"
```

### Run Only Quick Tests

```bash
pytest tests/ --ignore=tests/e2e -x
```

---

## Code Style

### Linting

```bash
# Check for issues
ruff check .

# Auto-fix issues
ruff check . --fix
```

### Formatting

```bash
# Check formatting
ruff format --check .

# Apply formatting
ruff format .
```

### Type Checking

```bash
# Install mypy if needed
pip install mypy

# Run type checks
mypy plex_generate_previews/
```

---

## Docker Build

### Build Image

```bash
docker build -t plex-previews:dev .
```

### Run Development Image

```bash
docker run --rm -p 8080:8080 \
  -v $(pwd)/config:/config \
  plex-previews:dev
```

### Multi-Architecture Build

```bash
docker buildx build --platform linux/amd64,linux/arm64 \
  -t stevezzau/plex_generate_vid_previews:dev \
  --push .
```

---

## Project Structure

```
plex_generate_vid_previews/
├── plex_generate_previews/     # Main Python package
│   ├── __init__.py
│   ├── __main__.py            # Entry point
│   ├── cli.py                 # CLI argument parsing
│   ├── config.py              # Configuration management
│   ├── worker.py              # Worker pool
│   ├── media_processing.py    # FFmpeg operations
│   ├── plex_client.py         # Plex API client
│   ├── gpu_detection.py       # GPU discovery
│   └── web/                   # Flask web app
│       ├── app.py             # App factory
│       ├── routes.py          # HTTP routes
│       ├── auth.py            # Authentication
│       ├── settings_manager.py# Settings persistence
│       ├── jobs.py            # Job management
│       ├── scheduler.py       # Job scheduling
│       ├── static/            # CSS, JS
│       └── templates/         # Jinja2 templates
├── tests/                     # Test suite
│   ├── conftest.py            # Shared fixtures
│   ├── test_*.py              # Unit tests
│   └── fixtures/              # Test data
├── docs/                      # Documentation
├── Dockerfile                 # Container image
├── pyproject.toml             # Package config
└── pytest.ini                 # Test config
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

2. Add test in `tests/test_oauth_routes.py` or create new test file

3. Update API documentation in `docs/API.md`

### Adding a Configuration Option

1. Add field to `Config` dataclass in `config.py`
2. Add loading logic in `load_config()`
3. Add to `SettingsManager` if web-configurable
4. Update `mock_config` fixture in `tests/conftest.py`
5. Document in `docs/configuration.md`

### Adding GPU Support

1. Add detection in `gpu_detection.py`
2. Add encoder selection in `media_processing.py`
3. Add tests with GPU marker: `@pytest.mark.gpu`

---

## Debugging Tips

### Enable Debug Logging

```bash
LOG_LEVEL=DEBUG python -m plex_generate_previews
```

### Flask Debug Mode

```bash
DEBUG=true python -m plex_generate_previews
```

### Check GPU Detection

```bash
plex-generate-previews --list-gpus
```

### Inspect Docker Container

```bash
docker exec -it plex-generate-previews /bin/bash
```

---

## Release Process

1. Update version in `plex_generate_previews/_version.py`
2. Update `CHANGELOG.md`
3. Create PR and merge to main
4. Tag release: `git tag vX.Y.Z && git push --tags`
5. GitHub Actions builds and pushes Docker image

---

[Back to Main README](README.md)
