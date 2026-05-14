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
@patch('media_preview_generator.media_processing.subprocess.run')
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
@patch('media_preview_generator.plex_client.PlexServer')
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

## Asserting boundary calls

When a test mocks a downstream call, **assert the kwargs the SUT controls — not just that the call happened**. The dispatcher → `process_canonical_path` regression that shipped to production (D34, job d9918149) hid for months because the test only checked `kwargs["canonical_path"]` and ignored `kwargs["server_id_filter"]`. The buggy code still called `process_canonical_path` once per item — the call count was right; the *arguments* were wrong.

```python
# BAD — bug-blind: passes regardless of which server_id_filter was forwarded
mock_process.assert_called_once()

# GOOD — asserts the contract the SUT is responsible for
call = mock_process.call_args
assert call.kwargs["canonical_path"] == "/data/x.mkv"
assert call.kwargs["server_id_filter"] == "plex-default"
```

Rule of thumb: if removing a parameter from the SUT wouldn't break the test, the test isn't covering that parameter.

## Cover the matrix, not one cell

When a function branches on the type/state of an input (originator type, auth method, vendor, retry stage), write tests for **every cell** that produces different downstream behavior — not just the one happy path. The D34 dispatcher tested "Jellyfin pin" but not "Plex pin"; the buggy `if server_cfg.type is not ServerType.PLEX` branch was the one that mattered, and it had zero coverage.

Quick checklist when writing tests for a branchy function:
- List every distinct value the branching variable takes (ServerType.PLEX, .EMBY, .JELLYFIN; `pin=None` vs `pin="x"`; `retry_attempt=0` vs `>0`).
- Multiply: that's your test matrix. Most cells need a row.
- If a row would just duplicate another, add a one-liner explaining *why* they collapse — otherwise write it.
