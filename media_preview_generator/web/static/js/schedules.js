// =========================================================================
// Schedules display — teaser card on the dashboard + full list on /schedules
//
// All read-side rendering for scheduled jobs. Sibling to schedule_modal.js
// which handles the create/edit modal. Previously lived inline in app.js
// (lines 1890-2136).
//
// Functions exported on window:
//   - describeSchedule(triggerType, triggerValue) — pure formatter that
//     turns a schedule's trigger spec into a human-readable string
//     ("Every 2 hours" / "Daily at 02:00 (Mon-Fri)" / "Cron: 0 3 * * *").
//   - updateScheduleTeaser() — renders the dashboard "Next Scheduled Run"
//     card (the small teaser at the top of the Jobs page).
//   - updateScheduleList() — renders the full table on /schedules.
//   - _formatRelativeToNow(dt) / _formatAbsoluteShort(dt) — internal date
//     helpers; private but kept on window so tests + future callers can
//     reuse them.
//
// External dependencies (resolved at call time via window globals defined
// in app.js): schedules, libraries, escapeHtml, showEditScheduleModal
// (from schedule_modal.js), toggleSchedule, runScheduleNow, deleteSchedule.
// Loaded AFTER schedule_modal.js in base.html so those refs resolve.
// =========================================================================

const DAY_NAMES = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];


function describeSchedule(triggerType, triggerValue) {
    if (triggerType === 'interval') {
        const mins = parseInt(triggerValue, 10);
        if (mins >= 60 && mins % 60 === 0) {
            const hrs = mins / 60;
            return hrs === 1 ? 'Every hour' : `Every ${hrs} hours`;
        }
        return mins === 1 ? 'Every minute' : `Every ${mins} minutes`;
    }

    if (triggerType !== 'cron' || !triggerValue) return triggerValue || '-';

    const parts = triggerValue.split(/\s+/);
    if (parts.length !== 5) return triggerValue;

    const [minute, hour, dom, month, dow] = parts;
    const isSimple = /^\d+$/.test(minute) && /^\d+$/.test(hour)
        && dom === '*' && month === '*' && /^[\d,]+$/.test(dow);

    if (isSimple) {
        const timeStr = `${hour.padStart(2, '0')}:${minute.padStart(2, '0')}`;
        // Convert APScheduler day (0=Mon) to Unix cron day (0=Sun) for DAY_NAMES lookup
        const dayNums = dow.split(',').map(d => (Number(d) + 1) % 7);
        const allDays = dayNums.length === 7;
        const weekdays = dayNums.length === 5
            && [1,2,3,4,5].every(d => dayNums.includes(d));
        const weekends = dayNums.length === 2
            && [0,6].every(d => dayNums.includes(d));

        let dayLabel;
        if (allDays) dayLabel = 'Daily';
        else if (weekdays) dayLabel = 'Weekdays';
        else if (weekends) dayLabel = 'Weekends';
        else dayLabel = dayNums.map(d => DAY_NAMES[d] || d).join(', ');

        return `${timeStr} ${dayLabel}`;
    }

    return triggerValue;
}

