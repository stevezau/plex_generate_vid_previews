# Visual regression strategy — decision doc

> **Status:** decision doc + active per-PR template. Replaces the old
> "checklist only" version that was failing in practice.
>
> **TL;DR recommendation:** **Stay manual, but tighten the process.**
> Adopt the per-PR template at the bottom of this doc, plus a
> screenshot-collection helper (`tests/e2e/snapshots/collect.py`,
> referenced below). Re-evaluate in 90 days against the bug rate.

---

## Why this doc exists

CLAUDE.md plan Phase 7 shipped a static checklist (`tests/VISUAL_REGRESSION_CHECKLIST.md`).
A 30-day audit of `git log` shows the checklist did not actually
prevent CSS-only regressions:

```
$ git log --since="30 days ago" --pretty="%h %s" \
  | grep -iE "(fix|feat)\((ui|servers/ui)" | wc -l
~50 commits
```

Of those, **~25 were pure CSS / visual** (would not be caught by any
existing structural Playwright/jsdom test). Concrete ship list:

| Commit  | Symptom (CSS/visual only)                                      |
|---------|----------------------------------------------------------------|
| 6a35f26 | Light mode round 3 — body bg too pale, card edges invisible    |
| d91f3c9 | Light mode round 2 — off-white cards + dropdown active state   |
| 96cbf27 | Light-mode design system overhaul — tinted badges + restraint  |
| 361627c | Light mode redesign — cool neutral surfaces                    |
| 8cfbc56 | Brand orange propagates to .bg-primary badges                  |
| c7232fb | Brand orange propagates to light mode + softer body bg         |
| d600496 | btn-outline-primary now uses brand orange on dark theme        |
| 1310efc | Mobile job card progress full-width + Settings unit addon      |
| fc93cf6 | Mobile section-header buttons no longer wrap to 2 lines        |
| 5fa8020 | Mobile job card status text was hidden by grid overlap         |
| 235b881 | Active Jobs running-card header layout                         |
| 7fd16a4 | Mobile schedules table — card-stack treatment                  |
| dffded9 | Mobile Trigger Activity Log table — card stack with title      |
| 3996b52 | Jobs progress bar — colour-by-status + hide on mobile          |
| 8a9709a | Brand-orange Webhook Copy + Logs toolbar layout cleanup        |
| 9391eee | Modal interior polish + Status section now matches siblings    |
| 6343fb2 | Unified nav icon controls + polished notification dropdown     |
| 6af8eab | Setup wizard brand polish + dashboard System card structure    |
| 301ae3d | Workers panel header count + server cards lift on hover        |
| f03023b | Redesign login screen — panel card on soft gradient            |
| 20ff9c9 | Polish — frosted nav, branded tabs + day picker, card hover    |
| ae8a997 | Round-2 polish — JS empty-state, schedule action buttons       |
| 895f037 | Mobile responsiveness — jobs table stacks as cards on phones   |

**~25 CSS-only bugs in 30 days, or ~1 every 1–2 days.** This is the
problem the strategy below has to address.

---

## Options evaluated

### Option A — Commercial visual-regression-as-a-service (Percy / Chromatic / Applitools)

| Service     | Free tier                | Paid                                   | Integration cost       |
|-------------|--------------------------|----------------------------------------|------------------------|
| Percy       | 5,000 screenshots/mo     | $149/mo (25k); $399/mo (100k)         | ~1 day                 |
| Chromatic   | 5,000 snapshots/mo       | $149/mo (35k); $349/mo (85k)          | ~0.5 day (Storybook)   |
| Applitools  | Trial only               | Custom; ~$300+/mo for hobby projects   | ~1 day                 |

**Pros**

- Out-of-the-box image comparison + reviewer UI (approve/reject diffs in a web app).
- Cross-browser baselines (Chrome / Firefox / WebKit) without managing them yourself.
- Smart diffing: ignores antialiasing noise, font subpixel rendering.
- Handles baseline storage (no LFS bloat in the repo).

**Cons**

- **Recurring cost** for a one-maintainer hobby project. Free tier is
  enough for the current dev velocity (~10 PRs/day × 8 surfaces × 2 themes
  = 160 snapshots/PR; 5,000 / 160 = 31 PRs/mo of headroom). One bad week
  blows past it.
- Lock-in to a third-party service for a self-hosted FOSS project.
- Snapshot review is now a context-switch out of the editor / terminal
  into a SaaS web UI.
- Chromatic specifically requires Storybook, which this project does not have.

**Cost estimate (12 months):** $0 (free tier squeaks by) → $1,800–$4,800
(once dev volume grows past free tier).

---

### Option B — Playwright snapshot testing (`expect(page).toHaveScreenshot()`)

