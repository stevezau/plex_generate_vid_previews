// =========================================================================
// Schedule create/edit modal + lifecycle actions
//
// All the UI for the modal that opens from the Schedules page when a user
// clicks "Add Schedule" or the edit icon next to a schedule row, plus the
// per-schedule lifecycle endpoints (toggle enabled/disabled, run-now,
// delete). Previously lived inline in app.js (lines 2631-2980).
//
// Functions exported on window:
//   - onScheduleTypeChange / onScanModeChange — radio-group change handlers
//   - showNewScheduleModal / showEditScheduleModal — modal open
//   - saveSchedule — form submit (POST /api/schedules or PUT /<id>)
//   - toggleSchedule, runScheduleNow, deleteSchedule — row actions
//   plus three private helpers: _getSelectedScheduleType, _resetScheduleForm
//
// External dependencies (defined in app.js, available as window globals):
//   showToast, escapeHtml, _renderScheduleLibraryList, updateScheduleList,
//   plus bootstrap.Modal. Loaded AFTER app.js in base.html so those refs
//   resolve.
// =========================================================================

function onScheduleTypeChange() {
    const selected = document.querySelector('input[name="scheduleType"]:checked').value;
    document.getElementById('scheduleFieldsTime').classList.toggle('d-none', selected !== 'specific-time');
    document.getElementById('scheduleFieldsInterval').classList.toggle('d-none', selected !== 'interval');
    document.getElementById('scheduleFieldsCron').classList.toggle('d-none', selected !== 'cron');
}

function onScanModeChange() {
    const selected = document.querySelector('input[name="scanMode"]:checked').value;
    const lookbackGroup = document.getElementById('scheduleLookbackGroup');
    if (lookbackGroup) {
        lookbackGroup.style.display = selected === 'recently_added' ? '' : 'none';
    }
    // Processing order only affects full-library scans — recently-added scans
    // touch a small, time-bounded set where shuffle is essentially a no-op.
    const sortByGroup = document.getElementById('scheduleSortByGroup');
    if (sortByGroup) {
        sortByGroup.style.display = selected === 'recently_added' ? 'none' : '';
    }
    // When flipping to recently-added in "Add" mode with untouched defaults,
    // nudge the trigger type to Interval and pre-fill 15 minutes — that's
    // the canonical shape of a Recently Added scanner.
    if (selected === 'recently_added') {
        const editId = document.getElementById('scheduleEditId').value;
        const intervalInput = document.getElementById('scheduleIntervalValue');
        if (!editId && intervalInput && intervalInput.value === '2') {
            document.getElementById('scheduleTypeInterval').checked = true;
            intervalInput.value = '15';
            document.getElementById('scheduleIntervalUnit').value = 'minutes';
            onScheduleTypeChange();
        }
    }
}

function _getSelectedScheduleType() {
    return document.querySelector('input[name="scheduleType"]:checked').value;
}

function _resetScheduleForm() {
    document.getElementById('scheduleName').value = '';
    const srvSel = document.getElementById('scheduleServer');
    if (srvSel) srvSel.value = '';
    document.getElementById('scheduleCron').value = '';
    document.getElementById('scheduleEditId').value = '';
    document.getElementById('scheduleEnabled').checked = true;
    document.getElementById('schedulePriority').value = '2';

    // Reset scan mode to Full library and hide lookback group
    document.getElementById('scanModeFull').checked = true;
    document.getElementById('scheduleLookback').value = '1';
    const sortByEl = document.getElementById('scheduleSortBy');
    if (sortByEl) sortByEl.value = '';
    onScanModeChange();

    // Reset schedule type to Specific Time
    document.getElementById('scheduleTypeTime').checked = true;
    onScheduleTypeChange();

    // Reset Specific Time fields
    document.getElementById('scheduleTime').value = '02:00';
    const defaultDays = new Set(['1', '2', '3', '4', '5']);
    document.querySelectorAll('.schedule-day').forEach(cb => {
        cb.checked = defaultDays.has(cb.value);
    });

    // Reset Interval fields
    document.getElementById('scheduleIntervalValue').value = '2';
    document.getElementById('scheduleIntervalUnit').value = 'hours';

    // Reset Cron Expression field
    document.getElementById('scheduleCronInput').value = '';
}