// Compact schedule summary rendered inside the Job Statistics card on the
// Dashboard.  Shows next upcoming run + a small configured/enabled count.
// The Automation page (Schedules tab) is the authoritative place for CRUD —
// this is just an at-a-glance pointer.
function updateScheduleTeaser() {
    const body = document.getElementById('scheduleTeaserBody');
    if (!body) return;

    if (!schedules || schedules.length === 0) {
        body.innerHTML =
            '<div class="d-flex align-items-center gap-2 text-muted small">' +
            '<i class="bi bi-calendar-x"></i>' +
            '<span>No schedules configured.</span>' +
            '<a href="/automation#schedules" class="ms-auto">Create one →</a>' +
            '</div>';
        return;
    }

    const total = schedules.length;
    const enabled = schedules.filter(s => s.enabled !== false);
    const enabledCount = enabled.length;
    const disabledCount = total - enabledCount;
    const upcoming = enabled
        .filter(s => s.next_run)
        .sort((a, b) => new Date(a.next_run) - new Date(b.next_run));
    const nextOne = upcoming[0] || null;

    const countWord = total === 1 ? 'schedule' : 'schedules';
    let summary = total + ' ' + countWord;
    if (total > 0 && enabledCount === total) {
        summary += ' · all enabled';
    } else if (enabledCount === 0) {
        summary += ' · all disabled';
    } else if (disabledCount === 1) {
        summary += ' · 1 disabled';
    } else {
        summary += ' · ' + disabledCount + ' disabled';
    }

    let topLine;
    if (nextOne) {
        const dt = new Date(nextOne.next_run);
        const rel = _formatRelativeToNow(dt);
        const absolute = _formatAbsoluteShort(dt);
        const tooltip = dt.toLocaleString();
        topLine =
            '<div class="d-flex align-items-baseline gap-2" title="' + escapeHtml(tooltip) + '">' +
            '<i class="bi bi-clock-history text-muted"></i>' +
            '<div class="text-truncate">' +
            '<span class="text-muted">Next:</span> ' +
            '<strong>' + escapeHtml(nextOne.name) + '</strong>' +
            '<span class="text-muted"> — ' + escapeHtml(rel) + '</span>' +
            '<span class="text-muted small ms-1">(' + escapeHtml(absolute) + ')</span>' +
            '</div>' +
            '</div>';
    } else {
        topLine =
            '<div class="d-flex align-items-center gap-2 text-muted">' +
            '<i class="bi bi-clock-history"></i>' +
            '<span>No upcoming runs</span>' +
            '</div>';
    }

    body.innerHTML =
        topLine +
        '<div class="small text-muted mt-1">' + escapeHtml(summary) + '</div>';
}

// Natural-language "time until" for the schedule teaser.  Kept separate from
// the generic formatDate helper so we can tune the copy for the dashboard
// without affecting other screens.
function _formatRelativeToNow(dt) {
    const diffMs = dt.getTime() - Date.now();
    const absMs = Math.abs(diffMs);
    const past = diffMs < 0;

    if (absMs < 45 * 1000) return past ? 'just now' : 'any moment now';

    const mins = Math.round(absMs / 60000);
    if (mins < 60) {
        const unit = mins === 1 ? 'minute' : 'minutes';
        return past ? mins + ' ' + unit + ' ago' : 'in ' + mins + ' ' + unit;
    }

    const hours = Math.round(mins / 60);
    if (hours < 24) {
        const unit = hours === 1 ? 'hour' : 'hours';
        return past ? 'about ' + hours + ' ' + unit + ' ago' : 'in about ' + hours + ' ' + unit;
    }

    // 24h+ — prefer day-anchored wording ("tomorrow", "Saturday", "in 2 weeks")
    if (past) {
        const days = Math.round(hours / 24);
        if (days === 1) return 'yesterday';
        if (days < 7) return days + ' days ago';
        if (days < 14) return '1 week ago';
        if (days < 30) return Math.round(days / 7) + ' weeks ago';
        return days + ' days ago';
    }

    const now = new Date();
    const nowMid = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    const dtMid = new Date(dt.getFullYear(), dt.getMonth(), dt.getDate()).getTime();
    const dayDiff = Math.round((dtMid - nowMid) / 86400000);

    if (dayDiff === 1) return 'tomorrow';
    if (dayDiff >= 2 && dayDiff <= 6) {
        const weekday = dt.toLocaleDateString(undefined, { weekday: 'long' });
        return 'on ' + weekday;
    }
    if (dayDiff === 7) return 'in 1 week';
    if (dayDiff < 14) return 'in ' + dayDiff + ' days';
    if (dayDiff < 30) return 'in ' + Math.round(dayDiff / 7) + ' weeks';
    return 'in ' + dayDiff + ' days';
}