The TypeScript Playwright API has a first-class `toHaveScreenshot()`
matcher with built-in PNG diffing. **This project uses the Python
`pytest-playwright` plugin (v0.7.2), which does NOT include that
matcher.** Adding it requires either:

1. The `pytest-playwright-visual` plugin (third-party, low maintenance),
2. Or a hand-rolled Pillow comparison helper (~50 lines),
3. Or migrating the e2e suite to TypeScript (huge cost, rejected).

**Pros**

- Free, runs locally, baselines committed to the repo.
- No SaaS dependency.
- Already have `pytest-playwright` and `Pillow` installed.

**Cons**

- **Font rendering varies between Linux distros, glibc versions, browser
  versions** → constant baseline churn. Real teams using this strategy
  end up running snapshot tests inside a pinned Docker container to get
  reproducible baselines. That's an extra CI lane to maintain.
- **Repo size**: ~10 surfaces × 2 themes × 1 viewport = 20 PNGs at
  ~150 KB each = 3 MB initial. Doubles for mobile viewport. Doubles
  again every time the design changes meaningfully. Still manageable
  (under 50 MB after a year), but real.
- Maintenance toll: every legitimate UI change needs `--update-snapshots`
  and a careful eyeball of every regenerated PNG. **In practice the
  reviewer rubber-stamps** — defeating the point.
- False positives on legitimate-but-unintended antialiasing diffs are
  the dominant cost; threshold tuning helps but never eliminates them.

**Cost estimate (12 months):**
- Setup: ~0.5 day (write helper, generate baselines for all 10 surfaces).
- Ongoing: ~30 min per CSS-touching PR for baseline review, × ~25 such
  PRs/mo = ~12 hr/mo overhead. Most of that is rubber-stamping noise.

---

### Option C — DIY pixelmatch / odiff comparison

Roll our own: take screenshot, compare against committed baseline using
`pixelmatch`-equivalent (Pillow has `ImageChops.difference`), fail above
threshold.

**Pros**

- No third-party dependency.
- Total control over diff threshold, which areas to mask, etc.

**Cons**

- All the cons of Option B (font rendering, repo bloat, baseline churn,
  reviewer rubber-stamping), plus:
- Now we own the diff-rendering UI (or there isn't one — diffs are just
  "test failed", and the reviewer has to open both PNGs in an image
  viewer side-by-side).
- Pure NIH for ~50 lines of code that pytest-playwright-visual already
  provides.

**Cost estimate (12 months):** strictly worse than Option B. Skip.

---

### Option D — Stay manual, formalize the process

The 30-day data shows the current "checklist exists but nobody runs it"
status quo is broken. Improve the manual loop instead of automating:

1. **A per-PR review template** (below) that's actually copy-pasted into
   every CSS-touching PR description, not a static doc that nobody opens.
2. **A screenshot-collection helper** that boots the app and dumps PNGs
   of every key surface in both themes into `/tmp/preview-screenshots/`
   so the reviewer has something concrete to look at without manually
   navigating 10 pages × 2 themes × 2 viewports.
3. **A `[ui]` PR-label convention** that triggers the template + a
   reminder to attach before/after screenshots in the PR description.

**Pros**

- Zero recurring cost, zero new infrastructure, zero false-positive
  triage.
- The reviewer (currently: maintainer) is already the human eye that
  catches "card edges invisible in light mode" — automation can't beat
  that, it can only augment it.
- The screenshot helper is also useful for documentation, support
  tickets, blog posts, README updates.

**Cons**

- **Still depends on the human actually running the helper and looking
  at the output.** No automation guarantee.
- Doesn't catch regressions that *only* the maintainer-on-vacation would
  notice. Single point of failure.
- Doesn't scale past 1–2 maintainers.

**Cost estimate (12 months):**
- Setup: ~1 hr (write the helper + ship the template).
- Ongoing: ~5 min per CSS PR to take + attach screenshots.

---

## Recommendation: **Option D (stay manual + tighten)**

The trade-off math:

- **Option B/C** would catch ~50% of the 25 monthly CSS regressions
  (the rest are layout tweaks where the diff IS the intended change, so
  the reviewer would approve them anyway). That's ~12 bugs/mo caught at
  a cost of ~12 hr/mo of baseline-review overhead. **Net: 1 bug per
  hour of overhead.** That's not an attractive ratio for a project
  where the maintainer can usually spot the regression in 30 seconds
  by visual inspection on the canary deploy.
- **Option A** trades that overhead for $150–$400/mo. For a hobby FOSS
  project with one maintainer, that's not justified by the bug rate.
- **Option D** has the lowest ceiling but the lowest floor too: the
  process is brittle (depends on humans), but the reviewer's eye is
  already the bottleneck and automation doesn't replace it for this
  class of bug.

