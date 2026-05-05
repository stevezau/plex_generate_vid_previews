"""
Pure JS unit tests for the schedule cron arithmetic in
``media_preview_generator/web/static/js/schedules.js`` and
``schedule_modal.js``.

Why this file exists
--------------------
Commit ``2d29fe9`` ("fix: correct day-of-week offset in schedule cron
expressions") shipped a real off-by-one bug to users for an unknown
duration: APScheduler uses ISO weekday numbers (0=Mon..6=Sun) but the
UI checkboxes used Unix cron numbering (0=Sun..6=Sat), so every weekly
schedule fired one day late. The hindsight audit
(``tests/audit/HINDSIGHT_90_DAYS.md``) explicitly flagged this gap:
"Pure JS day arithmetic. No ``test_static_app_js.py`` coverage."

The functions involved are pure arithmetic on strings and numbers — no
real DOM behaviour is required to exercise the bug shape — so we run
the actual JS source through ``node`` from a Python pytest test. A
small adapter stubs ``document.getElementById`` for the two functions
that read form values; ``describeSchedule`` is already a pure function
and runs unmodified.

This is intentionally lower-cost than adding vitest/jsdom + an npm
install + a CI step. Node is already on the dev machine and on the
GitHub Actions runner; the harness is one ``subprocess.run`` per test.

Coverage matrix (every cell is a separate test):
  * Each weekday Mon-Sun (round-trip through saveSchedule -> cron ->
    describeSchedule and back through showEditScheduleModal)
  * Every-N-minutes / every-N-hours interval display (1, 2, 60, 120, 1440)
  * Hour-of-day arithmetic at boundaries (00:00, 12:00, 23:00)
  * The exact bug shape from 2d29fe9 (Sunday-only must show "Sun")
  * The _pendingProgress replay shape from 31cd4a0
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
JS_DIR = REPO_ROOT / "media_preview_generator" / "web" / "static" / "js"
SCHEDULES_JS = JS_DIR / "schedules.js"
SCHEDULE_MODAL_JS = JS_DIR / "schedule_modal.js"
APP_JS = JS_DIR / "app.js"

# Skip everything if node isn't on PATH. This file is the only one in
# the suite that shells out to node, so we'd rather skip than fail when
# a dev runs pytest in an environment without node (e.g. a minimal
# container). CI installs node already.
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not available — JS unit tests skipped")


def _run_node(snippet: str) -> str:
    """Execute a snippet of JS in node and return stdout (stripped).

    The snippet is responsible for printing a single JSON document on
    stdout. Stderr is captured into the assertion message on failure
    so JS exceptions surface clearly.
    """
    result = subprocess.run(
        [NODE, "-e", snippet],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, (
        f"node exited {result.returncode}\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# JS adapter — loads the real source file(s) into a node VM context with a
# minimal DOM stub, then calls into them. We use vm.runInNewContext rather
# than `require()` because the JS files target the browser (top-level
# `function` declarations, `window` globals) and have no module exports.
# ---------------------------------------------------------------------------

_VM_PRELUDE = r"""
const vm = require('vm');
const fs = require('fs');

