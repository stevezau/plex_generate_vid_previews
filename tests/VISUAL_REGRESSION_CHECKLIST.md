# Visual regression checklist — TEST_AUDIT.md Phase 7

This file is the honest acknowledgement that ~10 of the 21 UI bug commits
in the last 3 days are CSS-only / visual issues that **cannot be caught
by jsdom or Playwright DOM tests**. Examples:

- Light mode rounds 1-3 (cards, badges, code chips) — `6a35f26`, `d91f3c9`, `96cbf27`, `361627c`
- Brand orange propagation — `8cfbc56`, `c7232fb`, `d600496`
- Mobile card-stack tables — `1310efc`, `fc93cf6`, `5fa8020`, `235b881`, `dffded9`, `7fd16a4`
- Hover-lift + frosted nav effects

Programmatic tests verify *structure* (does the right element render?
does the JS state map to the right DOM?). They don't verify *appearance*
(does it look right to a human eye?). For CSS-only issues, the safety
net is human review.

## Per-PR review checklist

When a PR touches CSS, templates, or styled components, eyeball every
surface in the table below in **both light AND dark mode** before
merging. Use the canary or a local dev container.

| Surface | Light mode | Dark mode | Mobile (≤640px) | Notes |
|---|---|---|---|---|
| Dashboard `/` | ☐ | ☐ | ☐ | Active jobs cards + Workers panel + KPI tiles |
| Servers `/servers` | ☐ | ☐ | ☐ | Per-server cards + Edit modal + Add Server form |
| Settings `/settings` | ☐ | ☐ | ☐ | Per-section panels + steppers + tooltips |
| Setup wizard `/setup` (each step) | ☐ | ☐ | ☐ | All 5 steps + per-vendor branching |
| Automation `/automation` | ☐ | ☐ | ☐ | Schedules + Webhooks + Triggers tabs |
| Logs `/logs` | ☐ | ☐ | ☐ | Live log streaming + level filter |
| Login `/login` | ☐ | ☐ | ☐ | Token input + branding |
| BIF Viewer `/bif-viewer` | ☐ | ☐ | ☐ | Frame grid + search input |
| Active Jobs / Workers panels | ☐ | ☐ | ☐ | Progress bars + retry-eta + fallback badges |
| Jobs queue table | ☐ | ☐ | ☐ | Sortable columns + per-row actions + mobile card stack |

## Specific things to look for

**Brand consistency:**
- ☐ Brand orange (`#f5a623` or whatever the current value is) applied consistently to primary buttons, badges, headers, links
- ☐ No washed-out greys where brand orange should appear
- ☐ Badge contrast adequate in both light + dark (text readable on background)

**Layout:**
- ☐ Mobile (≤640px): tables render as card stacks, not horizontal-scrolling rows
- ☐ Mobile: page headers don't wrap to 2 lines
- ☐ Mobile: action buttons fit within card boundaries

**Interaction:**
- ☐ Hover-lift effects work on cards and rows where intended
- ☐ Modal dialogs centered + dismissible
- ☐ Steppers (+/- buttons) align with their input fields
- ☐ Tooltips render on hover, not on focus-only

**Light-mode specifics (high-bug area):**
- ☐ Card edges visible against body background (not the same color)
- ☐ Code chips have a tinted background (not invisible against cards)
- ☐ Disabled-state text readable (not invisible grey-on-grey)
- ☐ Active dropdown items distinguishable from hover state

## Why no automation

Adding visual regression automation (Percy, Chromatic, Playwright
screenshot diffs) was considered and deferred:

- Cost: per-run snapshot generation + storage + review UI subscription
- False-positive rate: any pixel-level diff fires (font rendering,
  scrollbar width, antialiasing) — humans must triage
- Maintenance: every legitimate UI change requires baseline updates

For a project this size, **per-PR human eyeball + canary deploy review**
catches the same regressions at much lower operational cost. This
checklist exists to make that review repeatable.

## Future option

If the bug rate in this category gets worse, revisit Playwright
screenshot diffs (`page.screenshot()` + image comparison). Cheapest
incremental step that doesn't require a SaaS subscription.

## Audit + plan reference

- Original audit: `TEST_AUDIT.md`
- Execution plan: `/home/data/.claude/plans/iridescent-churning-bear.md`
- Regression validation table: `tests/REGRESSION_VERIFIED.md`