**Re-evaluation criteria:** if either of these becomes true, revisit:
- The maintainer count grows past 2 (manual review doesn't scale).
- The CSS-only regression rate exceeds 1/day for two consecutive weeks
  (current process is failing — formalize Option B inside a pinned
  Docker container).

---

## Per-PR review template (paste into PR description)

Mark with `[ui]` label. Copy-paste this block:

```markdown
### Visual regression review

**Touched surfaces** (check all that apply):
- [ ] Dashboard `/`
- [ ] Servers `/servers` (cards + Edit modal + Add Server form)
- [ ] Settings `/settings` (panels + steppers + tooltips)
- [ ] Setup wizard `/setup` (steps 1–5)
- [ ] Automation `/automation` (Schedules + Webhooks + Triggers)
- [ ] Logs `/logs`
- [ ] Login `/login`
- [ ] BIF Viewer `/bif-viewer`
- [ ] Active Jobs / Workers panels
- [ ] Jobs queue table

**Verification matrix** (check every cell for every touched surface):

| Mode  | Desktop (1280×720) | Mobile (≤640px) |
|-------|--------------------|-----------------|
| Light | ☐                  | ☐               |
| Dark  | ☐                  | ☐               |

**Screenshots attached:** ☐ before / ☐ after for each touched surface.
(Use `python tests/e2e/snapshots/collect.py --out /tmp/preview-shots/`
to regenerate the full set.)

**Specific things to look for:**
- [ ] Brand orange consistent on primary buttons / badges / headers / links
- [ ] No washed-out greys where brand orange should be
- [ ] Badge text contrast adequate in both themes
- [ ] Mobile tables render as card stacks (not horizontal scroll)
- [ ] Mobile page headers don't wrap to 2 lines
- [ ] Hover-lift on cards/rows where intended
- [ ] Modal dialogs centered + dismissible
- [ ] Steppers (+/-) align with input fields
- [ ] Tooltips render on hover (not focus-only)
- [ ] **Light mode**: card edges visible against body bg
- [ ] **Light mode**: code chips have a tinted bg
- [ ] **Light mode**: disabled-state text readable
- [ ] **Light mode**: active dropdown items distinct from hover
```

---

## Proof of concept (only if we ever flip to Option B)

If the bug rate forces us to adopt Playwright snapshots later, here is
the smallest possible POC. **Do not enable this by default** — it would
add ~10s to every test run for marginal value. Run on demand only.

```python
# tests/e2e/test_visual_dashboard_snapshot.py
"""POC: Playwright + Pillow visual regression for the dashboard.

Run on demand:
    pytest -m visual --no-cov tests/e2e/test_visual_dashboard_snapshot.py

First run generates the baseline. Subsequent runs diff against it and
fail if the diff exceeds 0.5% of pixels.

Not part of the default suite — gated behind the `visual` marker.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageChops

BASELINE_DIR = Path(__file__).parent / "snapshots" / "baselines"
DIFF_THRESHOLD = 0.005  # 0.5% of pixels may differ


@pytest.mark.visual
def test_dashboard_visual_baseline(authed_page, app_url):
    """Capture dashboard PNG + diff against committed baseline."""
    authed_page.goto(f"{app_url}/")
    authed_page.wait_for_load_state("networkidle")

    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    baseline = BASELINE_DIR / "dashboard_dark_1280.png"
    actual_bytes = authed_page.screenshot(full_page=False)

    if not baseline.exists():
        baseline.write_bytes(actual_bytes)
        pytest.skip("Generated baseline; re-run to compare.")

    actual_path = baseline.parent / "dashboard_dark_1280.actual.png"
    actual_path.write_bytes(actual_bytes)

    a = Image.open(baseline).convert("RGB")
    b = Image.open(actual_path).convert("RGB")
    if a.size != b.size:
        pytest.fail(f"Size mismatch: baseline={a.size} actual={b.size}")

    diff = ImageChops.difference(a, b)
    bbox = diff.getbbox()
    if bbox is None:
        return  # pixel-perfect

    # Count non-zero pixels
    diff_pixels = sum(1 for px in diff.getdata() if any(px))
    total = a.size[0] * a.size[1]
    ratio = diff_pixels / total
    assert ratio <= DIFF_THRESHOLD, (
        f"Visual diff {ratio:.2%} exceeds threshold {DIFF_THRESHOLD:.2%}. "
        f"Inspect {actual_path} vs {baseline}."
    )
```

To enable: add `visual: marks visual-regression snapshot tests` to the
`markers` block in `pyproject.toml` and run only on demand:

```
pytest -m visual --no-cov tests/e2e/
```

---

## Cross-references

- Original audit: `TEST_AUDIT.md`
- Execution plan: `/home/data/.claude/plans/iridescent-churning-bear.md`
- Regression validation table: `tests/REGRESSION_VERIFIED.md`
