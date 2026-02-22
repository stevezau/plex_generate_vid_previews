---
description: 'Python coding conventions'
applyTo: '**/*.py'
---

# Python Conventions

- Follow PEP 8; use `ruff format` and `ruff check` as the project standard
- Use type hints on all function parameters and return types
- Use `typing` module annotations (e.g., `list[str]`, `dict[str, int]`)
- Write Google-style docstrings with Args, Returns, Raises sections for public APIs
- Use descriptive function/variable names; break complex functions into smaller ones
- Handle edge cases explicitly with clear exception handling — no silent failures
- 4-space indentation, max 120 char lines (per project ruff config)