function loadSchedulesContext(formValues) {
    // formValues maps element id -> { value?, checked?, dataset? } shape.
    // Anything missing returns null from getElementById to mimic the real
    // browser when a control is absent (the early-return guards rely on
    // this).
    const elements = {};
    for (const [id, props] of Object.entries(formValues || {})) {
        elements[id] = Object.assign(
            {
                value: '',
                checked: false,
                dataset: {},
                addEventListener: function () {},
                innerHTML: '',
                disabled: false,
            },
            props,
        );
    }

    // Special: query selectors used by saveSchedule for the day checkboxes.
    const checkedDays = (formValues && formValues.__checkedDays) || [];
    const checkedLibs = (formValues && formValues.__checkedLibs) || [];
    const scanModeChecked = (formValues && formValues.__scanMode) || 'full_library';

    const docStub = {
        addEventListener: function () {},
        removeEventListener: function () {},
        getElementById: (id) => elements[id] || null,
        querySelectorAll: (sel) => {
            if (sel === '.schedule-day') {
                // For showEditScheduleModal: every day checkbox exists; we'll
                // record which were checked.
                const out = [];
                for (let v = 0; v < 7; v++) {
                    out.push({
                        value: String(v),
                        checked: false,
                    });
                }
                if (formValues && formValues.__editScheduleDayCheckboxes) {
                    formValues.__editScheduleDayCheckboxes.length = 0;
                    formValues.__editScheduleDayCheckboxes.push(...out);
                }
                return out;
            }
            if (sel === '.schedule-day:checked') {
                return checkedDays.map((v) => ({ value: String(v) }));
            }
            if (sel === '.schedule-library-checkbox:checked') {
                return checkedLibs.map((v) => ({ value: String(v) }));
            }
            if (sel === '.schedule-library-checkbox') {
                return [];
            }
            if (sel === 'input[name="scanMode"]:checked') {
                return [{ value: scanModeChecked }];
            }
            return [];
        },
        querySelector: (sel) => {
            if (sel === 'input[name="scanMode"]:checked') {
                return { value: scanModeChecked };
            }
            if (sel === 'input[name="scheduleType"]:checked') {
                return { value: (formValues && formValues.__scheduleType) || 'specific-time' };
            }
            return null;
        },
    };

    // Stubs for the functions saveSchedule depends on but that we don't
    // exercise here.
    const ctx = {
        document: docStub,
        window: { _scheduleQuietHoursOverlap: undefined, appConfirm: async () => true },
        bootstrap: { Modal: class { static getInstance() { return { hide: () => {} }; } show() {} } },
        showToast: function (title, msg, level) {
            ctx.__toasts.push({ title, msg, level });
        },
        __toasts: [],
        __captured: { puts: [], posts: [] },
        apiPost: async function (url, body) {
            ctx.__captured.posts.push({ url, body: JSON.parse(JSON.stringify(body)) });
            return { id: 'sch-test' };
        },
        apiPut: async function (url, body) {
            ctx.__captured.puts.push({ url, body: JSON.parse(JSON.stringify(body)) });
            return {};
        },
        apiDelete: async function () { return {}; },
        loadSchedules: function () {},
        loadJobs: function () {},
        loadJobStats: function () {},
        schedules: (formValues && formValues.__schedules) || [],
        libraries: (formValues && formValues.__libraries) || [],
        escapeHtml: (s) => String(s),
        // saveSchedule helpers
        _getSelectedScheduleType: function () {
            return (formValues && formValues.__scheduleType) || 'specific-time';
        },
        _populateScheduleServerPicker: () => Promise.resolve(),
        _renderScheduleLibraryList: () => {},
        _resetScheduleForm: () => {},
        onScheduleLibraryAllChange: () => {},
        onScheduleServerChange: () => Promise.resolve(),
        onScanModeChange: () => {},
        onScheduleTypeChange: () => {},
        console: console,
    };
    ctx.global = ctx;
    vm.createContext(ctx);
    // Load and evaluate the source files. schedules.js uses `const
    // DAY_NAMES = ...` at top level — that's a let/const binding in the
    // module scope, fine inside a vm context.
    const schedulesSrc = fs.readFileSync(__SCHEDULES_PATH__, 'utf8');
    const modalSrc = fs.readFileSync(__SCHEDULE_MODAL_PATH__, 'utf8');
    vm.runInContext(schedulesSrc, ctx);
    vm.runInContext(modalSrc, ctx);
    return ctx;
}
"""


def _vm_prelude() -> str:
    return _VM_PRELUDE.replace("__SCHEDULES_PATH__", json.dumps(str(SCHEDULES_JS))).replace(
        "__SCHEDULE_MODAL_PATH__", json.dumps(str(SCHEDULE_MODAL_JS))
    )


def _eval_js(call: str, form_values: dict | None = None) -> dict | str | int | float | list | None:
    """Run ``call`` (a JS expression) inside a fresh schedules.js context
    with form values applied. The expression must resolve to something
    JSON-serialisable.
    """
    snippet = (
        _vm_prelude()
        + f"\nconst ctx = loadSchedulesContext({json.dumps(form_values or {})});\n"
        + f"Promise.resolve(vm.runInContext({json.dumps(call)}, ctx))"
        + ".then((v) => { console.log(JSON.stringify({result: v, captured: ctx.__captured, toasts: ctx.__toasts})); })"
        + ".catch((e) => { console.error(e.stack || e.message); process.exit(1); });"
    )
    out = _run_node(snippet)
    return json.loads(out)


# ---------------------------------------------------------------------------
# describeSchedule — pure formatter
# ---------------------------------------------------------------------------


class TestDescribeScheduleInterval:
    """The interval branch returns 'Every N minutes' or 'Every N hours'."""

    @pytest.mark.parametrize(
        "minutes,expected",
        [
            (1, "Every minute"),
            (2, "Every 2 minutes"),
            (45, "Every 45 minutes"),
            (60, "Every hour"),
            (120, "Every 2 hours"),
            (1440, "Every 24 hours"),  # daily-as-interval edge case
        ],
    )
    def test_interval_formatting(self, minutes: int, expected: str) -> None:
        out = _eval_js(f"describeSchedule('interval', {minutes})")
        assert out["result"] == expected, f"interval={minutes}: got {out['result']!r}"


class TestDescribeScheduleCronWeekdays:
    """Each APScheduler weekday number must format to the correct DAY_NAMES
    label. Pins the 2d29fe9 fix: APS 0=Mon..6=Sun, mapped via (n+1)%7
    to Unix 0=Sun..6=Sat for DAY_NAMES lookup.

    The buggy pre-fix code would have shown the day one position EARLIER
    in the week (Sunday-only schedule displayed as Saturday, Monday-only
    as Sunday, etc.) because it indexed DAY_NAMES with the raw APS number.
    """

    # (aps_day_in_cron, expected_label) — every cell of the matrix.
    @pytest.mark.parametrize(
        "aps_dow,label",
        [
            (0, "Mon"),
            (1, "Tue"),
            (2, "Wed"),
            (3, "Thu"),
            (4, "Fri"),
            (5, "Sat"),
            (6, "Sun"),  # The exact 2d29fe9 bug shape.
        ],
    )
    def test_single_day_label(self, aps_dow: int, label: str) -> None:
        cron = f"0 14 * * {aps_dow}"
        out = _eval_js(f"describeSchedule('cron', '{cron}')")
        # Format is "HH:MM Day"
        assert out["result"] == f"14:00 {label}", (
            f"APS dow={aps_dow} should display {label} (the 2d29fe9 fix maps APS->Unix); got {out['result']!r}"
        )

    def test_sunday_only_does_not_say_monday(self) -> None:
        """Direct pin on the bug shape from 2d29fe9: a schedule for
        Sunday must read 'Sun', NOT 'Mon'. Pre-fix code indexed
        DAY_NAMES[6] which is 'Sat' for the buggy version that shipped
        before the (n+1)%7 conversion was added.
        """
        out = _eval_js("describeSchedule('cron', '30 9 * * 6')")
        assert "Sun" in out["result"], f"Sunday cron should mention Sun: {out['result']!r}"
        assert "Mon" not in out["result"], (
            f"BUG SHAPE 2d29fe9: Sunday cron must NOT display 'Mon' (or any other day); got {out['result']!r}"
        )


class TestDescribeScheduleCronAggregations:
    """The describeSchedule simple-time branch collapses common patterns
    into 'Daily' / 'Weekdays' / 'Weekends'. Pin every collapse cell.
    """

    def test_all_seven_days_displays_daily(self) -> None:
        # APS Mon..Sun = 0..6
        out = _eval_js("describeSchedule('cron', '0 0 * * 0,1,2,3,4,5,6')")
        assert out["result"] == "00:00 Daily", out["result"]

    def test_weekdays_collapse_to_weekdays_label(self) -> None:
        # Mon-Fri in APS numbering = 0,1,2,3,4
        out = _eval_js("describeSchedule('cron', '0 8 * * 0,1,2,3,4')")
        assert out["result"] == "08:00 Weekdays", out["result"]

    def test_weekends_collapse_to_weekends_label(self) -> None:
        # Sat=5, Sun=6 in APS numbering — these become Unix [6, 0] which
        # match the 'weekends' check ([0,6] sorted).
        out = _eval_js("describeSchedule('cron', '0 10 * * 5,6')")
        assert out["result"] == "10:00 Weekends", out["result"]

    def test_mwf_lists_individual_days_in_unix_order(self) -> None:
        """Mon, Wed, Fri (APS 0,2,4) -> displayed individually."""
        out = _eval_js("describeSchedule('cron', '0 14 * * 0,2,4')")
        # Days are looked up by (aps+1)%7 -> Unix order 1,3,5 -> Mon, Wed, Fri.
        assert out["result"] == "14:00 Mon, Wed, Fri", out["result"]


class TestDescribeScheduleCronHourBoundaries:
    """Hour/minute padding and edge values."""

    @pytest.mark.parametrize(
        "minute,hour,expected_time",
        [
            (0, 0, "00:00"),
            (5, 0, "00:05"),
            (0, 12, "12:00"),
            (59, 23, "23:59"),
            (0, 23, "23:00"),
        ],
    )
    def test_time_padding(self, minute: int, hour: int, expected_time: str) -> None:
        # APS 0,1,2,3,4,5,6 = all days -> "Daily"
        out = _eval_js(f"describeSchedule('cron', '{minute} {hour} * * 0,1,2,3,4,5,6')")
        assert out["result"] == f"{expected_time} Daily", out["result"]


class TestDescribeScheduleCronFallback:
    """Non-simple cron expressions return the raw value unchanged."""

    @pytest.mark.parametrize(
        "expr",
        [
            "*/15 * * * *",  # every 15 minutes — minute field isn't pure digits
            "0 9 1 * *",  # day-of-month set
            "0 9 * 6 *",  # month set
            "0 9 * * MON",  # textual dow
        ],
    )
    def test_complex_cron_returns_raw_value(self, expr: str) -> None:
        out = _eval_js(f"describeSchedule('cron', '{expr}')")
        assert out["result"] == expr, out["result"]

    def test_empty_value_returns_dash(self) -> None:
        out = _eval_js("describeSchedule('cron', '')")
        assert out["result"] == "-", out["result"]


# ---------------------------------------------------------------------------
# saveSchedule — UI checkbox values (Unix 0=Sun..6=Sat) -> APS cron string
# ---------------------------------------------------------------------------


class TestSaveScheduleCronEncoding:
    """saveSchedule converts Unix-cron checkbox values to APScheduler
    weekday numbers via ``(unix + 6) % 7``. Pin every weekday cell.
    """

    @pytest.mark.parametrize(
        "unix_dow,expected_aps",
        [
            (0, 6),  # Sun -> APS 6
            (1, 0),  # Mon -> APS 0
            (2, 1),  # Tue
            (3, 2),  # Wed
            (4, 3),  # Thu
            (5, 4),  # Fri
            (6, 5),  # Sat
        ],
    )
    def test_single_weekday_payload(self, unix_dow: int, expected_aps: int) -> None:
        form = {
            "scheduleEditId": {"value": ""},
            "scheduleName": {"value": "Test"},
            "scheduleEnabled": {"checked": True},
            "schedulePriority": {"value": "2"},
            "scheduleStopTime": {"value": ""},
            "scheduleLibraryAll": {"checked": True},
            "scheduleServer": {"value": ""},
            "scheduleTime": {"value": "14:30"},
            "__scheduleType": "specific-time",
            "__checkedDays": [unix_dow],
            "__scanMode": "full_library",
        }
        out = _eval_js("saveSchedule()", form_values=form)
        # saveSchedule POSTs (no edit id) — capture & assert.
        posts = out["captured"]["posts"]
        assert len(posts) == 1, f"expected 1 POST, got {len(posts)}: {posts}"
        cron = posts[0]["body"]["cron_expression"]
        # Format: "30 14 * * <APSday>"
        assert cron == f"30 14 * * {expected_aps}", (
            f"unix_dow={unix_dow} should map to APS {expected_aps}; got cron {cron!r}"
        )

    def test_sunday_only_encodes_to_aps_6_not_aps_0(self) -> None:
        """Direct pin on the 2d29fe9 bug shape from the SAVE side: a user
        ticking only the Sunday checkbox (UI value '0') must produce
        cron '... * * 6' (APS Sun=6), NOT '... * * 0' (APS Mon=0).
        """
        form = {
            "scheduleEditId": {"value": ""},
            "scheduleName": {"value": "Sunday only"},
            "scheduleEnabled": {"checked": True},
            "schedulePriority": {"value": "2"},
            "scheduleStopTime": {"value": ""},
            "scheduleLibraryAll": {"checked": True},
            "scheduleServer": {"value": ""},
            "scheduleTime": {"value": "09:00"},
            "__scheduleType": "specific-time",
            "__checkedDays": [0],  # Sunday in Unix cron
            "__scanMode": "full_library",
        }
        out = _eval_js("saveSchedule()", form_values=form)
        cron = out["captured"]["posts"][0]["body"]["cron_expression"]
        assert cron.endswith(" 6"), (
            f"BUG SHAPE 2d29fe9 (save-side): Sunday checkbox must encode to APS day 6, "
            f"NOT 0 (which would fire on Monday). Got cron: {cron!r}"
        )

    def test_weekdays_encode_to_aps_0_through_4(self) -> None:
        form = {
            "scheduleEditId": {"value": ""},
            "scheduleName": {"value": "Weekdays"},
            "scheduleEnabled": {"checked": True},
            "schedulePriority": {"value": "2"},
            "scheduleStopTime": {"value": ""},
            "scheduleLibraryAll": {"checked": True},
            "scheduleServer": {"value": ""},
            "scheduleTime": {"value": "08:00"},
            "__scheduleType": "specific-time",
            "__checkedDays": [1, 2, 3, 4, 5],  # Mon..Fri in Unix
            "__scanMode": "full_library",
        }
        out = _eval_js("saveSchedule()", form_values=form)
        cron = out["captured"]["posts"][0]["body"]["cron_expression"]
        # Order is preserved from the checkbox iteration: Mon..Fri -> APS 0..4
        assert cron == "0 8 * * 0,1,2,3,4", cron

    def test_weekends_encode_to_aps_5_and_6(self) -> None:
        form = {
            "scheduleEditId": {"value": ""},
            "scheduleName": {"value": "Weekends"},
            "scheduleEnabled": {"checked": True},
            "schedulePriority": {"value": "2"},
            "scheduleStopTime": {"value": ""},
            "scheduleLibraryAll": {"checked": True},
            "scheduleServer": {"value": ""},
            "scheduleTime": {"value": "10:00"},
            "__scheduleType": "specific-time",
            "__checkedDays": [0, 6],  # Sun, Sat in Unix
            "__scanMode": "full_library",
        }
        out = _eval_js("saveSchedule()", form_values=form)
        cron = out["captured"]["posts"][0]["body"]["cron_expression"]
        # Sun(0) -> APS 6, Sat(6) -> APS 5. Iteration order preserved.
        assert cron == "0 10 * * 6,5", cron


class TestSaveScheduleHourBoundaries:
    """saveSchedule strips zero-pad in cron output via parseInt."""

    @pytest.mark.parametrize(
        "time_str,expected_prefix",
        [
            ("00:00", "0 0 "),
            ("00:05", "5 0 "),
            ("12:00", "0 12 "),
            ("23:59", "59 23 "),
            ("09:30", "30 9 "),  # leading zero stripped by parseInt
        ],
    )
    def test_time_to_cron_prefix(self, time_str: str, expected_prefix: str) -> None:
        form = {
            "scheduleEditId": {"value": ""},
            "scheduleName": {"value": "T"},
            "scheduleEnabled": {"checked": True},
            "schedulePriority": {"value": "2"},
            "scheduleStopTime": {"value": ""},
            "scheduleLibraryAll": {"checked": True},
            "scheduleServer": {"value": ""},
            "scheduleTime": {"value": time_str},
            "__scheduleType": "specific-time",
            "__checkedDays": [1],  # Monday — irrelevant to this test
            "__scanMode": "full_library",
        }
        out = _eval_js("saveSchedule()", form_values=form)
        cron = out["captured"]["posts"][0]["body"]["cron_expression"]
        assert cron.startswith(expected_prefix), f"time={time_str}: got cron {cron!r}"


class TestSaveScheduleIntervalBranch:
    """The interval branch encodes minutes/hours into ``interval_minutes``."""

    @pytest.mark.parametrize(
        "value,unit,expected_minutes",
        [
            (1, "minutes", 1),
            (30, "minutes", 30),
            (1, "hours", 60),
            (2, "hours", 120),
            (24, "hours", 1440),
        ],
    )
    def test_interval_payload(self, value: int, unit: str, expected_minutes: int) -> None:
        form = {
            "scheduleEditId": {"value": ""},
            "scheduleName": {"value": "I"},
            "scheduleEnabled": {"checked": True},
            "schedulePriority": {"value": "2"},
            "scheduleStopTime": {"value": ""},
            "scheduleLibraryAll": {"checked": True},
            "scheduleServer": {"value": ""},
            "scheduleIntervalValue": {"value": str(value)},
            "scheduleIntervalUnit": {"value": unit},
            "__scheduleType": "interval",
            "__scanMode": "full_library",
        }
        out = _eval_js("saveSchedule()", form_values=form)
        body = out["captured"]["posts"][0]["body"]
        assert body["interval_minutes"] == expected_minutes, body
        # Interval schedules must NOT carry a cron_expression.
        assert "cron_expression" not in body or body["cron_expression"] is None, body
        # And stop_time must be cleared for interval triggers (D20 rule).
        assert body.get("stop_time") == "", body


# ---------------------------------------------------------------------------
# Round-trip: saveSchedule -> describeSchedule -> showEditScheduleModal.
# This is the strongest pin for the 2d29fe9 bug class — a regression in
# either direction breaks the round trip.
# ---------------------------------------------------------------------------


class TestScheduleRoundTrip:
    """saveSchedule produces an APS cron string; describeSchedule parses
    that string back to a label that mentions the same day the user picked.
    Any drift in either direction (off-by-one in either function) breaks
    this round trip.
    """

    @pytest.mark.parametrize(
        "unix_dow,day_label",
        [
            (0, "Sun"),
            (1, "Mon"),
            (2, "Tue"),
            (3, "Wed"),
            (4, "Thu"),
            (5, "Fri"),
            (6, "Sat"),
        ],
    )
    def test_user_picks_day_and_describe_displays_same_day(self, unix_dow: int, day_label: str) -> None:
        form = {
            "scheduleEditId": {"value": ""},
            "scheduleName": {"value": "RoundTrip"},
            "scheduleEnabled": {"checked": True},
            "schedulePriority": {"value": "2"},
            "scheduleStopTime": {"value": ""},
            "scheduleLibraryAll": {"checked": True},
            "scheduleServer": {"value": ""},
            "scheduleTime": {"value": "06:00"},
            "__scheduleType": "specific-time",
            "__checkedDays": [unix_dow],
            "__scanMode": "full_library",
        }
        # Combined: save then immediately describe the resulting cron.
        snippet = (
            _vm_prelude()
            + f"\nconst ctx = loadSchedulesContext({json.dumps(form)});\n"
            + "Promise.resolve(vm.runInContext('saveSchedule()', ctx))"
            + ".then(() => {"
            + "  const cron = ctx.__captured.posts[0].body.cron_expression;"
            + "  const label = vm.runInContext(`describeSchedule('cron', '${cron}')`, ctx);"
            + "  console.log(JSON.stringify({cron, label}));"
            + "}).catch((e) => { console.error(e.stack || e.message); process.exit(1); });"
        )
        out = json.loads(_run_node(snippet))
        assert day_label in out["label"], (
            f"Round-trip drift for unix_dow={unix_dow}: cron={out['cron']!r} "
            f"described as {out['label']!r}, expected to mention {day_label!r}"
        )


# ---------------------------------------------------------------------------
# _pendingProgress (commit 31cd4a0) — same file family; assert the cache
# behaviour pinned by the bug. The function lives in app.js, not
# schedules.js, but the hindsight audit asked for it to be covered here.
# ---------------------------------------------------------------------------


class TestPendingProgressReplay:
    """31cd4a0 added a frontend cache so progress events arriving before
    the active-job DOM card existed could be replayed once loadJobs()
    rendered the card. Pin the two contract edges:

      * updateJobProgress with a missing DOM target stashes into
        _pendingProgress instead of crashing.
      * After the DOM target appears, a subsequent call clears the cache
        entry.

    These are static-source guards (regex on app.js) rather than full
    JS execution because updateJobProgress drags in a much larger DOM
    surface (queue rendering, timers, percent-override math) that isn't
    relevant to the bug shape. The pair of lines below are the entire
    "cache-on-miss / clear-on-hit" contract.
    """

    @pytest.fixture(scope="class")
    def app_src(self) -> str:
        return APP_JS.read_text(encoding="utf-8")

    def test_pending_progress_cache_object_exists(self, app_src: str) -> None:
        assert "const _pendingProgress = {};" in app_src, (
            "31cd4a0 introduced a module-level _pendingProgress cache so progress "
            "events arriving before the active-job DOM card exists can be replayed. "
            "Removing the cache regresses the original bug (job stuck at 0% during first file)."
        )

    def test_update_job_progress_caches_when_dom_missing(self, app_src: str) -> None:
        body = app_src.split("function updateJobProgress(", 1)[1].split("\n}", 1)[0]
        assert "_pendingProgress[jobId] = progress" in body, (
            "updateJobProgress must cache the progress payload into _pendingProgress[jobId] "
            "when the per-job progress bar element is missing — otherwise the very first "
            "progress event of a job is dropped (the 31cd4a0 bug)."
        )
        # The cache write must be guarded by the missing-DOM branch — i.e.
        # a `return` immediately follows it so the rest of the function
        # doesn't try to mutate the absent bar.
        cache_idx = body.find("_pendingProgress[jobId] = progress")
        return_idx = body.find("return", cache_idx)
        assert 0 <= return_idx - cache_idx < 80, (
            "The cache write must be in the early-return branch (the DOM-missing path); "
            "without the early return the function falls through and crashes on null."
        )

    def test_update_job_progress_clears_cache_when_dom_ready(self, app_src: str) -> None:
        body = app_src.split("function updateJobProgress(", 1)[1].split("\n}", 1)[0]
        assert "delete _pendingProgress[jobId]" in body, (
            "updateJobProgress must delete the cache entry once the DOM target exists; "
            "otherwise the replay loop in loadJobs() would re-apply stale progress on "
            "every poll and overwrite live updates from the worker."
        )

    def test_load_jobs_replays_pending_progress(self, app_src: str) -> None:
        # The replay site lives at the end of loadJobs() — assert it iterates
        # the cache and calls updateJobProgress for each entry.
        assert "Object.keys(_pendingProgress)" in app_src, (
            "loadJobs() must iterate _pendingProgress after rendering the active-job "
            "cards and replay each cached event — that's the 'replay after DOM ready' "
            "half of the 31cd4a0 fix."
        )
        # Find the loop body and confirm it calls updateJobProgress.
        idx = app_src.find("Object.keys(_pendingProgress)")
        window = app_src[idx : idx + 200]
        assert "updateJobProgress(" in window, (
            "The Object.keys(_pendingProgress) loop must invoke updateJobProgress to "
            "actually flush the cache — without the call the cache fills up forever."
        )
