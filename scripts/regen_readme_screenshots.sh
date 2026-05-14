#!/bin/bash
#
# regen_readme_screenshots.sh — regenerate the 5 README screenshots
# (dark mode, sanitized) and offer to prune stray PNGs at repo root.
#
# Usage:
#   bash scripts/regen_readme_screenshots.sh
#
# Run from the repo root. Uses /home/data/.venv/bin/python (shared venv
# per project convention); fall back to `python3` if that's missing.

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

PYTHON=/home/data/.venv/bin/python
if [[ ! -x "$PYTHON" ]]; then
    PYTHON=python3
fi

echo "[regen] using $PYTHON"
echo "[regen] capturing 5 README screenshots into docs/images/..."
"$PYTHON" tests/e2e/snapshots/regen_readme.py --out docs/images/

echo
echo "[regen] checking for stray PNGs at repo root..."
# Glob *.png at repo root only (not under any subdir). `find -maxdepth 1`
# keeps docs/images/ untouched. Also skip any tracked files — those
# belong in git history and should be reviewed manually.
mapfile -t stray < <(find . -maxdepth 1 -type f -name '*.png' -printf '%f\n' | sort)

if [[ ${#stray[@]} -eq 0 ]]; then
    echo "[regen] no stray PNGs at repo root. Done."
    exit 0
fi

tracked=()
untracked=()
for f in "${stray[@]}"; do
    if git ls-files --error-unmatch "$f" >/dev/null 2>&1; then
        tracked+=("$f")
    else
        untracked+=("$f")
    fi
done

if [[ ${#tracked[@]} -gt 0 ]]; then
    echo "[regen] WARNING: these tracked PNGs live at repo root (leaving alone):"
    printf '  %s\n' "${tracked[@]}"
fi

if [[ ${#untracked[@]} -eq 0 ]]; then
    echo "[regen] no untracked stray PNGs to remove. Done."
    exit 0
fi

echo
echo "[regen] ${#untracked[@]} untracked PNG(s) at repo root (sample):"
printf '  %s\n' "${untracked[@]:0:10}"
if [[ ${#untracked[@]} -gt 10 ]]; then
    echo "  ... and $((${#untracked[@]} - 10)) more"
fi

echo
read -r -p "[regen] Delete these ${#untracked[@]} untracked PNGs? [y/N] " reply
if [[ "$reply" != "y" && "$reply" != "Y" ]]; then
    echo "[regen] skipped cleanup."
    exit 0
fi

for f in "${untracked[@]}"; do
    rm -- "$f"
done

echo "[regen] removed ${#untracked[@]} PNGs. Done."
