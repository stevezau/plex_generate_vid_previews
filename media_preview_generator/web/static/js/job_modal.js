// =========================================================================
// Job Details modal — Logs tab + Files tab
//
// Contents previously lived inline in app.js (lines 1732 + 1862-2472)
// and are the modal that opens from the Jobs page when a user clicks
// a job row. Two functionally-related tabs:
//
//   * Logs tab: showLogsModal, refreshLogs, pollNewLogs, loadEarlierLogs,
//     loadAllLogs, colorizeLogLine, filterLogs, clearLogSearch, copyLogs,
//     downloadLogs, plus a 5-second polling interval for running jobs.
//   * Files tab (Per-File Results): onFilesTabActivated, refreshFileResults,
//     renderFileResultsTable, renderFilePagination, the page navigation
//     helpers, and the outcome-filter dropdown handler.
//
// Kept together rather than as two files because the two tabs share the
// same modal lifecycle (showLogsModal resets the Files tab state, and
// the polling loop also refreshes files when the Files tab is active).
//
// External dependencies (defined in app.js, available as window globals):
//   _lastNotifiedJobId, jobs, _renderPublishersBlock, escapeHtml,
//   apiGet, copyToClipboard, showToast, plus bootstrap.Modal /
//   bootstrap.Tab. Loaded AFTER app.js in base.html so those refs
//   resolve at call time.
// =========================================================================

let logsRefreshInterval = null;

// Logs Functions
let _rawLogs = [];
let _logsModalJobId = null;
// When the modal's target Job is a retry-chain row, ``_logsModalAttemptId``
// holds the UUID of the per-attempt child Job currently selected in the
// Attempts dropdown. The three log-fetch functions (``refreshLogs``,
// ``pollNewLogs``, ``loadEarlierLogs``) route their API calls to this
// ID instead of ``_logsModalJobId`` so the user sees that attempt's
// real INFO/WARNING-coloured log instead of the chain row's synthesized
// status text.
let _logsModalAttemptId = null;
let _logsTotalLines = 0;
let _logsLoadedOffset = 0;
let _logsKnownCount = 0;
const _LOGS_CHUNK_SIZE = 500;

// ID the log-fetch functions should target. For non-chain rows this is
// just ``_logsModalJobId``; for chain rows it's the dropdown's
// currently-selected per-attempt UUID. Centralised so every API call
// site reads from one place — pre-fix this was duplicated and one
// branch forgot to consult the dropdown.
function _logsTargetId() {
    return _logsModalAttemptId || _logsModalJobId;
}

