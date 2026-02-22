# Testing Skill

Expertise in pytest patterns, fixtures, and testing strategies for this codebase.

## When to Use

- Writing new tests
- Mocking Plex API or FFmpeg
- Understanding test fixtures
- Running specific test categories

## Test Structure

```
tests/
├── conftest.py          # Shared fixtures (mock_config, mock_plex_server)
├── test_*.py            # Unit tests
├── mocks/               # Reusable mock classes
│   ├── mock_plex_server.py
│   └── mock_plex_tv.py
├── fixtures/            # Test data files
│   ├── reference.bif
│   └── plex_responses/  # XML response fixtures
└── e2e/                 # Playwright browser tests
```

## Key Fixtures

From [conftest.py](../../../tests/conftest.py):

```python
@pytest.fixture
def mock_config():
    """Mock Config dataclass with sensible defaults."""
    config = MagicMock()
    config.plex_url = "http://localhost:32400"
    config.plex_token = "test_token_12345"
    config.plex_bif_frame_interval = 5
    return config

@pytest.fixture
def mock_plex_server(mock_config):
    """Mock PlexServer with library sections."""
    # Returns configured mock with .library.sections()
```

## Test Commands

```bash
pytest                                    # All tests (GPU skipped)
pytest -v                                 # Verbose output
pytest tests/test_config.py              # Single file
pytest -k "test_load"                    # Pattern match
pytest --cov=plex_generate_previews      # With coverage
pytest -x                                 # Stop on first failure
pytest -m "not gpu and not e2e"          # Skip markers
```

## Pytest Markers

```python
@pytest.mark.gpu          # Requires GPU hardware
@pytest.mark.integration  # Integration tests
@pytest.mark.plex         # Requires Plex server
@pytest.mark.slow         # Long-running tests
@pytest.mark.e2e          # Browser tests (Playwright)
```

## Mocking Patterns

**Environment variables**:
```python
def test_config(monkeypatch):
    monkeypatch.setenv('PLEX_URL', 'http://test:32400')
```

**File system**:
```python
def test_bif(tmp_path):
    bif_file = tmp_path / "test.bif"
```

**Subprocess (FFmpeg)**:
```python
@patch('subprocess.run')
def test_ffmpeg(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
```

## Writing New Tests

1. Use existing fixtures from `conftest.py`
2. Mock external dependencies (Plex API, FFmpeg, file system)
3. Add appropriate markers for categorization
4. Follow naming: `test_<module>_<behavior>.py`
5. Use `monkeypatch` for env vars, `MagicMock` for objects
