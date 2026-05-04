# Audit: tests/test_logging_config.py — 19 tests, 4 classes

Tests `setup_logging`, JSON sink, SocketIO broadcaster, and the JSONL record patcher. Most `setup_logging` tests patch the loguru `logger` module-level reference and inspect `mock_logger.add.call_args_list[0]` — boundary is correct (loguru is the third-party we don't own, not the SUT).

## TestLoggingConfig

| Line | Test | Verdict |
|---|---|---|
| 72 | `test_setup_logging_default` | **Strong** — pins `add.call_count == 2` AND `stderr_call.kwargs["level"] == "INFO"`. Catches loss of either handler or wrong default level. |
| 84 | `test_setup_logging_debug` | **Strong** — DEBUG propagates to stderr handler AND app.log keeps `rotation="10 MB"`/`retention=5` even at DEBUG. The "rotation persists at DEBUG" pin catches an easy-to-miss regression. |
| 99 | `test_setup_logging_with_console` | **Weak** — only asserts `level == "INFO"` and `add.call_count == 2`. The docstring says "wraps console.print" but the test never checks the sink IS the console. A regression that ignores the console arg would pass. |
| 114 | `test_setup_logging_adds_app_log_handler` | **Strong** — pins app.log level/rotation/retention AND `"{extra[_jsonl]}"` is in the format string (catches dropping the JSONL formatter). |
| 129 | `test_setup_logging_custom_rotation_retention` | **Strong** — strict equality on custom `rotation="5 MB"` and `retention=4`. Catches the "ignored kwarg" bug. |
| 141 | `test_setup_logging_handles_permission_error` | **Strong** — when `os.makedirs` raises, only stderr (1 handler) is added — strict count pin. Catches the "swallow error then add file handler anyway" bug. |
| 149 | `test_setup_logging_creates_error_log` | **Strong** — real fs assertion via `tmp_path`; `os.path.isdir(log_dir)` pin. |

## TestJSONLogging

| Line | Test | Verdict |
|---|---|---|
| 176 | `test_json_format_adds_json_sink` | **Strong** — `first_add.args[0] is _json_sink` (identity). Catches a refactor that swaps in a stub. |
| 187 | `test_json_format_via_env_var` | **Strong** — pins env-var path equivalent to explicit `log_format="json"`. |
| 196 | `test_pretty_format_ignores_json_sink` | **Strong** — `is not _json_sink` mirror. |
| 204 | `test_console_ignored_when_json` | **Strong** — even with console arg, json wins. Pins precedence rule. |
| 211 | `test_json_sink_produces_valid_json` | **Strong** — actually parses each captured line, finds the test message, asserts level/timestamp/function fields. Real integration. |
| 244 | `test_json_sink_includes_exception` | **Strong** — pins exception field AND substring of original error in the JSON payload. Real `try/except` exercise. |

## TestSocketIOLogBroadcaster

| Line | Test | Verdict |
|---|---|---|
| 282 | `test_get_set_broadcaster` | **Strong** — full round-trip: None → set → get → None. Identity check (`is`). |
| 298 | `test_sink_emits_to_correct_room` | **Strong** — pins emit args (`"log_message"`, `room=="WARNING"`, `namespace=="/logs"`) AND payload fields (`level`, `msg`, `mod=="worker"`). The `mod=="worker"` pin catches a bug in the name-stripping logic (full module name → last segment). |
| 330 | `test_sink_filters_out_trace_level` | **Strong** — `mock_sio.emit.assert_not_called()`. Pins the TRACE filter. |
| 351 | `test_sink_swallows_emit_errors` | **Strong** — pins "doesn't raise" contract when downstream emit fails. The implicit assertion (no exception) is the right one for this contract. |
| 375 | `test_setup_logging_attaches_broadcaster` | **Strong** — pins `add.call_count == 3` (stderr + app.log + broadcaster) AND broadcaster level. Catches "broadcaster registered but not attached" bug. |

## TestJsonlRecordPatcher

| Line | Test | Verdict |
|---|---|---|
| 401 | `test_patcher_stores_jsonl_in_extra` | **Strong** — parses the produced JSON and pins every field (`ts`, `level`, `msg`, `mod="worker"`, `func`, `line`). The `mod="worker"` pin catches dotted-name-stripping regressions. |
| 427 | `test_patcher_handles_empty_name` | **Strong** — edge case: empty record name → empty `mod` AND `func` (None → ""). |
| 446 | `test_patcher_escapes_json_in_message` | **Strong** — quotes + backslashes round-trip via `json.loads`. Catches a homemade-quoting bug. |
| 464 | `test_get_app_log_path_default` | **Strong** — strict equality on default `/config/logs/app.log`. |
| 470 | `test_get_app_log_path_custom_config_dir` | **Strong** — strict equality on `CONFIG_DIR` override. |
| 475 | `test_app_log_written_as_jsonl` | **Strong** — real fs integration, parses every line, pins `ts`/`level`/`msg` fields present. |

## Summary

- **24 tests** — 23 Strong, 1 Weak
- **Weak**: `test_setup_logging_with_console` (line 99) — docstring promises "binds the stderr handler to it" but the assertion only checks the level and call count. Could be a tautology since the test never verifies the console got wired in. Suggest either dropping the test or adding `assert mock_console.print` was wrapped (or the sink callable references it).
- All other `add.call_count` pins are paired with kwarg checks → not bug-blind
- JSON-sink and JSONL-patcher tests parse real output (not just call mocks) → high confidence
- The `mod=="worker"` field pins (lines 298 & 401) catch a real class of bugs in module-name stripping

**File verdict: STRONG.** One test (`test_setup_logging_with_console`) could be tightened but isn't actively bug-locking.