function showNewScheduleModal() {
    _resetScheduleForm();
    document.getElementById('scheduleModalTitle').innerHTML =
        '<i class="bi bi-calendar-plus me-2"></i>Add Schedule';
    document.getElementById('scheduleSubmitBtn').innerHTML =
        '<i class="bi bi-check me-1"></i>Create Schedule';

    _populateScheduleServerPicker();
    // Render the multi-select library checkboxes from the current global cache.
    _renderScheduleLibraryList(libraries, '');
    const modal = new bootstrap.Modal(document.getElementById('newScheduleModal'));
    modal.show();
}

function showEditScheduleModal(scheduleId) {
    const schedule = schedules.find(s => s.id === scheduleId);
    if (!schedule) {
        showToast('Error', 'Schedule not found', 'danger');
        return;
    }

    _resetScheduleForm();

    document.getElementById('scheduleEditId').value = schedule.id;
    document.getElementById('scheduleName').value = schedule.name || '';
    document.getElementById('scheduleEnabled').checked = schedule.enabled !== false;
    document.getElementById('schedulePriority').value = String(schedule.priority || 2);

    // Phase H7: pre-select the multi-select library checkboxes from
    // schedule.library_ids (with single-element fallback for legacy entries).
    const wantIds = Array.isArray(schedule.library_ids) && schedule.library_ids.length
        ? schedule.library_ids.map(String)
        : (schedule.library_id ? [String(schedule.library_id)] : []);

    function _applyLibraryPreselect() {
        const allCb = document.getElementById('scheduleLibraryAll');
        if (!wantIds.length) {
            // "All Libraries" semantics — leave master checkbox checked.
            if (allCb) allCb.checked = true;
            onScheduleLibraryAllChange(allCb);
            return;
        }
        if (allCb) allCb.checked = false;
        onScheduleLibraryAllChange(allCb);
        document.querySelectorAll('.schedule-library-checkbox').forEach(cb => {
            cb.disabled = false;
            cb.checked = wantIds.includes(String(cb.value));
        });
    }

    // Populate server picker, then refresh libraries scoped to it, then
    // pre-select the saved library_ids.
    _populateScheduleServerPicker(schedule.server_id || '').then(() => {
        if (schedule.server_id) {
            onScheduleServerChange().then(_applyLibraryPreselect);
        } else {
            _renderScheduleLibraryList(libraries, '');
            _applyLibraryPreselect();
        }
    });

    // Pre-fill scan mode + lookback from the schedule's config
    const cfg = schedule.config || {};
    if (cfg.job_type === 'recently_added') {
        document.getElementById('scanModeRecent').checked = true;
        const lookbackSelect = document.getElementById('scheduleLookback');
        const lookbackVal = String(cfg.lookback_hours || 1);
        if (Array.from(lookbackSelect.options).some(o => o.value === lookbackVal)) {
            lookbackSelect.value = lookbackVal;
        }
    } else {
        document.getElementById('scanModeFull').checked = true;
    }
    const sortBySelect = document.getElementById('scheduleSortBy');
    if (sortBySelect) {
        const savedSortBy = cfg.sort_by || '';
        if (Array.from(sortBySelect.options).some(o => o.value === savedSortBy)) {
            sortBySelect.value = savedSortBy;
        } else {
            sortBySelect.value = '';
        }
    }
    onScanModeChange();

    if (schedule.trigger_type === 'interval' && schedule.trigger_value) {
        // Interval schedule: populate interval fields
        document.getElementById('scheduleTypeInterval').checked = true;
        const totalMinutes = parseInt(schedule.trigger_value, 10);
        if (totalMinutes >= 60 && totalMinutes % 60 === 0) {
            document.getElementById('scheduleIntervalValue').value = String(totalMinutes / 60);
            document.getElementById('scheduleIntervalUnit').value = 'hours';
        } else {
            document.getElementById('scheduleIntervalValue').value = String(totalMinutes);
            document.getElementById('scheduleIntervalUnit').value = 'minutes';
        }
    } else if (schedule.trigger_type === 'cron' && schedule.trigger_value) {
        const parts = schedule.trigger_value.split(/\s+/);
        const isSimpleTimeDays = parts.length === 5
            && /^\d+$/.test(parts[0])
            && /^\d+$/.test(parts[1])
            && parts[2] === '*'
            && parts[3] === '*'
            && /^[\d,]+$/.test(parts[4]);

        if (isSimpleTimeDays) {
            // Simple time+days pattern: use the Specific Time UI
            document.getElementById('scheduleTypeTime').checked = true;
            document.getElementById('scheduleTime').value =
                `${parts[1].padStart(2, '0')}:${parts[0].padStart(2, '0')}`;
            // Convert APScheduler day (0=Mon) back to Unix cron day (0=Sun)
            const cronDays = parts[4].split(',').map(d => String((parseInt(d.trim()) + 1) % 7));
            document.querySelectorAll('.schedule-day').forEach(cb => {
                cb.checked = cronDays.includes(cb.value);
            });
        } else {
            // Complex cron: show the raw cron input
            document.getElementById('scheduleTypeCron').checked = true;
            document.getElementById('scheduleCronInput').value = schedule.trigger_value;
        }
    }
    onScheduleTypeChange();

    document.getElementById('scheduleModalTitle').innerHTML =
        '<i class="bi bi-pencil me-2"></i>Edit Schedule';
    document.getElementById('scheduleSubmitBtn').innerHTML =
        '<i class="bi bi-check me-1"></i>Save Changes';

    const modal = new bootstrap.Modal(document.getElementById('newScheduleModal'));
    modal.show();
}