function showLogsModal(jobId) {
    const targetId = jobId || _lastNotifiedJobId;
    if (!targetId) return;
    _logsModalJobId = targetId;
    _logsModalAttemptId = null;

    document.getElementById('logsJobId').textContent = `Job ID: ${targetId}`;
    document.getElementById('logsSearchInput').value = '';

    // Phase H8: render the per-publisher header for this job.
    const _job = jobs.find(j => j.id === targetId);
    const _hdr = document.getElementById('logsModalPublishers');
    if (_hdr) _hdr.innerHTML = _job ? _renderPublishersBlock(_job) : '';

    // Retry-chain rows show the Attempts dropdown so the user can flip
    // between per-firing logs (each is a real Job with its own log
    // file on disk, hidden from the main list — see
    // ``api_jobs.py``'s ``include_retry_attempts`` opt-in). Populating
    // the dropdown sets ``_logsModalAttemptId``, which the log-fetch
    // functions then target.
    const _attemptsWrap = document.getElementById('attemptsDropdownWrap');
    if (_attemptsWrap) {
        const isChainRow = !!(_job && _job.config && _job.config.is_retry_chain);
        if (isChainRow) {
            _attemptsWrap.classList.remove('d-none');
            _attemptsWrap.classList.add('d-flex');
            _loadAttemptsDropdown(targetId);
        } else {
            _attemptsWrap.classList.add('d-none');
            _attemptsWrap.classList.remove('d-flex');
            document.getElementById('attemptsDropdown').innerHTML = '';
            document.getElementById('attemptsHint').textContent = '';
        }
    }

    _rawLogs = [];
    _logsTotalLines = 0;
    _logsLoadedOffset = 0;
    _logsKnownCount = 0;
    _updateEarlierLogsButton();

    // Reset Files tab state
    _fileResultsActiveFilter = '';
    _fileResultsLoaded = false;
    _filePage = 1;
    _fileProcessedTotal = 0;
    _fileListTruncated = false;
    document.getElementById('fileResultsBody').innerHTML =
        '<tr><td colspan="4" class="text-muted text-center">Click to load file results</td></tr>';
    document.getElementById('fileResultsCount').textContent = '';
    document.getElementById('fileResultsSearch').value = '';
    var _ofSel = document.getElementById('fileOutcomeFilter');
    if (_ofSel) _ofSel.value = '';
    var pFooter = document.getElementById('filePaginationFooter');
    if (pFooter) pFooter.classList.add('d-none');

    // Reset to Logs tab
    var logsTabBtn = document.getElementById('logsTab');
    if (logsTabBtn) {
        var tab = new bootstrap.Tab(logsTabBtn);
        tab.show();
    }

    const job = jobs.find(j => j.id === targetId);
    const isRunning = job && job.status === 'running';
    const autoScrollEl = document.getElementById('logsAutoScroll');
    autoScrollEl.checked = isRunning;

    const modal = new bootstrap.Modal(document.getElementById('logsModal'));
    modal.show();

    refreshLogs();

    if (logsRefreshInterval) clearInterval(logsRefreshInterval);
    if (isRunning) {
        logsRefreshInterval = setInterval(function() {
            pollNewLogs();
            // D27 — always refresh files (not just when the Files tab is
            // active) so switching to the tab mid-run shows current
            // data instantly, not a 5s-stale snapshot. Loading the
            // file list when invisible is cheap; the JSONL parser is
            // O(filtered+page) thanks to the cap + pagination work.
            if (_fileResultsLoaded) refreshFileResults();
        }, 5000);
    }

    document.getElementById('logsModal').addEventListener('hidden.bs.modal', function() {
        if (logsRefreshInterval) {
            clearInterval(logsRefreshInterval);
            logsRefreshInterval = null;
        }
        _logsModalJobId = null;
        _logsModalAttemptId = null;
    }, { once: true });
}

function scrollLogsTo(position) {
    const el = document.getElementById('logsContent');
    if (!el) return;
    _suppressScrollDetect = Date.now() + 600;
    if (position === 'top') {
        el.scrollTo({ top: 0, behavior: 'smooth' });
        document.getElementById('logsAutoScroll').checked = false;
    } else {
        el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
    }
}

let _suppressScrollDetect = 0;
document.addEventListener('DOMContentLoaded', () => {
    const el = document.getElementById('logsContent');
    if (!el) return;
    el.addEventListener('scroll', () => {
        if (Date.now() < _suppressScrollDetect) return;
        const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
        if (!atBottom) {
            document.getElementById('logsAutoScroll').checked = false;
        }
    });
});

function colorizeLogLine(line) {
    const escaped = escapeHtml(line);
    let levelClass = '';
    if (/\bDEBUG\b/i.test(line)) levelClass = 'log-level-debug';
    else if (/\bINFO\b/i.test(line)) levelClass = 'log-level-info';
    else if (/\bWARNING\b/i.test(line)) levelClass = 'log-level-warning';
    else if (/\bERROR\b/i.test(line)) levelClass = 'log-level-error';
    else if (/\bCRITICAL\b/i.test(line)) levelClass = 'log-level-critical';
    return `<span class="log-line ${levelClass}">${escaped}</span>`;
}

