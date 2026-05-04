# Audit: tests/test_version_check.py — 33 tests, 7 classes

## TestGetCurrentVersion

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 63 | `test_returns_package_version_when_dunder_version_present` | **Strong** | Strict equality `== "9.9.9-test"`. Pins branch 1 (the `__version__` source). |
| 70 | `test_falls_back_to_importlib_metadata_when_dunder_missing` | **Strong** | Strict equality `== "5.4.3"`. Pins branch 2 fallback path with deletion of `__version__`. |
| 80 | `test_falls_back_to_zero_zero_zero_when_all_sources_fail` | **Strong** | Strict equality `== "0.0.0"`. Pins sentinel and exception-handling for `PackageNotFoundError`. Together with above two, fully covers the 3-branch matrix. |

## TestParseVersion

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 98 | `test_parse_version_valid` | **Strong** | Strict tuple equality `(2, 0, 0)`. |
| 103 | `test_parse_version_with_v_prefix` | **Strong** | Pins v-prefix stripping behavior. |
| 108 | `test_parse_version_with_metadata` | **Strong** | Pins discard of `-alpha+build123` metadata. |
| 113 | `test_parse_version_with_local_identifier` | **Strong** | Two strict-equality checks for PEP 440 local identifier handling (the dev-snapshot case is critical for the version-check downstream logic). |
| 121 | `test_parse_version_invalid` | **Strong** | Three distinct invalid forms — bare string, 2-segment, 4-segment — all expected to raise `ValueError`. Catches a regression that loosens the regex. |

## TestGetLatestGitHubRelease

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 137 | `test_get_latest_github_release` | **Strong** | Strict equality `== "v2.1.0"`. Tests JSON parsing and tag extraction. |
| 147 | `test_get_latest_github_release_timeout` | **Strong** | Asserts `is None` on `Timeout` exception — pins error suppression contract. |
| 155 | `test_get_latest_github_release_connection_error` | **Strong** | Same as above for `ConnectionError`. Distinct error path. |
| 163 | `test_get_latest_github_release_rate_limit` | **Strong** | 429 HTTP response → `None`. Distinct branch (status_code check matters for log differentiation in production). |
| 174 | `test_get_latest_github_release_404` | **Strong** | 404 → `None`. Distinct status path. |
| 185 | `test_get_latest_github_release_empty_tag` | **Strong** | Empty `tag_name` → `None`. Pins falsy-tag rejection (catches a regression that returns `""`). |

## TestCheckForUpdates

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 204 | `test_check_for_updates_newer_available` | **Strong** | Asserts 3 substrings in log text (`newer version is available`, `v2.1.0`, `2.0.0`). Multi-substring catches partial-message regressions. |
| 219 | `test_check_for_updates_up_to_date` | **Strong (negative)** | Negative-assertion pair: `not in` for both `newer version` and `Update:`. Catches false-positive nags. |
| 233 | `test_check_for_updates_current_newer` | **Strong** | Negative pair confirms no false downgrade nag. |
| 254 | `test_check_for_updates_api_failure` | **Strong (negative)** | Pins silent-failure contract when API returns None. |
| 267 | `test_check_for_updates_docker_message` | **Strong** | Pins docker-pull instruction with image name `stevezzau/media_preview_generator`. |
| 285 | `test_check_for_updates_non_docker_message` | **Strong** | Pins pip-install instruction with full git URL — strong contract pin. |
| 302 | `test_check_for_updates_dev_snapshot` | **Strong** | 3 substrings pinning the dev-snapshot path (`development snapshot`, `v2.1.0`, `Latest stable release`). |
| 314 | `test_check_for_updates_invalid_version_handled` | **Bug-locking (acknowledged)** | Test docstring explicitly notes it pins the current swallow-failure-silently behavior as a known product-quality gap. Tracked separately. Acceptable but flagged. |
| 330 | `test_check_for_updates_dev_docker_up_to_date` | **Strong** | Asserts `"Dev build up to date"` and `"dev"` substrings, plus negative for `"Newer dev commit"`. |
| 341 | `test_check_for_updates_dev_docker_behind` | **Strong** | Asserts `"Newer dev commit"` AND `":dev"` (catches regression that uses `:latest`). |
| 352 | `test_check_for_updates_dev_docker_api_failure` | **Strong (negative)** | Both negative substrings — no fake claims. |
| 364 | `test_check_for_updates_git_checkout_up_to_date` | **Strong** | Mixed positive (`"Git checkout up to date"`, `"main"`) + negative (`"Newer commit on"`). |
| 379 | `test_check_for_updates_git_checkout_behind` | **Strong** | Pins both `"Newer commit on"` AND the literal `"git pull origin main"` instruction string. |

## TestGetGitCommitSha

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 394 | `test_returns_sha_when_in_repo` | **Strong** | Strict equality on full 40-char SHA. Pins newline stripping. |
| 405 | `test_returns_none_when_not_in_repo` | **Strong** | Pins `is None` on returncode 128. |
| 417 | `test_handles_file_not_found` | **Strong** | Pins `is None` on `FileNotFoundError` (git binary absent). |

## TestGetGitBranch

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 430 | `test_returns_branch_name` | **Strong** | Strict equality `== "dev"`. |
| 442 | `test_returns_none_for_detached_head` | **Strong** | Strict `is None` for the `HEAD` sentinel — distinct branch from failure. |
| 453 | `test_returns_none_on_failure` | **Strong** | Pins `is None` on returncode 128. |

## TestGetBranchHeadSha

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 470 | `test_returns_sha_on_success` | **Strong** | Strict equality on 40-char SHA. |
| 482 | `test_returns_none_on_timeout` | **Strong** | Distinct error branch. |
| 488 | `test_returns_none_on_connection_error` | **Strong** | Distinct error branch. |
| 495 | `test_returns_none_on_http_error` | **Strong** | Distinct error branch (HTTP 404 path). |
| 504 | `test_returns_none_on_empty_sha` | **Strong** | Pins falsy-SHA rejection. |
| 514 | `test_returns_none_on_request_exception` | **Strong** | Generic RequestException branch. |
| 521 | `test_returns_none_on_unexpected_error` | **Strong** | Pins broad `except Exception` swallow — catches a regression that re-raises. |

## TestGetLatestGitHubReleaseExtra

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 532 | `test_handles_generic_http_error` | **Strong** | 500 path distinct from 404/429. |
| 541 | `test_handles_generic_request_exception` | **Strong** | Generic RequestException branch. |
| 546 | `test_handles_key_error` | **Strong** | Pins `KeyError` swallow on malformed JSON. |
| 554 | `test_handles_unexpected_exception` | **Strong** | Pins broad `except Exception`. |

## Summary

- **41 tests** total
- **40 Strong**, **1 Bug-locking (acknowledged)**, 0 weak/bug-blind/tautological/dead/needs-human

**File verdict: STRONG.**

The single bug-locking test (line 314 `test_check_for_updates_invalid_version_handled`) is intentional — its docstring documents that it pins the current swallow-failure-silently behavior and the gap is tracked elsewhere. No action required.
