---
globs: "tests/**/*.py"
---

# Testing Conventions

## Structure

- Files: `test_{module}.py`
- Classes: `Test{ClassName}` or `Test{FunctionGroup}`
- Methods: `test_{behavior}_when_{condition}`
- Pattern: Arrange / Act / Assert

## Fixtures (from tests/conftest.py)

- `mock_config` -- pre-configured Config mock with sensible defaults
- `mock_plex_server` -- PlexServer mock with library sections
- `tmp_path` -- pytest built-in for temp directories

## Mocking Patterns

Environment variables:
```python
def test_config(monkeypatch):
    monkeypatch.setenv('PLEX_URL', 'http://test:32400')
```

Subprocess (FFmpeg):
```python
@patch('plex_generate_previews.media_processing.subprocess.run')
def test_ffmpeg(mock_run, mock_config):
    mock_run.return_value = MagicMock(returncode=0, stderr=b'')
```

File system:
```python
def test_bif(tmp_path, mock_config):
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    (images_dir / "00001.jpg").write_bytes(b'\xff\xd8\xff')
```

Plex API:
```python
@patch('plex_generate_previews.plex_client.PlexServer')
def test_plex(mock_plex_class, mock_config):
    mock_server = MagicMock()
    mock_plex_class.return_value = mock_server
```

## Markers

```python
@pytest.mark.gpu          # Requires GPU hardware (skipped in CI)
@pytest.mark.integration  # Integration tests
@pytest.mark.plex         # Requires Plex server
@pytest.mark.slow         # Long-running
@pytest.mark.e2e          # Browser tests (Playwright)
```

## Rules

- External dependencies (Plex API, FFmpeg, filesystem) must always be mocked
- New functionality requires corresponding tests
- Use `monkeypatch` for env vars, `MagicMock` for objects