async function saveSchedule() {
    const editId = document.getElementById('scheduleEditId').value;
    const name = document.getElementById('scheduleName').value.trim();
    if (!name) {
        showToast('Error', 'Name is required', 'danger');
        return;
    }

    const scheduleType = _getSelectedScheduleType();
    // Phase H7: collect selected library_ids from the checkbox group.
    // Empty list → "All Libraries" master is checked → backend treats as None.
    const allLibsCb = document.getElementById('scheduleLibraryAll');
    let selectedLibraryIds = [];
    if (allLibsCb && !allLibsCb.checked) {
        selectedLibraryIds = Array.from(document.querySelectorAll('.schedule-library-checkbox:checked'))
            .map(cb => cb.value);
        if (selectedLibraryIds.length === 0) {
            showToast('Error', 'Select at least one library or check "All Libraries"', 'warning');
            return;
        }
    }
    // Display name: a single library uses its name; multi shows count.
    let libraryDisplay = 'All Libraries';
    if (selectedLibraryIds.length === 1) {
        const lib = libraries.find(l => String(l.id) === String(selectedLibraryIds[0]));
        libraryDisplay = lib ? lib.name : 'Selected Library';
    } else if (selectedLibraryIds.length > 1) {
        libraryDisplay = `${selectedLibraryIds.length} libraries`;
    }
    const scanMode = document.querySelector('input[name="scanMode"]:checked').value;

    // Build the config blob — recently-added schedules carry their
    // lookback_hours value through the same config dict that user
    // schedules already use.
    const scheduleConfig = { job_type: scanMode };
    if (scanMode === 'recently_added') {
        scheduleConfig.lookback_hours = parseFloat(document.getElementById('scheduleLookback').value) || 1;
    } else {
        // Processing order only applies to full-library scans
        const sortByEl = document.getElementById('scheduleSortBy');
        const sortBy = sortByEl ? sortByEl.value : '';
        if (sortBy) {
            scheduleConfig.sort_by = sortBy;
        }
    }

    const serverSelect = document.getElementById('scheduleServer');
    const serverId = serverSelect ? serverSelect.value : '';

    const payload = {
        name: name,
        library_id: selectedLibraryIds.length === 1 ? selectedLibraryIds[0] : null,
        library_ids: selectedLibraryIds,
        library_name: libraryDisplay,
        server_id: serverId || null,
        enabled: document.getElementById('scheduleEnabled').checked,
        priority: parseInt(document.getElementById('schedulePriority').value, 10) || 2,
        config: scheduleConfig,
    };

    if (scheduleType === 'specific-time') {
        const timeValue = document.getElementById('scheduleTime').value;
        if (!timeValue) {
            showToast('Error', 'Time is required', 'danger');
            return;
        }
        const selectedDays = Array.from(document.querySelectorAll('.schedule-day:checked')).map(cb => cb.value);
        if (selectedDays.length === 0) {
            showToast('Error', 'Select at least one day', 'danger');
            return;
        }
        const [hours, minutes] = timeValue.split(':');
        // Convert Unix cron day (0=Sun) to APScheduler day (0=Mon)
        const apsDays = selectedDays.map(d => (parseInt(d) + 6) % 7);
        payload.cron_expression = `${parseInt(minutes)} ${parseInt(hours)} * * ${apsDays.join(',')}`;
    } else if (scheduleType === 'interval') {
        const intervalValue = parseInt(document.getElementById('scheduleIntervalValue').value, 10);
        if (!intervalValue || intervalValue < 1) {
            showToast('Error', 'Interval must be at least 1', 'danger');
            return;
        }
        const unit = document.getElementById('scheduleIntervalUnit').value;
        payload.interval_minutes = unit === 'hours' ? intervalValue * 60 : intervalValue;
    } else if (scheduleType === 'cron') {
        const cronInput = document.getElementById('scheduleCronInput').value.trim();
        if (!cronInput) {
            showToast('Error', 'Cron expression is required', 'danger');
            return;
        }
        const parts = cronInput.split(/\s+/);
        if (parts.length !== 5) {
            showToast('Error', 'Cron expression must have 5 fields (minute hour day-of-month month day-of-week)', 'danger');
            return;
        }
        payload.cron_expression = cronInput;
    }

    try {
        if (editId) {
            await apiPut(`/api/schedules/${editId}`, payload);
            showToast('Schedule Updated', `Schedule "${name}" updated successfully`, 'success');
        } else {
            await apiPost('/api/schedules', payload);
            showToast('Schedule Created', `Schedule "${name}" created successfully`, 'success');
        }

        bootstrap.Modal.getInstance(document.getElementById('newScheduleModal')).hide();
        loadSchedules();
    } catch (error) {
        const action = editId ? 'update' : 'create';
        showToast('Error', `Failed to ${action} schedule: ` + error.message, 'danger');
    }
}

async function toggleSchedule(scheduleId, enabled) {
    try {
        await apiPut(`/api/schedules/${scheduleId}`, { enabled: enabled });
        loadSchedules();
    } catch (error) {
        showToast('Error', 'Failed to update schedule: ' + error.message, 'danger');
    }
}

async function runScheduleNow(scheduleId) {
    try {
        await apiPost(`/api/schedules/${scheduleId}/run`);
        loadJobs();
        loadJobStats();
        showToast('Schedule Triggered', 'Schedule has been triggered', 'success');
    } catch (error) {
        showToast('Error', 'Failed to run schedule: ' + error.message, 'danger');
    }
}

async function deleteSchedule(scheduleId) {
    if (!confirm('Are you sure you want to delete this schedule?')) return;

    try {
        await apiDelete(`/api/schedules/${scheduleId}`);
        loadSchedules();
        showToast('Schedule Deleted', 'Schedule has been deleted', 'info');
    } catch (error) {
        showToast('Error', 'Failed to delete schedule: ' + error.message, 'danger');
    }
}
