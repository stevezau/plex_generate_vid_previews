# Audit: tests/test_servers_page.py — 3 tests, 1 class

## TestServersPage

| Line | Test | Verdict |
|---|---|---|
| 54 | `test_unauthenticated_redirects_to_login` | **Strong** — pins exact 302 status (rejects 308/303), AND asserts `/login` substring in `Location`. The inline comment shows prior weak version (status not pinned) was tightened. |
| 62 | `test_authenticated_renders` | **Strong** — pins 200, then asserts 5 specific landmarks: page title, button label, webhook URL marker (or JS variable), and `data-type="plex"|"emby"|"jellyfin"` attributes. Each is a contract the JS layer reads — catches blueprint/template regressions. |
| 74 | `test_navbar_includes_servers_link` | **Strong** — explicitly demands 200 (per inline note, the prior version wrapped in `if status_code == 200` and silently passed on redirects to /setup or /login). The `"/servers" in body` check is a substring test but on a UI link. Could be tightened to assert `href="/servers"` rather than the path alone, but acceptable. |

## Summary

- **3 tests** — 3 Strong, 0 Weak / Bug-blind / Tautological
- Inline comments reference prior audit fixes (status-code pinning, removal of conditional skip) — already hardened.

**File verdict: STRONG.** No changes needed. Minor: line 81 substring (`"/servers" in body`) could become `'href="/servers"' in body` for stricter coverage, but not bug-blind today.
