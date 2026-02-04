# Contributing

Thank you for considering contributing to Plex Generate Previews!

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

### Submitting Code

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Make your changes
4. Run tests: `pytest`
5. Run linting: `ruff check .`
6. Commit with clear message: `git commit -m "feat: Add my feature"`
7. Push to your fork: `git push origin feature/my-feature`
8. Open a Pull Request

---

## Development Setup

See [DEVELOPMENT.md](DEVELOPMENT.md) for detailed setup instructions.

Quick start:
```bash
git clone https://github.com/stevezau/plex_generate_vid_previews.git
cd plex_generate_vid_previews
python -m venv venv
source venv/bin/activate
pip install -e ".[dev,test]"
pytest
```

---

## Code Style

### Python

- Follow PEP 8
- Use type hints for function signatures
- Write docstrings for public functions (Google style)
- Keep functions focused and small
- Use `loguru` for logging, not stdlib `logging`

```python
def process_item(
    item_key: str,
    config: Config,
    plex: PlexServer
) -> bool:
    """
    Process a single media item.
    
    Args:
        item_key: Plex library item key
        config: Application configuration
        plex: Plex server connection
        
    Returns:
        True if processing succeeded
    """
    ...
```

### JavaScript

- Use ES6+ features
- Use `const` by default, `let` when needed
- Add JSDoc comments for classes and functions

```javascript
/**
 * Manages Plex OAuth authentication flow.
 */
class PlexAuth {
    /**
     * Start the OAuth flow.
     * @param {string} clientId - Client identifier
     * @returns {Promise<string>} Auth token
     */
    async startFlow(clientId) {
        ...
    }
}
```

### Commit Messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

- `feat:` New feature
- `fix:` Bug fix
- `docs:` Documentation
- `test:` Tests
- `refactor:` Code refactoring
- `chore:` Maintenance

Examples:
```
feat: Add AMD GPU support
fix: Handle missing BIF directory
docs: Update Docker configuration guide
test: Add tests for SettingsManager
```

---

## Testing

### Running Tests

```bash
# All tests
pytest

# With coverage
pytest --cov=plex_generate_previews

# Specific file
pytest tests/test_config.py -v

# Skip GPU tests
pytest -m "not gpu"
```

### Writing Tests

- Put tests in `tests/` directory
- Name test files `test_*.py`
- Use pytest fixtures from `conftest.py`
- Mock external dependencies (Plex API, FFmpeg)

```python
def test_config_loads_from_env(monkeypatch):
    """Test that config loads from environment variables."""
    monkeypatch.setenv('PLEX_URL', 'http://test:32400')
    monkeypatch.setenv('PLEX_TOKEN', 'test-token')
    
    config = load_config(...)
    
    assert config.plex_url == 'http://test:32400'
```

---

## Pull Request Process

1. **Before submitting:**
   - All tests pass locally
   - Code is formatted (`ruff format .`)
   - No linting errors (`ruff check .`)
   - Documentation updated if needed

2. **PR Description:**
   - Clear description of changes
   - Link to related issues
   - Screenshots for UI changes

3. **Review:**
   - Address reviewer feedback
   - Keep PR focused on one thing
   - Squash commits if requested

4. **After merge:**
   - Delete your feature branch
   - Update your fork

---

## Release Process

Releases are managed by maintainers:

1. Update version in `plex_generate_previews/_version.py`
2. Update `CHANGELOG.md`
3. Create release PR
4. After merge, tag: `git tag vX.Y.Z`
5. Push tag: `git push --tags`
6. GitHub Actions builds Docker image

---

## Getting Help

- Open an issue for questions
- Check existing documentation
- Look at similar issues/PRs for examples

---

Thank you for contributing! 🎉
