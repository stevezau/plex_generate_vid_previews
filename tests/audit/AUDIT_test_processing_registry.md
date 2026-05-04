# Audit: tests/test_processing_registry.py — 12 tests, 4 classes

## TestRegistryRoundTrip

| Line | Test | Verdict |
|---|---|---|
| 70 | `test_register_then_get_round_trips` | **Strong** — `is` identity check (not equality) — pins that the SAME object is returned, not a copy |
| 75 | `test_get_accepts_string_form` | **Strong** — string lookup contract (settings JSON loads strings, not ServerType enums) |
| 81 | `test_unknown_string_raises_keyerror` | **Strong** — strict `match="unknown server type"` — pins error message text the user sees |
| 85 | `test_unregistered_known_type_raises_keyerror` | **Strong** — different error path (known enum, no registration) — distinct from row above |
| 89 | `test_re_registration_overrides` | **Strong** — last-write-wins contract for the registry |
| 96 | `test_registered_types_lists_what_was_added` | **Strong** — strict equality on sorted list |

## TestProtocolShape

| Line | Test | Verdict |
|---|---|---|
| 103 | `test_stub_satisfies_protocol` | **Weak (structural only)** — exercises each method but the assertions are all `== []` / `is None` from the stub. Doesn't test ANY production behavior — only that the protocol signature is consumable. **Decision: Keep** as a structural sanity check; it does catch a regression that adds a new required method to the protocol without updating the stub. Marginal value but cheap. |

## TestProcessableItemShape

| Line | Test | Verdict |
|---|---|---|
| 115 | `test_minimal_construction` | **Strong** — pins all default field values (`{}`, `""`, `None`) with strict equality |
| 123 | `test_full_construction` | **Strong** — strict equality on every field |
| 135 | `test_is_frozen` | **Strong** — pins immutability (FrozenInstanceError on mutation). Catches refactor that drops `frozen=True` |

## TestScanOutcomeShape

| Line | Test | Verdict |
|---|---|---|
| 142 | `test_default_zeroes` | **Strong** — pins zero-defaults (matters for downstream sum/aggregate logic) |
| 148 | `test_mutability` | **Strong** — confirms mutable (NOT frozen) — opposite of ProcessableItem; pins the data-class kind |

## Summary

- **12 tests** — 11 Strong, 1 Weak-but-keep
- Strong test_is_frozen / test_mutability pair pins the dataclass FROZEN vs MUTABLE distinction at both ends
- Registry contract fully covered (round-trip, string form, both error paths, override, listing)

**File verdict: STRONG.** Marginal value in `test_stub_satisfies_protocol` but not bug-blind. No changes needed.
