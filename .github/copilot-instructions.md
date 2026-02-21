# Project Guidelines

Plex Generate Previews - Python tool that generates video preview thumbnails (BIF files) for Plex media libraries using FFmpeg with GPU acceleration support.

## Code Style

- **Formatter/Linter**: Use `ruff format` and `ruff check`
- **Imports**: stdlib → third-party → local (relative imports within package)
- **Type hints**: Required on function parameters and return types
- **Docstrings**: Google-style with Args, Returns, Raises sections
- **Logging**: Use `from loguru import logger` (never stdlib `logging`)

Reference: [cli.py](../plex_generate_previews/cli.py), [config.py](../plex_generate_previews/config.py)

## Architecture

```
plex_generate_previews/
├── cli.py              # Entry point, arg parsing, Rich progress, signal handling
├── config.py           # @dataclass Config, env/CLI loading, validation
├── plex_client.py      # Plex API connection, library queries, retry logic
├── worker.py           # ThreadPool workers, task assignment
├── media_processing.py # FFmpeg execution, BIF generation, HDR detection
├── gpu_detection.py    # GPU detection (NVIDIA/AMD/Intel/Apple)
├── utils.py            # Path sanitization, Docker detection
├── logging_config.py   # Loguru + Rich console setup
├── version_check.py    # PyPI/GitHub version checking
└── web/                # Flask app with SocketIO, auth, scheduler
    ├── wsgi.py         # Gunicorn entry point
    ├── app.py         # App factory, SocketIO init (async_mode=threading)
    ├── routes.py       # HTTP routes + API endpoints
    ├── auth.py         # Token authentication
    ├── jobs.py         # Job state management + SocketIO events
    ├── settings_manager.py # Persistent settings (JSON file)
    ├── scheduler.py    # APScheduler with SQLAlchemy jobstore
    └── webhooks.py     # Radarr/Sonarr webhook handlers
```

**Flow**: `cli.main()` → `load_config()` → `detect_all_gpus()` → `plex_server()` → `WorkerPool` → `process_item()`

## Build and Test

```bash
# Install
pip install -e ".[dev]"

# Run
plex-generate-previews --help
python -m plex_generate_previews

# Test (GPU tests skipped by default)
pytest
pytest --cov=plex_generate_previews --cov-fail-under=70
pytest -m "not gpu"  # Skip GPU tests explicitly

# Lint/Format
ruff check .
ruff format .
```

## Project Conventions

**Configuration priority**: CLI args > settings.json (Web UI) > Environment variables > Defaults
```python
def get_config_value(cli_args, field_name: str, env_key: str, default, value_type: type = str):
```

**Error handling**: Custom exceptions + retry with backoff for network calls
```python
class CodecNotSupportedError(Exception): ...
retry_plex_call(func, *args, max_retries=3, retry_delay=1.0)
```

**Test fixtures**: Use mocks from [tests/conftest.py](../tests/conftest.py) - `mock_config`, `mock_plex_server`
```python
def test_something(mock_config, monkeypatch):
    monkeypatch.setenv('PLEX_URL', 'http://test:32400')
```

**Docker awareness**: Check `utils.is_docker_environment()` for container-specific behavior

## BIF File Format

BIF (Base Index Frame) is Roku's format for video preview thumbnails, also used by Plex. See [media_processing.py](../plex_generate_previews/media_processing.py) `generate_bif()`.

**Structure**:
```
Header (64 bytes):
├── Magic: 0x89 0x42 0x49 0x46 0x0d 0x0a 0x1a 0x0a (8 bytes)
├── Version: uint32 LE (always 0)
├── Image count: uint32 LE
├── Frame interval: uint32 LE (milliseconds, default 5000ms)
└── Reserved: 44 bytes of 0x00

Index table (8 bytes per image + 8 byte terminator):
├── For each image: timestamp (uint32) + offset (uint32)
└── Terminator: 0xffffffff + final offset

Image data:
└── Concatenated JPEG files
```

**Generation flow**: FFmpeg extracts frames → saves as numbered `.jpg` → `generate_bif()` packs into single `.bif`

Output location: `{plex_config}/Media/localhost/{hash}/Indexes/index-sd.bif`

## Integration Points

- **Plex API**: Via `plexapi` library - always wrap in `retry_plex_call()`
- **FFmpeg**: Subprocess calls in `media_processing.py` - handle codec errors gracefully
- **GPU drivers**: NVIDIA (nvenc), Intel (qsv), AMD (vaapi), Apple (videotoolbox)
- **Web UI**: gunicorn + gthread at `:8080`, Flask-SocketIO for real-time updates, APScheduler with SQLAlchemy jobstore for schedules, JSON file for settings persistence

## Security

- **Plex tokens**: Never log, passed via `PLEX_TOKEN` env var
- **Web auth**: Token-based with `@login_required` decorator in [web/auth.py](../plex_generate_previews/web/auth.py)
- **File access**: Media paths read-only, only write to Plex config directories