async function refreshLogs() {
    const targetId = _logsTargetId();
    if (!targetId) return;

    try {
        const probe = await apiGet(`/api/jobs/${targetId}/logs?offset=0&limit=0`);
        const total = probe.total_lines || 0;
        _logsTotalLines = total;

        const logsContent = document.getElementById('logsContent');
        const lineCountEl = document.getElementById('logsLineCount');
        const autoScroll = document.getElementById('logsAutoScroll').checked;

        if (probe.log_cleared_by_retention) {
            _rawLogs = [];
            _logsLoadedOffset = 0;
            _logsKnownCount = 0;
            logsContent.innerHTML = [
                '<div class="alert alert-info mb-0" role="alert">',
                '<i class="bi bi-info-circle me-2"></i>',
                'Log file was cleared due to log retention policy.',
                '</div>'
            ].join('');
            if (lineCountEl) lineCountEl.textContent = '';
            _updateEarlierLogsButton();
            return;
        }

        if (total === 0) {
            _rawLogs = [];
            _logsLoadedOffset = 0;
            _logsKnownCount = 0;
            logsContent.innerHTML = '<span class="text-muted">No logs available yet...</span>';
            if (lineCountEl) lineCountEl.textContent = '';
            _updateEarlierLogsButton();
            return;
        }

        const startOffset = Math.max(0, total - _LOGS_CHUNK_SIZE);
        const data = await apiGet(`/api/jobs/${targetId}/logs?offset=${startOffset}&limit=${_LOGS_CHUNK_SIZE}`);
        const lines = data.logs || [];

        _rawLogs = lines;
        _logsLoadedOffset = startOffset;
        _logsKnownCount = total;

        logsContent.innerHTML = lines.map(colorizeLogLine).join('\n');
        filterLogs();

        if (lineCountEl) {
            const showing = startOffset > 0
                ? `Showing last ${lines.length.toLocaleString()} of ${total.toLocaleString()} log lines`
                : `${total.toLocaleString()} log lines`;
            lineCountEl.textContent = showing;
        }
        _updateEarlierLogsButton();

        if (autoScroll) {
            _suppressScrollDetect = Date.now() + 600;
            logsContent.scrollTo({ top: logsContent.scrollHeight, behavior: 'smooth' });
        }
    } catch (error) {
        console.error('Failed to load logs:', error);
    }
}

async function pollNewLogs() {
    const targetId = _logsTargetId();
    if (!targetId) return;

    try {
        const data = await apiGet(`/api/jobs/${targetId}/logs?offset=${_logsKnownCount}&limit=${_LOGS_CHUNK_SIZE}`);
        const newLines = data.logs || [];
        const newTotal = data.total_lines || _logsKnownCount;

        if (newLines.length === 0) {
            _logsTotalLines = newTotal;
            return;
        }

        const logsContent = document.getElementById('logsContent');
        const lineCountEl = document.getElementById('logsLineCount');
        const autoScroll = document.getElementById('logsAutoScroll').checked;

        _rawLogs = _rawLogs.concat(newLines);
        _logsKnownCount = newTotal;
        _logsTotalLines = newTotal;

        const fragment = document.createDocumentFragment();
        const query = (document.getElementById('logsSearchInput').value || '').toLowerCase();
        newLines.forEach(line => {
            const wrapper = document.createElement('span');
            wrapper.innerHTML = colorizeLogLine(line);
            const el = wrapper.firstChild;
            if (query && !(line.toLowerCase().includes(query))) {
                el.classList.add('log-line-hidden');
            }
            fragment.appendChild(el);
            fragment.appendChild(document.createTextNode('\n'));
        });

        const placeholder = logsContent.querySelector('.text-muted');
        if (placeholder && !logsContent.querySelector('.log-line')) {
            logsContent.innerHTML = '';
        }

        logsContent.appendChild(fragment);

        if (lineCountEl) {
            const loaded = _rawLogs.length;
            const showing = _logsLoadedOffset > 0
                ? `Showing ${loaded.toLocaleString()} of ${newTotal.toLocaleString()} log lines`
                : `${newTotal.toLocaleString()} log lines`;
            lineCountEl.textContent = showing;
        }

        if (autoScroll) {
            _suppressScrollDetect = Date.now() + 600;
            logsContent.scrollTo({ top: logsContent.scrollHeight, behavior: 'smooth' });
        }
    } catch (error) {
        console.error('Failed to poll new logs:', error);
    }
}

