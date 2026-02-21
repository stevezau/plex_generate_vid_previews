---
description: 'Code review guidelines'
applyTo: '**/*.py'
---

# Code Review Guidelines

## Priority Levels

- **CRITICAL (block merge)**: Security vulnerabilities, exposed secrets, logic errors, data corruption, race conditions, breaking API changes
- **IMPORTANT (discuss)**: Missing tests for critical paths, N+1 queries, memory leaks, SOLID violations, architecture deviations
- **SUGGESTION (non-blocking)**: Naming improvements, minor optimizations, documentation gaps

## Review Checklist

- No sensitive data (tokens, keys, PII) in code or logs
- All user inputs validated and sanitized
- Parameterized queries only (no string concatenation for SQL)
- Proper error handling with meaningful messages — no silent failures
- New functionality has test coverage including edge cases
- No obvious performance issues (N+1, unbounded queries, missing pagination)
- Follows established codebase patterns and conventions
- Resource cleanup (connections, files, streams) handled properly

## Comment Format

```
**[PRIORITY] Category: Title**
Description of issue and impact.
**Suggested fix:** [code if applicable]
```
