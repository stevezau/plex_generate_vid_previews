---
description: 'Keep documentation in sync with code changes'
applyTo: '**/*.{py,md}'
---

# Update Documentation on Code Change

When modifying code, check if documentation needs updating:

## Trigger Conditions

Update docs when: new features are added, APIs change, breaking changes occur, dependencies/requirements change, config options or env vars are modified, CLI commands change, or code examples become outdated.

## What to Update

- **README.md**: Features list, installation steps, CLI usage, config examples
- **API & config docs** (docs/reference.md): Endpoint signatures, request/response examples, env vars, config options, defaults
- **Guides** (docs/guides.md): Web interface, webhooks, FAQ, troubleshooting
- **Code examples**: Verify snippets still work after signature changes; update imports

## Breaking Changes

Document what changed, provide before/after examples, and include migration steps.
