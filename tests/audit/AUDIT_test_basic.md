# Audit: tests/test_basic.py — 7 tests, 3 classes

## TestBasicFunctionality

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 13 | `test_package_imports` | **Strong** | Asserts `__version__` exists AND matches PEP 440 regex. Catches setuptools-scm misconfiguration (b845349 fix). Not just `is not None` — pattern match is meaningful. |
| 27 | `test_web_module_importable` | **Strong** | Actually instantiates `create_app()`, confirms `isinstance(app, flask.Flask)`, asserts URL rules exist AND that `/api/` routes are present. Catches blueprint-registration silent-import failures. Not a tautology — production wiring is exercised. |
| 58 | `test_socketio_polling_only` | **Strong (duplicate of P0.6)** | Pins `engineio.Server.allow_upgrades is False`. Same contract as `tests/test_socketio.py::TestSocketIOTransportConfig::test_allow_upgrades_is_false_on_underlying_engineio_server` (Phase 1 P0.6). Slightly redundant — could be deleted. **Decision: keep both** (one in test_basic for the canary smoke, one in test_socketio for the dedicated transport-pin file). Audit verified ✅. |
| 86 | `test_no_cli_module` | **Strong (defensive)** | Asserts CLI module was actually removed. If a refactor accidentally re-introduces `media_preview_generator.cli`, this fires. Edge case: marginal value (a CLI re-introduction would be obvious in a PR), but cheap to keep. |
| 93 | `test_config_validation_error_class` | **Weak → Strong-enough** | Tests that `ConfigValidationError` accepts a list and exposes `.errors`. Substring assertion + length check. The `.errors` attribute IS the contract callers rely on; without this, a refactor that drops the attr would only show up in production. **Keep**. |

## TestConfigFunctions

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 106 | `test_get_config_value_cli_precedence` | **Strong** | CLI value AND env var both set → asserts CLI wins. Three-line precedence-chain function; this exact test would catch swapped precedence. |
| 117 | `test_get_config_value_env_fallback` | **Strong** | CLI=None + env set → env value. Mirror of above. |
| 128 | `test_get_config_value_default_fallback` | **Strong** | CLI=None + no env → default. Together with the two above, fully covers the precedence matrix. |

## TestGPUDetection

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 143 | `test_format_gpu_info` | **Strong** | 3 specific format checks (NVIDIA, AMD VAAPI, legacy fallback). Strict equality on output strings. Catches format drift. |
| 159 | `test_ffmpeg_version_check` | **Strong** | Tests version regex extraction (parses `"ffmpeg version 7.1.1-1ubuntu1.2"` → `(7,1,1)`), then tests the ≥ check at the boundary (7.1.0 → True, 6.9.0 → False). Mocks at the right boundary (subprocess + the version-getter). Not tautological. |

## Summary

- **10 tests** total
- **0 weak / bug-blind / dead** — all tests exercise real production contracts
- **0 needs_human** — no judgment calls required

**File verdict: STRONG.** All tests already meaningful. No changes needed.

The closest-to-redundant is `test_socketio_polling_only` which duplicates Phase 1 P0.6 work, but keeping it as the canary smoke in test_basic is reasonable.
