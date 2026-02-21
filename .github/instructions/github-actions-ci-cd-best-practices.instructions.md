---
applyTo: '.github/workflows/*.yml,.github/workflows/*.yaml'
description: 'GitHub Actions CI/CD best practices'
---

# GitHub Actions Best Practices

## Workflow Structure

- Use descriptive workflow and job names; use granular triggers (`on: push: branches: [main]`)
- Set `concurrency` to prevent duplicate runs; use `workflow_dispatch` for manual triggers
- Set explicit `permissions` with least privilege (default `contents: read`)

## Jobs & Steps

- One logical phase per job (build, test, deploy); use `needs` for dependencies
- Pin actions to SHA or major version tag (never `main`/`latest`)
- Use `outputs` to pass data between jobs; use `if` conditions for conditional execution
- Name every step descriptively for log readability

## Security

- Store secrets in GitHub Secrets; access via `secrets.<NAME>` — never print to logs
- Use OIDC for cloud authentication instead of long-lived credentials
- Integrate dependency scanning (`dependency-review-action`) and SAST (CodeQL)
- Enable secret scanning; use pre-commit hooks to prevent credential leaks

## Performance

- Cache dependencies (`actions/cache`) with hash-based keys
- Use matrix strategies for multi-version/platform testing
- Upload/download artifacts between jobs instead of rebuilding
- Use `timeout-minutes` on jobs to prevent hangs