async function loadEarlierLogs() {
    const targetId = _logsTargetId();
    if (!targetId || _logsLoadedOffset <= 0) return;

    const btn = document.getElementById('logsLoadEarlierBtn');
    if (btn) { btn.disabled = true; btn.textContent = 'Loading...'; }

    try {
        const newStart = Math.max(0, _logsLoadedOffset - _LOGS_CHUNK_SIZE);
        const limit = _logsLoadedOffset - newStart;
        const data = await apiGet(`/api/jobs/${targetId}/logs?offset=${newStart}&limit=${limit}`);
        const earlierLines = data.logs || [];

        if (earlierLines.length === 0) {
            _logsLoadedOffset = 0;
            _updateEarlierLogsButton();
            return;
        }

        const logsContent = document.getElementById('logsContent');
        const prevScrollHeight = logsContent.scrollHeight;

        _rawLogs = earlierLines.concat(_rawLogs);
        _logsLoadedOffset = newStart;

        const fragment = document.createDocumentFragment();
        const query = (document.getElementById('logsSearchInput').value || '').toLowerCase();
        earlierLines.forEach(line => {
            const wrapper = document.createElement('span');
            wrapper.innerHTML = colorizeLogLine(line);
            const el = wrapper.firstChild;
            if (query && !(line.toLowerCase().includes(query))) {
                el.classList.add('log-line-hidden');
            }
            fragment.appendChild(el);
            fragment.appendChild(document.createTextNode('\n'));
        });

        logsContent.insertBefore(fragment, logsContent.firstChild);
        logsContent.scrollTop += logsContent.scrollHeight - prevScrollHeight;

        const lineCountEl = document.getElementById('logsLineCount');
        if (lineCountEl) {
            const loaded = _rawLogs.length;
            const showing = _logsLoadedOffset > 0
                ? `Showing ${loaded.toLocaleString()} of ${_logsTotalLines.toLocaleString()} log lines`
                : `${_logsTotalLines.toLocaleString()} log lines`;
            lineCountEl.textContent = showing;
        }
        _updateEarlierLogsButton();
    } catch (error) {
        console.error('Failed to load earlier logs:', error);
    } finally {
        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="bi bi-arrow-up me-1"></i>Load earlier logs'; }
    }
}

async function loadAllLogs() {
    const targetId = _logsTargetId();
    if (!targetId) return;

    const btn = document.getElementById('logsLoadAllBtn');
    if (btn) { btn.disabled = true; btn.textContent = 'Loading...'; }

    try {
        const data = await apiGet(`/api/jobs/${targetId}/logs`);
        const allLines = data.logs || [];

        _rawLogs = allLines;
        _logsTotalLines = allLines.length;
        _logsLoadedOffset = 0;
        _logsKnownCount = allLines.length;

        const logsContent = document.getElementById('logsContent');
        logsContent.innerHTML = allLines.map(colorizeLogLine).join('\n');
        filterLogs();

        const lineCountEl = document.getElementById('logsLineCount');
        if (lineCountEl) {
            lineCountEl.textContent = `${allLines.length.toLocaleString()} log lines`;
        }
        _updateEarlierLogsButton();
    } catch (error) {
        console.error('Failed to load all logs:', error);
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Load all'; }
    }
}

// ========================================================================
// Attempts dropdown — chain-row drill-down
// ========================================================================
//
// Retry-chain rows surface as ONE row in the dashboard (commit
// f0f08a6 introduced per-firing Jobs but they're hidden from the
// main list by default — see api_jobs.py's ``include_retry_attempts``
// flag). Drilling into a chain row's modal must let the user flip
// between each firing's real log. This pair of helpers does the
// fetch (``_loadAttemptsDropdown``) and the on-change redirect
// (``onAttemptSelected``).
//
// The countdown timer (next-attempt-in-Ns) on the chain row is
// rendered by the existing data-scheduled-at tick loop in app.js
// (see ``_updateElapsedTimers``); the modal header doesn't need its
// own — the user can glance back at the row for that.

const _ATTEMPT_STATUS_GLYPH = {
    'completed': '✓',
    'failed':    '✗',
    'cancelled': '⊘',
    'running':   '⏳',
    'pending':   '·',
};

function _formatAttemptDuration(secs) {
    if (secs === null || secs === undefined) return '';
    const s = Math.max(0, Math.round(secs));
    if (s < 60) return s + 's';
    const m = Math.floor(s / 60);
    const rem = s % 60;
    return rem ? (m + 'm' + rem + 's') : (m + 'm');
}

