"""Drive the architecture-review agent for the GitHub Actions workflow.

Reads the diff between BASE_SHA and HEAD, sends it to Claude with a
focused audit prompt (the same shape that found the May 2026 audit
chain bugs — bug-blind tests, retry-pin loss, vestigial pre-connect),
and writes the findings to stdout in markdown.

Inputs (env):
* ANTHROPIC_API_KEY — required.
* BASE_SHA          — diff base. Defaults to HEAD~1.
* CHANGED_FILES     — newline-separated paths the workflow already
                      filtered to media_preview_generator/ + tests/.

Output (stdout):
* Markdown audit report. Empty body when there are no findings.
"""

from __future__ import annotations

import os
import subprocess
import textwrap

import anthropic

# Same audit prompt shape that found the production bugs in the
# May 2026 audit chain. Calibrated to flag:
#  * Bug-blind tests (assert_called_once / call_count without kwargs).
#  * Un-wrapped failure_scope on dispatch entry points.
#  * Tests that mock at HTTP boundary while asserting on URL/header
#    substrings (D31-shape).
#  * Lazy-init without lock (concurrency race).
#  * Vestigial pre-connection / blocking work on hot paths.
#  * Comments lying vs the code (we've seen this multiple times).
AUDIT_PROMPT = """\
You are reviewing a code diff in the plex_generate_vid_previews
project (Python, Flask + SocketIO + plexapi/Emby/Jellyfin clients,
GPU FFmpeg pipeline). The repo's testing rules live at
``.claude/rules/testing.md``; the codebase conventions at
``.claude/CLAUDE.md``.

Audit the diff for the following bug shapes that have shipped to
production in this codebase before. Be aggressive — false positives
are cheaper than false negatives.

1. **Bug-blind mock tests.** ``mock.assert_called_once()`` /
   ``assert_called()`` without ``_with(...)`` covering the kwargs
   the SUT controls. Or ``mock.call_count == N`` without checking
   ``call.kwargs``. The D34 retry-pin regression hid for months
   because the test asserted "called once" but not "with
   server_id_filter=…".

2. **HTTP-boundary mocks asserting on URL/header substrings.** If a
   new test mocks ``_request`` / ``requests.*`` / ``plex.fetchItems``
   while asserting things like ``"file=" in url``, that's the D31
   ``?type=`` 500 shape. Suggest: convert to a cassette in
   ``tests/test_servers_*_vcr.py``.

3. **Un-wrapped ``failure_scope``.** If the diff adds a new dispatch
   entry point (calls ``process_canonical_path``, ``run_processing``,
   or schedules an APScheduler timer), check that it's wrapped in
   ``with failure_scope(...)``. Missing scope → "Internal bookkeeping
   bug" warnings in production.

4. **Lazy init without lock.** Pattern ``if self._x is None:
   self._x = construct()`` without ``threading.Lock`` is a race —
   N parallel workers will all construct.

5. **Vestigial blocking work.** Connection establishment, pre-fetches,
   or other I/O on the hot path of a function whose result the
   downstream caller doesn't actually use. Specifically watch for
   ``plex_server(config)`` calls whose return value is then passed
   to a function that doesn't use it.

6. **Comments lying vs code.** Docstring or inline comment that
   describes a behaviour the code no longer implements. Example:
   "always falls through to Pass 1+2" comment over code that now
   short-circuits.

7. **Tests that mock at the wrong layer.** Tests that mock OUR
   helper (``_uncached_resolve_remote_path_to_item_id``) when their
   purpose is to verify SUT logic AROUND that helper — they will
   pass even if the helper itself regresses. Suggest mocking at
   the boundary (``_request``).

8. **Cover-the-matrix gaps.** New branching code (``if x is
   PLEX``) that has only one or two cells of the test matrix
   covered. The .claude/rules/testing.md "Cover the matrix" rule
   demands every distinct branch value get a test.

For each finding, output exactly this markdown shape:

    ### <SEVERITY> — <one-line title>
    **File:** `<path>:<line>`
    **Why:** <one-line explanation, linking to the bug shape above>
    **Fix:** <concrete remediation>

SEVERITY is HIGH | MED | LOW. HIGH means production-shape that has
shipped before. MED means latent that an audit caught. LOW means
nit / hygiene.

If there are NO findings, output exactly:

    ✅ No findings. Diff looks clean against the eight bug shapes.

Do NOT speculate beyond the diff. Do NOT report stylistic preferences.
Stay under 1500 words.

---

DIFF (truncated to 200KB if larger):

```diff
{diff}
```

CHANGED FILES:

{changed_files}
"""


def _fetch_diff(base: str) -> str:
    """Capture the full diff against ``base``. Truncated to 200KB to
    fit comfortably in a single Claude turn — most PRs are far under
    that. Truncation is at line boundary so the agent doesn't see
    half a hunk.
    """
    try:
        out = subprocess.check_output(
            ["git", "diff", "--unified=5", base, "HEAD", "--", "media_preview_generator/", "tests/"],
            text=True,
            stderr=subprocess.STDOUT,
        )
    except subprocess.CalledProcessError as exc:
        return f"<git diff failed: {exc.output!r}>"
    if len(out) <= 200_000:
        return out
    truncated = out[:200_000]
    last_nl = truncated.rfind("\n")
    return truncated[: last_nl + 1] + "\n... (truncated; diff exceeds 200KB)\n"


def main() -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY") or ""
    if not api_key:
        # Workflow's gate step should have skipped this; defensive.
        print("# Architecture review skipped: ANTHROPIC_API_KEY not set.")
        return 0

    base = os.environ.get("BASE_SHA") or "HEAD~1"
    changed_files = (os.environ.get("CHANGED_FILES") or "").strip()
    if not changed_files:
        print("# Architecture review skipped: no Python files changed.")
        return 0

    diff = _fetch_diff(base)
    prompt = AUDIT_PROMPT.format(diff=diff, changed_files=changed_files)

    client = anthropic.Anthropic(api_key=api_key)
    # Use Sonnet for the audit — Claude's strongest cost-effective
    # tier for code review. Cap output at 4K tokens (audits should
    # be terse). Temperature 0 for reproducible reports.
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    body_parts = [
        block.text
        for block in response.content
        if getattr(block, "type", None) == "text" and getattr(block, "text", None)
    ]
    body = (
        "\n\n".join(body_parts).strip()
        or textwrap.dedent("""
        # Architecture review

        ✅ No findings. Diff looks clean against the eight bug shapes.
    """).strip()
    )
    print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
