# Audit: tests/test_servers_base.py — 18 tests, 9 classes

## TestServerType

| Line | Test | Verdict |
|---|---|---|
| 29 | `test_values_are_lowercase_strings` | **Strong** — strict equality on every enum value. The string forms are stable wire-format identifiers in settings.json; pinning them prevents an accidental rename from silently breaking persisted configs. |
| 34 | `test_can_round_trip_through_string` | **Strong** — `is` identity check (singleton enum). Pins string-construction (used by `server_config_from_dict`). |

## TestLibrary

| Line | Test | Verdict |
|---|---|---|
| 40 | `test_minimal_library_defaults_to_enabled` | **Strong** — strict `enabled is True` AND `kind is None` defaults. Catches a regression that flipped the default to disabled (would silently skip libraries on settings load). |
| 45 | `test_remote_paths_is_tuple` | **Strong** — tuple type pin (immutable). If someone changed to `list`, the frozen dataclass contract would break in subtle ways. |
| 49 | `test_is_frozen` | **Strong** — explicit `FrozenInstanceError` raise on mutation. Pins immutability. |

## TestMediaItem

| Line | Test | Verdict |
|---|---|---|
| 56 | `test_required_fields` | **Weak** — only asserts `id == "42"` and `remote_path == "/m/foo.mkv"`, ignoring `library_id` and `title`. Removing those parameters from the SUT wouldn't fail this test. **Note for fixing**: add `assert item.library_id == "1"` and `assert item.title == "Foo"` (and assert any default fields stay at their defaults). |

## TestWebhookEvent

| Line | Test | Verdict |
|---|---|---|
| 63 | `test_path_only_event` | **Strong** — pins both fields (`item_id is None`, `remote_path == "/m/foo.mkv"`). |
| 68 | `test_item_id_only_event` | **Weak (could be tighter)** — pins `remote_path is None` but doesn't assert the `item_id` was actually set to "42" or that `event_type == "ItemAdded"`. **Note for fixing**: add `assert ev.item_id == "42"` so removing the param from `WebhookEvent` would fail. |

## TestConnectionResult

| Line | Test | Verdict |
|---|---|---|
| 74 | `test_failure_minimum` | **Strong** — `not r.ok` AND `server_id is None` default. |
| 79 | `test_success_carries_identity` | **Weak** — only asserts `r.ok` and `server_id == "abc123"`; ignores `server_name` and `version` which are explicitly set in the test data. **Note for fixing**: add `assert r.server_name == "Home Plex"` and `assert r.version == "1.40.0"`. |

## TestServerConfig

| Line | Test | Verdict |
|---|---|---|
| 91 | `test_defaults` | **Strong** — pins every default (`libraries == []`, `path_mappings == []`, `output == {}`, `verify_ssl is True`). The `verify_ssl` default in particular is security-relevant. |

## TestMediaServerABC

| Line | Test | Verdict |
|---|---|---|
| 136 | `test_cannot_instantiate_without_implementing_abstract_methods` | **Strong** — strict `TypeError` raise — pins ABC contract. |
| 140 | `test_concrete_subclass_works` | **Strong** — exercises every method on the protocol with strict equality (`s.id`, `s.name`, `s.type is`, etc.). Catches a regression that drops a method from the ABC's expected surface. |

## TestSearchItemsDefault

| Line | Test | Verdict |
|---|---|---|
| 163 | `test_empty_query_returns_empty` | **Strong** — empty string AND whitespace-only both → `[]`. Mirrors the production `(query or "").strip().lower()` short-circuit. |
| 168 | `test_walks_libraries_and_items_filtering_substring` | **Strong** — strict `len == 1` AND `results[0].title == "Interstellar"`. Pins both the case-insensitive substring match AND the result count. |
| 182 | `test_respects_limit` | **Strong** — strict `len == 5` for limit=5 from a 20-item iterator. Pins the early-return after limit. |
| 194 | `test_case_insensitive` | **Strong** — `"matrix"` query against `"The MATRIX"` title returns 1 match. |

## TestOutputAdapterABC

| Line | Test | Verdict |
|---|---|---|
| 222 | `test_cannot_instantiate_without_implementing_abstract_methods` | **Strong** — `TypeError` on bare ABC. |
| 226 | `test_bundle_dataclass_shape` | **Weak** — pins `canonical_path` and `bif_path` but ignores `frame_dir`, `frame_interval`, `width`, `height`, `frame_count` — all explicitly set in the constructor. **Note for fixing**: add at minimum `assert b.frame_interval == 10` and `assert b.frame_count == 540`. Without it, a refactor that swaps two field positions would silently pass. |
| 239 | `test_concrete_adapter_works` | **Strong** — pins `paths == [Path("/m/foo.bif")]` strict equality AND `not adapter.needs_server_metadata()`. |

## TestLibraryNotYetIndexedError

| Line | Test | Verdict |
|---|---|---|
| 256 | `test_inherits_from_exception` | **Strong** — `issubclass` pin. Catches a refactor that broke the exception hierarchy (would prevent `except Exception` callers from catching it). |
| 259 | `test_can_carry_message` | **Strong** — strict `str(e) == "..."` — pins the standard exception message contract. |

## Summary

- **22 tests** total — 18 Strong, 4 Weak
- Weak: `test_required_fields` (line 56), `test_item_id_only_event` (line 68), `test_success_carries_identity` (line 79), `test_bundle_dataclass_shape` (line 226). All weak in the same way: they construct a dataclass with N fields explicitly set but only assert on M < N of them. A field reorder / silent rename would slip through.

**File verdict: MIXED.** All weak tests are dataclass-shape pinning that can be tightened by asserting on every field that was explicitly set in the constructor. None are bug-blind/tautological/bug-locking.
