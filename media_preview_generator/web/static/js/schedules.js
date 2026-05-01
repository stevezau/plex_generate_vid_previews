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

        html += `
            <tr>
                <td>${escapeHtml(schedule.name)}${typeBadge}</td>
                <td>${escapeHtml(schedule.library_name) || 'All Libraries'}${_serverBadge(schedule)}</td>
                <td><code>${escapeHtml(cronDisplay)}</code></td>
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
