---
description: 'Shell scripting conventions'
applyTo: '**/*.sh'
---

# Shell Scripting Guidelines

- Start with `#!/bin/bash` and `set -euo pipefail`
- Double-quote all variable references: `"$var"`
- Use `[[ ]]` for conditionals, `$()` for command substitution
- Use `trap` for cleanup on exit; use `mktemp` for temp files
- Use `jq`/`yq` for structured data — avoid ad-hoc `grep`/`awk` parsing of JSON/YAML
- Define defaults at top, use functions for reusable logic, validate required params early
- Use `readonly` for constants; keep scripts clean and concise
