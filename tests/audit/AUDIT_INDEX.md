# Test-suite manual audit — master tracker

Per-file deep audit of every test in `tests/`. For each test:
- **Strong** — would catch the bug it claims to test
- **Weak** — assertion too loose (truthy / substring / `is not None`)
- **Tautological** — tests the mock, not the SUT
- **Bug-blind** — `assert_called_once()` without arg checks (D34 paradigm)
- **Dead/redundant** — same coverage as another test
- **Framework trivia** — tests pytest/Flask/loguru we don't own
- **Bug-locking** — asserts what the buggy code currently does
- **Needs human** — I can't judge; flagged for user review

| File | Tests | Audit doc | Status |
|---|---|---|---|
| test_basic.py | 10 | [AUDIT_test_basic.md](AUDIT_test_basic.md) | ✅ all Strong |
| test_eta_calculation.py | 8 | [AUDIT_test_eta_calculation.md](AUDIT_test_eta_calculation.md) | ✅ all Strong |
| test_timezone.py | 4 | [AUDIT_test_timezone.md](AUDIT_test_timezone.md) | ✅ all Strong |
| test_headers.py | 2 | [AUDIT_test_headers.md](AUDIT_test_headers.md) | ✅ all Strong |
| test_priority.py | 18 | [AUDIT_test_priority.md](AUDIT_test_priority.md) | ✅ all Strong |
| test_worker_naming.py | 9 | [AUDIT_test_worker_naming.md](AUDIT_test_worker_naming.md) | ✅ all Strong |
| test_processing_registry.py | 12 | [AUDIT_test_processing_registry.md](AUDIT_test_processing_registry.md) | ✅ 11 Strong, 1 Weak-keep |

**Progress: 7 / 66 files audited (~10%). 63 tests reviewed, 0 needs_human, 0 fixes required so far.**

Next batch: test_static_app_js, test_recent_added_scanner, test_processing_outcome, test_logging_config, test_security_fixes, test_servers_base.
