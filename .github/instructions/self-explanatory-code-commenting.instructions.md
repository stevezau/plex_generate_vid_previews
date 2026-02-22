---
description: 'Write self-explanatory code with minimal comments'
applyTo: '**'
---

# Commenting Guidelines

Write code that speaks for itself. Before adding a comment, ask:

1. Would a better variable/function name eliminate the need? → Refactor instead
2. Does this explain **WHY**, not **WHAT**? → Good comment
3. Is the code self-explanatory without it? → Skip the comment

**Do comment**: complex business logic, regex patterns, API constraints/gotchas, non-obvious algorithm choices, and annotations (TODO, FIXME, HACK with context).

**Don't comment**: obvious operations, code that restates what the code does, changelog history, commented-out code, or decorative dividers.