// Short absolute timestamp for the schedule teaser hint line.  Shows just
// the time for runs today, weekday+time within a week, and date+time for
// anything farther out.  Locale-respecting.
function _formatAbsoluteShort(dt) {
    const now = new Date();
    const nowMid = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    const dtMid = new Date(dt.getFullYear(), dt.getMonth(), dt.getDate()).getTime();
    const dayDiff = Math.round((dtMid - nowMid) / 86400000);

    const timeStr = dt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    if (dayDiff === 0) return timeStr;
    if (dayDiff >= 1 && dayDiff <= 6) {
        const weekday = dt.toLocaleDateString(undefined, { weekday: 'short' });
        return weekday + ' ' + timeStr;
    }
    return dt.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) + ' ' + timeStr;
}

// D21 — quiet-hours config cache, populated by /api/quiet-hours.
// Used by updateScheduleList() to render an overlap warning on rows
// whose fire time falls inside the paused window, and by the schedule
// modal to flag a save that would land in the window.
window._quietHoursConfig = window._quietHoursConfig || null;

// D21 — parse "HH:MM" → [hour, minute] or null on bad input.
function _parseHHMM(value) {
    const v = String(value || '').trim();
    if (!v) return null;
    const m = /^(\d{1,2}):(\d{1,2})$/.exec(v);
    if (!m) return null;
    const h = parseInt(m[1], 10);
    const min = parseInt(m[2], 10);
    if (!(h >= 0 && h < 24 && min >= 0 && min < 60)) return null;
    return [h, min];
}

// D21 — true when ``minute`` (minutes-since-midnight) is inside the
// quiet-hours paused window. Handles cross-midnight (start > end).
function _isInQuietWindow(minute, startMin, endMin) {
    if (startMin === endMin) return false;
    if (startMin < endMin) return minute >= startMin && minute < endMin;
    return minute >= startMin || minute < endMin;
}

// D21 — for a schedule, return the time-of-day [hour, minute] it
// fires at IF it has a single fixed daily fire time (specific-time
// or simple "MIN HOUR * * *" cron). Returns null for interval or
// complex cron — we don't warn for those because some fires WILL
// land outside quiet hours and the user gets the partial behaviour
// they probably want.
function _scheduleFireTime(schedule) {
    if (!schedule || schedule.trigger_type !== 'cron' || !schedule.trigger_value) return null;
    const parts = String(schedule.trigger_value).split(/\s+/);
    if (parts.length !== 5) return null;
    if (!/^\d+$/.test(parts[0]) || !/^\d+$/.test(parts[1])) return null;
    const minute = parseInt(parts[0], 10);
    const hour = parseInt(parts[1], 10);
    if (!(hour >= 0 && hour < 24 && minute >= 0 && minute < 60)) return null;
    return [hour, minute];
}

// D21 — return a human-friendly tooltip string when ``schedule`` would
// fire inside the configured quiet-hours window. Returns "" when no
// overlap. Cached config in window._quietHoursConfig.
function _scheduleQuietHoursOverlap(schedule) {
    const qh = window._quietHoursConfig;
    if (!qh || !qh.enabled) return '';
    const startHM = _parseHHMM(qh.start);
    const endHM = _parseHHMM(qh.end);
    if (!startHM || !endHM || (startHM[0] === endHM[0] && startHM[1] === endHM[1])) return '';
    const fire = _scheduleFireTime(schedule);
    if (!fire) return '';
    const fireMin = fire[0] * 60 + fire[1];
    const startMin = startHM[0] * 60 + startHM[1];
    const endMin = endHM[0] * 60 + endHM[1];
    if (!_isInQuietWindow(fireMin, startMin, endMin)) return '';
    return 'This schedule fires at ' + String(fire[0]).padStart(2, '0') + ':' + String(fire[1]).padStart(2, '0')
        + ' which is inside Quiet Hours (' + qh.start + '–' + qh.end + ') — every fire will be skipped.';
}
window._scheduleQuietHoursOverlap = _scheduleQuietHoursOverlap;

