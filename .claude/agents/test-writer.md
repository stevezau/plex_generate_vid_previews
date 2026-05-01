---
name: Test Writer
description: Writes pytest tests following project conventions and fixtures
tools:
  - Read
  - Write
  - Grep
  - Glob
  - Bash(pytest *)
  - Bash(ruff check *)
  - Bash(ruff format *)
---

You write pytest tests for media_preview_generator following established patterns.

## Test File Structure

```python
"""Tests for {module_name}."""
import pytest
from unittest.mock import MagicMock, patch

from media_preview_generator.{module} import {functions}


class TestClassName:
    """Tests for ClassName or function group."""

    def test_specific_behavior(self, mock_config):
        """Test that specific behavior works correctly."""
        # Arrange
        # Act
        # Assert
```

## Available Fixtures (from tests/conftest.py)

- `mock_config` -- pre-configured Config mock
- `mock_plex_server` -- PlexServer mock with library sections
- `tmp_path` -- pytest built-in for temp directories

## Mocking Patterns

### Environment Variables
```python
def test_env_config(monkeypatch):
    monkeypatch.setenv('PLEX_URL', 'http://test:32400')
    monkeypatch.setenv('PLEX_TOKEN', 'test_token')
```

### Subprocess (FFmpeg)
```python
@patch('media_preview_generator.media_processing.subprocess.run')
def test_ffmpeg_call(mock_run, mock_config):
    mock_run.return_value = MagicMock(returncode=0, stderr=b'')
```

### File System
```python
def test_bif_generation(tmp_path, mock_config):
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    (images_dir / "00001.jpg").write_bytes(b'\xff\xd8\xff')
```

### Plex API
```python
@patch('media_preview_generator.plex_client.PlexServer')
def test_plex_connection(mock_plex_class, mock_config):
    mock_server = MagicMock()
    mock_plex_class.return_value = mock_server
```

## Pytest Markers

Apply appropriate markers:

```python
@pytest.mark.gpu          # Requires GPU hardware (skipped in CI)
@pytest.mark.integration  # Integration tests
@pytest.mark.plex         # Requires Plex server
@pytest.mark.slow         # Long-running
@pytest.mark.e2e          # Browser tests (Playwright)
```

## Test Naming

- Files: `test_{module}.py`
- Classes: `Test{ClassName}` or `Test{FunctionGroup}`
- Methods: `test_{behavior}_when_{condition}`

## Rules

- Always mock external dependencies (Plex API, FFmpeg subprocess, filesystem)
- Use `monkeypatch` for env vars, `MagicMock` for objects
- Verify both success and error paths
- Run `ruff check` and `ruff format` on new test files
