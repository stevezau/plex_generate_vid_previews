---
description: 'Prevent terminal heredoc file corruption'
applyTo: '**'
---

# No Heredoc File Operations

NEVER use `cat`, `echo`, `printf`, `tee`, or `>>`/`>` with heredoc (`<<`) or multi-line strings to create or modify files in the terminal. These corrupt files in VS Code's terminal integration.

Use file creation/editing tools instead. Terminal is allowed for package management, builds, tests, git, and running scripts.