function updateScheduleList() {
    // Always update the teaser card if it exists (Dashboard).
    updateScheduleTeaser();

    // Full schedule table only lives on the Schedules page.  When the
    // tbody isn't in the DOM, there's nothing else to render.
    const tbody = document.getElementById('scheduleList');
    if (!tbody) return;

    if (schedules.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="7" class="text-center text-muted py-4">
                    No schedules configured
                </td>
            </tr>
        `;
        return;
    }

    let html = '';

    for (const schedule of schedules) {
        const statusBadge = schedule.enabled ?
            '<span class="badge bg-success">Enabled</span>' :
            '<span class="badge bg-secondary">Disabled</span>';

        const cronDisplay = describeSchedule(schedule.trigger_type, schedule.trigger_value);

        const nextRun = schedule.next_run ? formatDate(schedule.next_run) : '-';

        const schedPri = schedule.priority || 2;
        const schedPriLabel = PRIORITY_LABELS[schedPri] || 'Normal';
        const schedPriBadge = PRIORITY_BADGE_CLASS[schedPri] || 'bg-primary';

        // Recently-added schedules get a subtle primary badge next to
        // the name so users can tell them apart from full-library scans.
        const cfg = schedule.config || {};
        const isRecentlyAdded = cfg.job_type === 'recently_added';
        const typeBadge = isRecentlyAdded
            ? ' <span class="badge bg-primary bg-opacity-25 text-primary" title="Scans items added in the last ' + (cfg.lookback_hours || 1) + 'h"><i class="bi bi-arrow-repeat me-1"></i>Recently Added</span>'
            : '';

        // D20 — show the optional stop-time inline next to the cron
        // summary so users can see at a glance which schedules pause
        // at a specific time of day.
        const stopBadge = schedule.stop_time
            ? ' <span class="badge bg-info text-dark" title="Pauses any running job from this schedule daily; resumes on next start">stops at ' + escapeHtml(schedule.stop_time) + '</span>'
            : '';

        // D21 — overlap warning. The processing_paused gate in
        // scheduler.execute_scheduled_job will silently skip this
        // schedule's fires during quiet hours; surface the conflict so
        // users don't think the schedule is broken.
        const overlapTip = _scheduleQuietHoursOverlap(schedule);
        const overlapBadge = overlapTip
            ? ' <i class="bi bi-exclamation-triangle text-warning" title="' + escapeHtml(overlapTip) + '" data-bs-toggle="tooltip"></i>'
            : '';

        html += `
            <tr>
                <td>${escapeHtml(schedule.name)}${typeBadge}${overlapBadge}</td>
                <td>${escapeHtml(schedule.library_name) || 'All Libraries'}${_serverBadge(schedule)}</td>
                <td><code>${escapeHtml(cronDisplay)}</code>${stopBadge}</td>
                <td><span class="badge ${schedPriBadge} priority-badge">${schedPriLabel}</span></td>
                <td>${nextRun}</td>
                <td>${statusBadge}</td>
                <td class="text-nowrap">
                    <button class="btn btn-sm btn-outline-primary me-1" onclick="runScheduleNow('${escapeHtml(schedule.id)}')" title="Run Now">
                        <i class="bi bi-play-fill"></i>
                    </button>
                    <button class="btn btn-sm btn-outline-info me-1" onclick="showEditScheduleModal('${escapeHtml(schedule.id)}')" title="Edit">
                        <i class="bi bi-pencil"></i>
                    </button>
                    <button class="btn btn-sm btn-outline-secondary me-1" onclick="toggleSchedule('${escapeHtml(schedule.id)}', ${!schedule.enabled})"
                            title="${schedule.enabled ? 'Disable' : 'Enable'}">
                        <i class="bi bi-${schedule.enabled ? 'pause' : 'play'}"></i>
                    </button>
                    <button class="btn btn-sm btn-outline-danger" onclick="deleteSchedule('${escapeHtml(schedule.id)}')" title="Delete">
                        <i class="bi bi-trash"></i>
                    </button>
                </td>
            </tr>
        `;
    }

    tbody.innerHTML = html;
}


// ---------------------------------------------------------------------------
// D21 — Quiet Hours card load / save / sync
// ---------------------------------------------------------------------------

async function loadQuietHours() {
    if (!document.getElementById('quietHoursSaveBtn')) return; // not on this page
    try {
        const data = await apiGet('/api/quiet-hours');
        window._quietHoursConfig = data || null;
    } catch (e) {
        window._quietHoursConfig = null;
        console.error('Failed to load quiet hours config:', e);
        return;
    }
    _renderQuietHoursCard();
    // Refresh schedule list so overlap badges reflect the new config.
    if (typeof updateScheduleList === 'function') updateScheduleList();
}
window.loadQuietHours = loadQuietHours;

function _renderQuietHoursCard() {
    const qh = window._quietHoursConfig || {};
    const enabledEl = document.getElementById('quietHoursEnabled');
    const startEl = document.getElementById('quietHoursStart');
    const endEl = document.getElementById('quietHoursEnd');
    if (!enabledEl || !startEl || !endEl) return;
    enabledEl.checked = !!qh.enabled;
    if (qh.start) startEl.value = qh.start;
    if (qh.end) endEl.value = qh.end;
    _refreshQuietHoursBadge();
}

// D21 — public so app.js's processing_paused_changed handler can call it
// to flip the badge between "on" and "paused now" without a refetch.
function _refreshQuietHoursBadge() {
    const badge = document.getElementById('quietHoursStateBadge');
    if (!badge) return;
    const qh = window._quietHoursConfig || {};
    if (!qh.enabled) {
        badge.textContent = 'off';
        badge.className = 'badge bg-secondary';
        badge.title = 'Quiet hours are disabled';
        return;
    }
    const pausedNow = !!(qh.currently_in_quiet_window
        || (typeof processingPaused !== 'undefined' && processingPaused));
    if (pausedNow) {
        badge.textContent = 'paused now';
        badge.className = 'badge bg-warning text-dark';
        badge.title = 'Quiet hours window is currently active — processing paused';
    } else {
        badge.textContent = 'on';
        badge.className = 'badge bg-info text-dark';
        badge.title = 'Quiet hours scheduled — currently outside the paused window';
    }
}
window._refreshQuietHoursBadge = _refreshQuietHoursBadge;

async function saveQuietHours() {
    const enabled = document.getElementById('quietHoursEnabled').checked;
    const start = document.getElementById('quietHoursStart').value;
    const end = document.getElementById('quietHoursEnd').value;
    if (!_parseHHMM(start)) {
        if (typeof showToast === 'function') showToast('Invalid time', 'Pause time must be HH:MM (00:00–23:59)', 'danger');
        return;
    }
    if (!_parseHHMM(end)) {
        if (typeof showToast === 'function') showToast('Invalid time', 'Resume time must be HH:MM (00:00–23:59)', 'danger');
        return;
    }
    if (enabled && start === end) {
        if (typeof showToast === 'function') showToast('Quiet hours', 'Pause and Resume can\'t be the same time. Pick a window.', 'warning');
        return;
    }
    const btn = document.getElementById('quietHoursSaveBtn');
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Saving…';
    }
    try {
        await apiPost('/api/quiet-hours', { enabled, start, end });
        await loadQuietHours();
        if (typeof showToast === 'function') showToast('Quiet hours', 'Saved', 'success');
    } catch (e) {
        if (typeof showToast === 'function') showToast('Save failed', (e && e.message) || 'Unknown error', 'danger');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '<i class="bi bi-check me-1"></i>Save';
        }
    }
}
window.saveQuietHours = saveQuietHours;

document.addEventListener('DOMContentLoaded', () => {
    const btn = document.getElementById('quietHoursSaveBtn');
    if (btn) {
        btn.addEventListener('click', saveQuietHours);
        loadQuietHours();
    }
});
