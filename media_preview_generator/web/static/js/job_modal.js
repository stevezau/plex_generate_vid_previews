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
            // Wrap manages its own inner flex layout; we only toggle visibility.
            _attemptsWrap.classList.remove('d-none');
            _loadAttemptsDropdown(targetId);
        } else {
            _attemptsWrap.classList.add('d-none');
            document.getElementById('attemptsDropdown').innerHTML = '';
            const _hint = document.getElementById('attemptsHint');
            if (_hint) {
                _hint.className = 'badge attempts-state-chip d-none';
                _hint.textContent = '';
            }
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
    const isChainRow = !!(job && job.config && job.config.is_retry_chain);
    const autoScrollEl = document.getElementById('logsAutoScroll');
    autoScrollEl.checked = isRunning;

    const modal = new bootstrap.Modal(document.getElementById('logsModal'));
    modal.show();

    refreshLogs();

    if (logsRefreshInterval) clearInterval(logsRefreshInterval);
    // Chain rows poll regardless of their current status because a
    // PENDING chain can transition to RUNNING (mid-firing) and back to
    // PENDING (next backoff) repeatedly while the modal is open. The
    // poll also refreshes the Attempts dropdown so new firings appear
    // as options without forcing the user to close+reopen. Pre-fix
    // this gated only on ``isRunning`` captured at modal-open time,
    // meaning a modal opened during a PENDING window never saw later
    // attempts spawn.
    if (isRunning || isChainRow) {
        logsRefreshInterval = setInterval(function() {
            pollNewLogs();
            // D27 — always refresh files (not just when the Files tab is
            // active) so switching to the tab mid-run shows current
            // data instantly, not a 5s-stale snapshot.
            if (_fileResultsLoaded) refreshFileResults();
            // For chain rows: re-fetch the attempts list so new firings
            // appear in the dropdown without user intervention. Preserves
            // the currently-selected option (no auto-switch).
            if (isChainRow) _refreshAttemptsDropdown(targetId);
        }, 5000);
    }

    document.getElementById('logsModal').addEventListener('hidden.bs.modal', function() {
        if (logsRefreshInterval) {
            clearInterval(logsRefreshInterval);
            logsRefreshInterval = null;
        }
        if (_chainStateTickInterval) {
            clearInterval(_chainStateTickInterval);
            _chainStateTickInterval = null;
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
    'deleted':   '⊘',  // sentinel for an originating dispatch that no longer exists
};

function _formatAttemptDuration(secs) {
    if (secs === null || secs === undefined) return '';
    const s = Math.max(0, Math.round(secs));
    if (s < 60) return s + 's';
    const m = Math.floor(s / 60);
    const rem = s % 60;
    return rem ? (m + 'm' + rem + 's') : (m + 'm');
}

// Status → Bootstrap badge class (background + text colour for the pill).
const _ATTEMPT_STATUS_CLASS = {
    'completed': 'btn-outline-success',
    'failed':    'btn-outline-danger',
    'cancelled': 'btn-outline-secondary',
    'running':   'btn-outline-primary',
    'pending':   'btn-outline-warning',
    'deleted':   'btn-outline-secondary',
};

// Build one <button> pill for the Attempts row. Handles three shapes:
//   1. Regular per-attempt firing — label "Attempt N" with status colour.
//   2. Originating dispatch (a.is_originating === true) — label "Original"
//      with a distinct style (bordered + icon) so the user reads it as the
//      first run, not just another retry.
//   3. Deleted-original sentinel (a.id === null AND a.is_originating)
//      — disabled pill labelled "Original (deleted)" with greyed-out styling.
//
// Each pill carries its status colour via Bootstrap btn-outline-* classes —
// the user sees green/red/amber at a glance and can spot the failure point
// without opening every attempt. Tooltip provides the full label
// (status + duration + timestamp) on hover.
function _renderAttemptOption(a, max) {
    const glyph = _ATTEMPT_STATUS_GLYPH[a.status] || '?';
    const clsBase = _ATTEMPT_STATUS_CLASS[a.status] || 'btn-outline-secondary';
    const dur = _formatAttemptDuration(a.duration_sec);
    const durSuffix = dur ? ' · ' + dur : '';
    if (a.is_originating && !a.id) {
        // Sentinel — disabled pill, no value, can't be selected.
        return '<button type="button" class="btn btn-sm btn-outline-secondary disabled" disabled'
            + ' title="Original dispatch is no longer available (likely cleaned by retention policy)">'
            + '<i class="bi bi-slash-circle me-1"></i>Original (deleted)</button>';
    }
    let label;
    let tooltip;
    if (a.is_originating) {
        // The originating dispatch — distinct icon (play-fill = "first
        // run") + clear "Original" label so it reads differently from
        // the numbered retry pills next to it.
        label = '<i class="bi bi-play-fill me-1"></i>Original';
        tooltip = 'Original dispatch — ' + a.status + durSuffix;
    } else {
        // Compact attempt pill: "1", "2", "3" with status glyph.
        // Full "Attempt N of M — status · duration" lives in the tooltip
        // so hover reveals it without bloating the pill itself.
        label = glyph + ' ' + a.retry_attempt;
        tooltip = 'Attempt ' + a.retry_attempt + ' of ' + max + ' — ' + a.status + durSuffix;
    }
    return '<button type="button" class="btn btn-sm ' + clsBase + ' attempts-pill"'
        + ' data-attempt-id="' + escapeHtml(a.id) + '"'
        + ' data-is-originating="' + (a.is_originating ? '1' : '0') + '"'
        + ' onclick="onAttemptSelected(this)"'
        + ' title="' + escapeHtml(tooltip) + '"'
        + ' aria-label="' + escapeHtml(tooltip) + '">' + label + '</button>';
}

// Apply the "selected" visual treatment to one pill button — fill the
// background with its status colour so the active attempt is unmissable
// in a sea of identical outlined pills. The btn-outline-* class flips
// to its solid btn-* counterpart.
function _setActivePill(button) {
    const wrap = document.getElementById('attemptsDropdown');
    if (!wrap) return;
    // De-activate any previously-active pill.
    wrap.querySelectorAll('button.attempts-pill').forEach(b => {
        b.classList.remove('active');
        b.setAttribute('aria-current', 'false');
        // Flip solid back to outline (matched on token swap).
        b.className = b.className.replace(/\bbtn-(success|danger|secondary|primary|warning)\b/g,
            'btn-outline-$1');
    });
    if (button) {
        button.classList.add('active');
        button.setAttribute('aria-current', 'true');
        // Outline → solid for the active pill so it visually pops.
        button.className = button.className.replace(/\bbtn-outline-(success|danger|secondary|primary|warning)\b/g,
            'btn-$1');
    }
}

// Chain-state chip on the right of the pill row — shows countdown to
// next attempt (PENDING + retry_eta), running indicator, or terminal
// outcome. Driven by the chain Job's status + progress fields read out
// of the global ``jobs`` array. Ticked once a second while the chip
// is showing a countdown so users see Xm Ys decrement live.
let _chainStateTickInterval = null;

function _renderChainStateChip(chainId) {
    const chip = document.getElementById('attemptsHint');
    if (!chip) return;
    if (_chainStateTickInterval) { clearInterval(_chainStateTickInterval); _chainStateTickInterval = null; }
    const job = (typeof jobs !== 'undefined' && Array.isArray(jobs))
        ? jobs.find(j => j.id === chainId) : null;
    if (!job) { chip.className = 'badge attempts-state-chip d-none'; chip.textContent = ''; return; }
    const status = job.status || '';
    const attempt = (job.config && job.config.retry_attempt) || 0;
    const max = (job.config && job.config.retry_max_attempts) || 0;
    const progress = job.progress || {};
    const retryEta = progress.retry_eta || null;
    chip.classList.remove('d-none', 'bg-success', 'bg-danger', 'bg-warning', 'bg-info', 'bg-secondary', 'text-dark');
    chip.classList.add('attempts-state-chip');
    if (status === 'completed') {
        chip.classList.add('bg-success');
        chip.innerHTML = '<i class="bi bi-check2-circle me-1"></i>Chain completed';
    } else if (status === 'failed') {
        chip.classList.add('bg-danger');
        chip.innerHTML = '<i class="bi bi-exclamation-circle me-1"></i>Chain failed';
    } else if (status === 'cancelled') {
        chip.classList.add('bg-secondary');
        chip.innerHTML = '<i class="bi bi-slash-circle me-1"></i>Cancelled';
    } else if (status === 'running') {
        chip.classList.add('bg-info', 'text-dark');
        const label = attempt && max
            ? `Attempt ${attempt}/${max} running`
            : 'Attempt running';
        chip.innerHTML = `<i class="bi bi-lightning-charge-fill me-1"></i>${label}`;
    } else if (status === 'pending' && retryEta) {
        chip.classList.add('bg-warning', 'text-dark');
        const tick = () => {
            const remaining = Math.max(0, Math.ceil((new Date(retryEta).getTime() - Date.now()) / 1000));
            const label = _formatRetryRemaining(remaining);
            const ofMax = (attempt && max) ? ` (attempt ${attempt + 1}/${max})` : '';
            chip.innerHTML = `<i class="bi bi-hourglass-split me-1"></i>Next attempt in ${label}${ofMax}`;
            if (remaining === 0) {
                clearInterval(_chainStateTickInterval);
                _chainStateTickInterval = null;
            }
        };
        tick();
        _chainStateTickInterval = setInterval(tick, 1000);
    } else {
        // PENDING without retry_eta — chain spawned, first attempt not
        // yet scheduled. Rare transient state.
        chip.classList.add('bg-secondary');
        chip.innerHTML = '<i class="bi bi-hourglass me-1"></i>Pending';
    }
}

function _formatRetryRemaining(seconds) {
    if (seconds <= 0) return '0s';
    if (seconds < 60) return seconds + 's';
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    if (m < 60) return s ? `${m}m ${s}s` : `${m}m`;
    const h = Math.floor(m / 60);
    const rm = m % 60;
    return rm ? `${h}h ${rm}m` : `${h}h`;
}

async function _loadAttemptsDropdown(chainId) {
    const wrap = document.getElementById('attemptsDropdown');
    if (!wrap) return;
    wrap.innerHTML = '<small class="text-muted">Loading attempts…</small>';
    try {
        const data = await apiGet('/api/jobs/' + encodeURIComponent(chainId) + '/attempts');
        const attempts = data.attempts || [];
        if (attempts.length === 0) {
            wrap.innerHTML = '<small class="text-muted">No attempts yet — showing chain status</small>';
            _logsModalAttemptId = null;
            _renderChainStateChip(chainId);
            refreshLogs();
            return;
        }
        const max = data.max_attempts || attempts[attempts.length - 1].retry_attempt || 0;
        let html = '';
        for (let i = 0; i < attempts.length; i++) {
            html += _renderAttemptOption(attempts[i], max);
        }
        wrap.innerHTML = html;
        // Default-select the LATEST selectable pill (skip the deleted
        // sentinel which has no id). Attempts are sorted ascending so
        // the last is newest.
        let defaultIdx = -1;
        for (let i = attempts.length - 1; i >= 0; i--) {
            if (attempts[i].id) { defaultIdx = i; break; }
        }
        if (defaultIdx >= 0) {
            const target = wrap.querySelector(`button[data-attempt-id="${CSS.escape(attempts[defaultIdx].id)}"]`);
            if (target) {
                _setActivePill(target);
                _logsModalAttemptId = attempts[defaultIdx].id;
            }
        } else {
            _logsModalAttemptId = null;
        }
        _renderChainStateChip(chainId);
        refreshLogs();
    } catch (error) {
        console.error('Failed to load attempts:', error);
        wrap.innerHTML = '<small class="text-danger">Could not load attempts — see console.</small>';
        const chip = document.getElementById('attemptsHint');
        if (chip) { chip.className = 'badge attempts-state-chip d-none'; chip.textContent = ''; }
        _logsModalAttemptId = null;
    }
}

async function _refreshAttemptsDropdown(chainId) {
    // Poll-driven refresh: re-fetch /attempts, rebuild pill row, restore
    // the currently-selected pill so the user's selection survives the
    // refresh. New attempts appear as additional pills without losing
    // the user's place.
    const wrap = document.getElementById('attemptsDropdown');
    if (!wrap) return;
    try {
        const data = await apiGet('/api/jobs/' + encodeURIComponent(chainId) + '/attempts');
        const attempts = data.attempts || [];
        if (attempts.length === 0) return;
        const max = data.max_attempts || attempts[attempts.length - 1].retry_attempt || 0;
        const previouslySelected = _logsModalAttemptId;
        let html = '';
        for (let i = 0; i < attempts.length; i++) {
            html += _renderAttemptOption(attempts[i], max);
        }
        wrap.innerHTML = html;
        // Restore previous selection, otherwise pick newest selectable.
        let targetId = null;
        if (previouslySelected) {
            const found = attempts.find(a => a.id === previouslySelected);
            if (found) targetId = found.id;
        }
        if (!targetId) {
            for (let i = attempts.length - 1; i >= 0; i--) {
                if (attempts[i].id) { targetId = attempts[i].id; break; }
            }
        }
        if (targetId) {
            const target = wrap.querySelector(`button[data-attempt-id="${CSS.escape(targetId)}"]`);
            if (target) _setActivePill(target);
            _logsModalAttemptId = targetId;
        }
        // Re-render the chain-state chip — the chain Job's status /
        // retry_eta might have changed since modal-open (new firing,
        // completion, exhaustion) and the poll caught it.
        _renderChainStateChip(chainId);
    } catch (error) {
        // Silent — user has last-good UI; next tick will retry.
        console.debug('Attempts poll-refresh failed:', error);
    }
}


function onAttemptSelected(button) {
    const attemptId = button?.dataset?.attemptId;
    if (!attemptId) return;
    if (attemptId === _logsModalAttemptId) return;  // already selected — no-op
    _logsModalAttemptId = attemptId;
    _setActivePill(button);
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
    // Refresh the Files tab too: per-file results are written per
    // dispatch Job (each retry firing has its own JSONL), so the user
    // sees DIFFERENT files for attempt 1 vs attempt 5 (the latter
    // recorded the final publish, the former a pending_registration).
    _filePage = 1;
    _fileResultsLoaded = false;
    document.getElementById('fileResultsBody').innerHTML =
        '<tr><td colspan="4" class="text-muted text-center">Loading…</td></tr>';
    var filesTabBtn = document.getElementById('filesTab');
    if (filesTabBtn && filesTabBtn.classList.contains('active')) {
        refreshFileResults();
    }
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
    // Scope to the selected attempt when the modal target is a chain
    // row — the per-file results JSONL is written per dispatch Job, so
    // each retry firing has its own. The dropdown sets
    // ``_logsModalAttemptId`` which ``_logsTargetId`` returns. For
    // non-chain jobs this collapses to ``_logsModalJobId`` as before.
    var targetId = _logsTargetId();
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