async function _loadAttemptsDropdown(chainId) {
    const select = document.getElementById('attemptsDropdown');
    const hint = document.getElementById('attemptsHint');
    if (!select) return;
    select.innerHTML = '<option disabled>Loading attempts…</option>';
    try {
        const data = await apiGet('/api/jobs/' + encodeURIComponent(chainId) + '/attempts');
        const attempts = data.attempts || [];
        if (attempts.length === 0) {
            // No firings recorded yet — fall back to viewing the chain
            // row's own synthesized status log so the modal isn't blank.
            select.innerHTML = '<option value="" selected>No attempts yet — showing chain status</option>';
            _logsModalAttemptId = null;
            if (hint) hint.textContent = '';
            refreshLogs();
            return;
        }
        const max = data.max_attempts || attempts[attempts.length - 1].retry_attempt;
        let options = '';
        for (let i = 0; i < attempts.length; i++) {
            const a = attempts[i];
            const glyph = _ATTEMPT_STATUS_GLYPH[a.status] || '?';
            const dur = _formatAttemptDuration(a.duration_sec);
            const durLabel = dur ? ' · ' + dur : '';
            const label = glyph + ' Attempt ' + a.retry_attempt + ' of ' + max
                + ' — ' + a.status + durLabel;
            options += '<option value="' + escapeHtml(a.id) + '">'
                + escapeHtml(label) + '</option>';
        }
        select.innerHTML = options;
        // Default-select the LATEST attempt (the one the user most likely
        // wants to see). Attempts are sorted ascending by retry_attempt,
        // so the last option is newest.
        select.selectedIndex = attempts.length - 1;
        _logsModalAttemptId = attempts[attempts.length - 1].id;
        if (hint) {
            hint.textContent = attempts.length === max
                ? attempts.length + ' of ' + max + ' attempts'
                : attempts.length + ' attempts so far (max ' + max + ')';
        }
        refreshLogs();
    } catch (error) {
        console.error('Failed to load attempts dropdown:', error);
        select.innerHTML = '<option disabled>Could not load attempts</option>';
        if (hint) hint.textContent = 'Error loading attempts — see console.';
        _logsModalAttemptId = null;
    }
}

function onAttemptSelected(select) {
    if (!select || !select.value) return;
    _logsModalAttemptId = select.value;
    // Reset log state so refreshLogs() loads from offset 0 for the
    // newly-selected attempt instead of continuing the previous
    // attempt's pagination.
    _rawLogs = [];
    _logsTotalLines = 0;
    _logsLoadedOffset = 0;
    _logsKnownCount = 0;
    document.getElementById('logsContent').innerHTML = '<span class="text-muted">Loading…</span>';
    _updateEarlierLogsButton();
    refreshLogs();
}

function _updateEarlierLogsButton() {
    const wrap = document.getElementById('logsEarlierWrap');
    if (!wrap) return;
    if (_logsLoadedOffset > 0) {
        wrap.classList.remove('d-none');
    } else {
        wrap.classList.add('d-none');
    }
}

function filterLogs() {
    const query = (document.getElementById('logsSearchInput').value || '').toLowerCase();
    const lines = document.querySelectorAll('#logsContent .log-line');

    lines.forEach((line, idx) => {
        const text = (_rawLogs[idx] || '').toLowerCase();
        if (!query || text.includes(query)) {
            line.classList.remove('log-line-hidden');
            if (query) {
                const escaped = escapeHtml(_rawLogs[idx]);
                const re = new RegExp(`(${escapeHtml(query).replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`, 'gi');
                line.innerHTML = escaped.replace(re, '<mark class="log-highlight">$1</mark>');
            }
        } else {
            line.classList.add('log-line-hidden');
        }
    });
}

function clearLogSearch() {
    document.getElementById('logsSearchInput').value = '';
    filterLogs();
    // Re-render without highlights
    const logsContent = document.getElementById('logsContent');
    if (_rawLogs.length > 0) {
        logsContent.innerHTML = _rawLogs.map(colorizeLogLine).join('\n');
    }
}

async function copyLogs() {
    const text = _rawLogs.length > 0 ? _rawLogs.join('\n') : '';
    if (!text.trim()) {
        showToast('Warning', 'No logs to copy', 'warning');
        return;
    }
    await copyToClipboard(text, 'Logs copied to clipboard', 'Failed to copy logs');
}

