#!/usr/bin/env bash
set -euo pipefail

echo "Installing project in editable mode with dev dependencies..."
pip install -e ".[dev]"

echo "Installing Playwright Chromium browser and OS dependencies..."
playwright install --with-deps chromium

echo "Activating pre-commit git hooks..."
pre-commit install

echo "Verifying key tools..."
python --version
ffmpeg -version | head -1
mediainfo --version | head -1
ruff --version
pytest --version
pre-commit --version
playwright --version
locust --version

echo ""
echo "Dev container ready. Run 'pytest' to test or 'plex-generate-previews --help' to start."
