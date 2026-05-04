# Audit: tests/test_static_app_js.py — 10 tests, 5 classes

Static guards on JS files. The audit doc TEST_AUDIT.md flagged this file
as "JS syntax validation; redundant with the linter" (P3.7). After
reading every test: **the audit was wrong**. These are NOT syntax tests
— they're contract pins for specific JS bug shapes that have shipped:
null-deref crashes on SocketIO reconnect, embedded `</script>` token
inside inline JS, missing children-promotion in HTML sanitizer, etc.

## TestSocketReconnectNullGuards

| Line | Test | Verdict |
|---|---|---|
| 46 | `test_update_job_queue_guards_missing_tbody` | **Strong** — pins the early-return guard in updateJobQueue. Catches the SocketIO-reconnect-from-non-dashboard null-deref crash. Real bug class. |
| 55 | `test_update_worker_statuses_guards_missing_container` | **Strong** — same pattern for workerStatusContainer |
| 63 | `test_load_job_stats_guards_missing_stat_elements` | **Strong** — same pattern for loadJobStats |

## TestNoLiteralScriptTagsInsideTemplateInlineScripts

| Line | Test | Verdict |
|---|---|---|
| 90 | `test_no_literal_script_close_inside_template_inline_scripts` | **Strong (clever)** — scans every Jinja template's inline script blocks for the literal `<script>` token. Real bug from CI (setup.html had it in a comment, broke wizard handlers). Cheap regex; high blast-radius bug. |

## TestServersJsBailsOnNonServersPages

| Line | Test | Verdict |
|---|---|---|
| 132 | `test_dom_content_loaded_short_circuits_when_modal_absent` | **Strong** — pins the early-return in servers.js's Edit-Server-modal DOMContentLoaded handler. CI caught this as a folder_picker e2e timeout. Static-grep verifies the pattern is preserved. |

## TestNotificationSanitizerAllowsDisclosure

| Line | Test | Verdict |
|---|---|---|
| 167 | `test_details_and_summary_are_in_allow_list` | **Strong** — pins `<details>` + `<summary>` in the sanitizer allow-list. Without these, the schema-migration "What changed" expander collapses. |
| 175 | `test_unwrap_promotes_children_not_textcontent` | **Strong** — pins the children-promotion path (the B5 bug fix). Asserts both: NOT collapsing to textContent AND DOES use insertBefore. Two-edge contract pin. |

## TestRenderMarkdownBasicHandlesGitHubReleaseBodies

| Line | Test | Verdict |
|---|---|---|
| 200 | `test_normalizes_crlf_to_lf` | **Strong** — pins the CRLF normalization step. GitHub release bodies arrive with CRLF; without normalization, `/\n{2,}/` regex never matches paragraph breaks. |
| 208 | `test_h2_h3_tolerate_leading_whitespace` | **Strong** — pins the `^[ \t]{0,4}### ` regex form. Catches drop-back to `^### ` which silently drops indented sub-headings. |
| 218 | `test_supports_markdown_links_with_safe_schemes` | **Strong** — three-way contract: (a) link replace exists, (b) https/mailto allowed, (c) javascript: NOT in regex (XSS protection). Excellent multi-edge pin. |

## Summary

- **10 tests**, all **Strong**
- The audit's recommendation to delete this file (P3.7) was wrong — these tests catch real bug shapes that have shipped
- Notable: 2 tests use clever cross-file static analysis (template scan + servers.js block extraction) — appropriate given no JS test infra exists

**File verdict: STRONG.** No changes needed. **Audit's "delete this file" recommendation REJECTED** — keep the file.
