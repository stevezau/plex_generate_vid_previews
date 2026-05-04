# Audit: tests/test_dispatcher_kwargs_matrix.py — 12 tests, 7 classes (+ 1 parametrized sweep)

This file is the dedicated D34-paradigm closure: every test pins the FULL kwarg shape forwarded into `process_canonical_path`. The `_drive_dispatcher` helper returns the captured `call_args.kwargs` AND the registry/config/cfg objects so identity (not just truthiness) can be asserted on the forwarded references.

## TestPlexNoPinFansOut

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 192 | `test_plex_no_caller_pin_forwards_server_id_filter_none` | **Strong** — `_assert_common_kwargs_shape` pins canonical_path, registry IDENTITY, config IDENTITY, callable progress_callback. Then strict `server_id_filter is None`. Cell 1. |
| 203 | `test_regenerate_default_propagates_as_false` | **Strong** — `regenerate is False` (bool, not None — pinned in message). |
| 211 | `test_regenerate_true_propagates_when_config_set` | **Strong** — flips the bit and asserts True propagates. Two-cell coverage. |

## TestPlexWithCallerPin

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 226 | `test_plex_caller_pin_wins_over_originator_default` | **Strong** — `server_id_filter == "explicit-pin"` strict equality + common kwargs shape. Closes the d9918149 reproducer (called out in the assertion message). Cell 2. |

## TestEmbyNoPinScopes

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 243 | `test_emby_no_caller_pin_scopes_to_originator` | **Strong** — `server_id_filter == cfg.id` (the originator). The exact branch the D34 dispatcher bug hit (Plex-pinned vs non-Plex-scoped). Cell 3. |

## TestEmbyWithCallerPin

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 261 | `test_emby_caller_pin_wins_over_originator_scope` | **Strong** — caller pin overrides originator scope; strict equality. Cell 4. |

## TestJellyfinNoPinScopes

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 276 | `test_jellyfin_no_caller_pin_scopes_to_originator` | **Strong** — `server_id_filter == cfg.id`. Cell 5. |

## TestJellyfinWithCallerPin

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 291 | `test_jellyfin_caller_pin_wins` | **Strong** — `server_id_filter == "jelly-explicit"`. Cell 6. Closes the matrix. |

## TestItemFieldsPropagate

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 309 | `test_item_id_by_server_hint_propagates` | **Strong** — strict equality `{"plex-only": "rk-12345"}`. Pins the optimisation contract (avoid Plex reverse lookup). |
| 319 | `test_item_id_by_server_none_when_unset` | **Strong** — pins the empty-dict-coerced-to-None contract (orchestrator line 716 quoted in comment). |
| 328 | `test_bundle_metadata_by_server_propagates` | **Strong** — strict equality on tuple value `("hash", 0.123)`. |

## TestGpuKwargsPropagate

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 350 | `test_gpu_none_when_no_selected_gpus` | **Strong** — `gpu is None` AND `gpu_device_path is None`. Pins CPU-fallback. |

## Module-level parametrized matrix sweep

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 382 | `test_full_pin_matrix` (6 parametrized cells) | **Strong** — single sweep over the (server_type × caller_pin) matrix. Catches the case where adding a new ServerType silently breaks one cell. Each parametrized id is meaningful (e.g. `plex_no_pin_fans_out`). |

## Summary

- **12 named tests + 6 parametrized cells = 18 effective tests**
- **All Strong** — every test pins the FULL kwarg shape (`_assert_common_kwargs_shape` enforces canonical_path, registry IDENTITY, config IDENTITY, progress_callback callability) PLUS a cell-specific assertion
- 0 weak / bug-blind / tautological / dead
- This is THE D34-paradigm closure file; the helper architecture (`_drive_dispatcher` returning real refs for identity assertions) prevents the "silently swapped registry/config" regression

**File verdict: STRONG.** No changes needed. This file is exemplary — should be cited in the testing rules as the model for kwargs-matrix closures.