async function downloadLogs() {
    const targetId = _logsTargetId();
    if (!targetId) {
        showToast('Warning', 'No logs to download', 'warning');
        return;
    }

    try {
        const data = await apiGet(`/api/jobs/${targetId}/logs`);
        const allLines = data.logs || [];
        if (allLines.length === 0) {
            showToast('Warning', 'No logs to download', 'warning');
            return;
        }
        const text = allLines.join('\n');
        const blob = new Blob([text], { type: 'text/plain' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `job-${targetId}-logs.txt`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    } catch (error) {
        console.error('Failed to download logs:', error);
        showToast('Error', 'Failed to download logs', 'danger');
    }
}

// ========================================================================
// Per-File Results (Files tab in the Job Details modal)
// ========================================================================

var _fileResultsActiveFilter = '';
var _fileResultsLoaded = false;
var _filePage = 1;
var _filePerPage = parseInt(localStorage.getItem('filePerPage'), 10) || 100;
var _fileTotalPages = 1;
var _fileFilteredCount = 0;
var _fileTotal = 0;
// D35 — JSONL file-list is capped at 5k rows; processed_total is the real
// run scope from job.progress.outcome, surfaced so the truncated pagination
// wording can render "Showing 1–100 of 5,000 files in list (117,981 items
// processed — list truncated for performance)". list_truncated is the
// authoritative signal from the backend (presence of the "truncated"
// sentinel row in the JSONL).
var _fileProcessedTotal = 0;
var _fileListTruncated = false;
var _fileSearchDebounce = null;

// D14 — every status chip reads from the unified STATUS_META map in
// app.js so per-row file-outcome badges, per-server pills, and aggregate
// badges all render the same label + color for the same status. Update
// STATUS_META, not this shim.
function _fileOutcomeMeta(key) {
    var m = (typeof window !== 'undefined' && window._statusMeta)
        ? window._statusMeta(key)
        : { label: key, cls: 'bg-secondary' };
    return { label: m.label, badge: m.cls };
}

function onFilesTabActivated() {
    if (!_fileResultsLoaded) {
        refreshFileResults();
    }
}

async function refreshFileResults() {
    var targetId = _logsModalJobId;
    if (!targetId) return;
    try {
        var params = 'page=' + _filePage + '&per_page=' + _filePerPage;
        if (_fileResultsActiveFilter) params += '&outcome=' + encodeURIComponent(_fileResultsActiveFilter);
        var search = (document.getElementById('fileResultsSearch').value || '').trim();
        if (search) params += '&search=' + encodeURIComponent(search);

        var data = await apiGet('/api/jobs/' + targetId + '/files?' + params);
        _fileResultsLoaded = true;
        _filePage = data.page || 1;
        _fileTotalPages = data.total_pages || 1;
        _fileFilteredCount = data.filtered_count || 0;
        _fileTotal = data.total || 0;
        _fileProcessedTotal = data.processed_total || 0;
        _fileListTruncated = !!data.list_truncated;

        renderFileResultsTable(data.files || []);
        renderFilePagination();
    } catch (e) {
        document.getElementById('fileResultsBody').innerHTML =
            '<tr><td colspan="4" class="text-muted text-center">Could not load file results</td></tr>';
    }
}

function renderFileResultsTable(files) {
    var tbody = document.getElementById('fileResultsBody');
    var countEl = document.getElementById('fileResultsCount');

    if (!files || files.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" class="text-muted text-center">No matching files</td></tr>';
        countEl.textContent = _fileFilteredCount === 0 && _fileTotal > 0
            ? '0 of ' + _fileTotal + ' files match'
            : '';
        return;
    }

    var start = (_filePage - 1) * _filePerPage + 1;
    var end = start + files.length - 1;
    var label = 'Showing ' + start + '\u2013' + end + ' of ' + _fileFilteredCount.toLocaleString();
    if (_fileFilteredCount !== _fileTotal) label += ' (' + _fileTotal.toLocaleString() + ' in list)';
    if (_fileListTruncated) label += ' \u2014 ' + _fileProcessedTotal.toLocaleString() + ' items processed';
    countEl.textContent = label;

    var html = '';
    for (var i = 0; i < files.length; i++) {
        var f = files[i];
        var meta = _fileOutcomeMeta(f.outcome);
        var fileName = f.file || '';
        var shortName = fileName.split('/').pop() || fileName;
        var reason = escapeHtml(f.reason || '');
        var worker = escapeHtml(f.worker || '');
        // D9 \u2014 per-server pills tell the user which server each file
        // landed on. For single-server installs it's one pill; for
        // multi-server fan-out it's one per target.
        var serversHtml = _renderFileServerPills(f.servers || []);
        // D11 \u2014 for files that have a BIF on disk (generated this run
        // OR already existed), show a shortcut to /bif-viewer pre-loaded
        // with this file. Skipped/failed-with-no-output files don't get
        // the shortcut \u2014 there'd be nothing to preview.
        //
        // Bug fix: the button used to live inside the same `text-truncate`
        // td as the filename, so a long path would push it off-screen
        // (Bootstrap text-truncate sets white-space:nowrap + overflow:
        // hidden). Wrap in a flex row with `flex-shrink-0` on the button
        // so the filename truncates around it instead of swallowing it.
        var inspectorBtn = '';
        if (fileName && (f.outcome === 'generated' || f.outcome === 'skipped_bif_exists' || f.outcome === 'skipped_output_exists' || f.outcome === 'published')) {
            // D34 — when the per-file row carries the absolute BIF path
            // (recorded by Worker._capture_publishers from the publisher
            // result), deep-link straight to it so the viewer skips the
            // Plex title-search heuristic. The title-search path was
            // mis-resolving episodes whose release-group suffix happened
            // to look like a season/episode tag (e.g. "Fire Country" hit
            // "Fire Country (2022)E17 - …-NTb" which the SxxExx regex
            // fixed but the underlying search is still a guess).
            // Falling back to ?file=<source_path> when no bif_path is
            // present keeps older job histories functional.
            var bifPath = f.bif_path || '';
            var inspectorHref;
            if (bifPath) {
                inspectorHref = '/bif-viewer?bif=' + encodeURIComponent(bifPath);
            } else {
                inspectorHref = '/bif-viewer?file=' + encodeURIComponent(fileName);
            }
            inspectorBtn = '<a href="' + inspectorHref
                + '" target="_blank" rel="noopener" class="btn btn-sm btn-outline-secondary py-0 px-1 ms-2 flex-shrink-0"'
                + ' title="Open in Preview Inspector"><i class="bi bi-eye"></i></a>';
        }

        html += '<tr>'
            + '<td style="max-width: 400px;">'
            +   '<div class="d-flex align-items-center">'
            +     '<small class="text-truncate" title="' + escapeHtml(fileName) + '">' + escapeHtml(shortName) + '</small>'
            +     inspectorBtn
            +   '</div>'
            + '</td>'
            + '<td><span class="badge ' + meta.badge + '">' + meta.label + '</span></td>'
            + '<td>' + serversHtml + '</td>'
            + '<td><small class="text-muted" title="' + reason + '">' + reason + '</small></td>'
            + '<td><small class="text-muted">' + worker + '</small></td>'
            + '</tr>';
    }
    tbody.innerHTML = html;
}

// D9 \u2014 render the per-server attribution pills for a file row. Each
// `servers` entry has {id, name, type, status, frame_source?}. The
// vendor palette colours the pill (so users can spot Plex vs Emby at a
// glance), and STATUS_META in app.js drives the tooltip text so the
// per-server pill says exactly what the file-outcome chip says.
var _FILE_SERVER_PALETTE = {
    plex:     'bg-warning text-dark',
    emby:     'bg-success',
    jellyfin: 'bg-info text-dark',
};
function _renderFileServerPills(servers) {
    if (!servers || !servers.length) return '<small class="text-muted">&mdash;</small>';
    var html = '';
    for (var i = 0; i < servers.length; i++) {
        var s = servers[i] || {};
        var t = String(s.type || '').toLowerCase();
        var cls = _FILE_SERVER_PALETTE[t] || 'bg-secondary';
        var label = s.name || (t ? t.charAt(0).toUpperCase() + t.slice(1) : 'Server');
        var status = String(s.status || '').toLowerCase();
        // Dim the pill (lower opacity) when the publisher didn't actually
        // publish \u2014 gives the user a one-glance "this server got it" vs
        // "this server skipped it" signal without needing a second column.
        var dim = (status && status !== 'published') ? ' style="opacity:.55;"' : '';
        var meta = _fileOutcomeMeta(status);
        var tip = meta.label || status || '';
        var title = tip ? (escapeHtmlAttr(label) + ' \u2014 ' + escapeHtmlAttr(tip)) : escapeHtmlAttr(label);
        html += '<span class="badge me-1 ' + cls + '"' + dim + ' title="' + title + '">'
            + escapeHtml(label) + '</span>';
    }
    return html;
}

function renderFilePagination() {
    var footer = document.getElementById('filePaginationFooter');
    var info = document.getElementById('filePaginationInfo');
    var controls = document.getElementById('filePaginationControls');
    var perPageSelect = document.getElementById('filePerPageSelect');
    if (!footer) return;

    if (_fileTotal === 0) {
        footer.classList.add('d-none');
        return;
    }
    footer.classList.remove('d-none');
    perPageSelect.value = String(_filePerPage);

    var start = (_filePage - 1) * _filePerPage + 1;
    var end = Math.min(_filePage * _filePerPage, _fileFilteredCount);
    var base = 'Showing ' + start + '\u2013' + end + ' of ' + _fileFilteredCount.toLocaleString();
    if (_fileListTruncated) {
        base += ' files in list (' + _fileProcessedTotal.toLocaleString()
             +  ' items processed \u2014 list truncated for performance)';
    }
    info.textContent = base;

    var pagesHtml = '';
    pagesHtml += '<li class="page-item ' + (_filePage <= 1 ? 'disabled' : '') + '">'
        + '<a class="page-link" href="#" onclick="goToFilePage(' + (_filePage - 1) + '); return false;" aria-label="Previous">&lsaquo;</a></li>';

    var maxVisible = 5;
    var rangeStart = Math.max(1, _filePage - Math.floor(maxVisible / 2));
    var rangeEnd = Math.min(_fileTotalPages, rangeStart + maxVisible - 1);
    if (rangeEnd - rangeStart + 1 < maxVisible) {
        rangeStart = Math.max(1, rangeEnd - maxVisible + 1);
    }

    if (rangeStart > 1) {
        pagesHtml += '<li class="page-item"><a class="page-link" href="#" onclick="goToFilePage(1); return false;">1</a></li>';
        if (rangeStart > 2) {
            pagesHtml += '<li class="page-item disabled"><span class="page-link">&hellip;</span></li>';
        }
    }
    for (var p = rangeStart; p <= rangeEnd; p++) {
        pagesHtml += '<li class="page-item ' + (p === _filePage ? 'active' : '') + '">'
            + '<a class="page-link" href="#" onclick="goToFilePage(' + p + '); return false;">' + p + '</a></li>';
    }
    if (rangeEnd < _fileTotalPages) {
        if (rangeEnd < _fileTotalPages - 1) {
            pagesHtml += '<li class="page-item disabled"><span class="page-link">&hellip;</span></li>';
        }
        pagesHtml += '<li class="page-item"><a class="page-link" href="#" onclick="goToFilePage(' + _fileTotalPages + '); return false;">' + _fileTotalPages + '</a></li>';
    }
    pagesHtml += '<li class="page-item ' + (_filePage >= _fileTotalPages ? 'disabled' : '') + '">'
        + '<a class="page-link" href="#" onclick="goToFilePage(' + (_filePage + 1) + '); return false;" aria-label="Next">&rsaquo;</a></li>';

    controls.innerHTML = pagesHtml;
}

function goToFilePage(page) {
    if (page < 1 || page > _fileTotalPages) return;
    _filePage = page;
    refreshFileResults();
}

function changeFilePerPage(value) {
    _filePerPage = parseInt(value, 10) || 100;
    _filePage = 1;
    localStorage.setItem('filePerPage', String(_filePerPage));
    refreshFileResults();
}

function onFileOutcomeFilterChanged(select) {
    _fileResultsActiveFilter = select.value || '';
    _filePage = 1;
    refreshFileResults();
}

function filterFileResults() {
    if (_fileSearchDebounce) clearTimeout(_fileSearchDebounce);
    _fileSearchDebounce = setTimeout(function() {
        _filePage = 1;
        refreshFileResults();
    }, 300);
}

function clearFileSearch() {
    document.getElementById('fileResultsSearch').value = '';
    _filePage = 1;
    refreshFileResults();
}
