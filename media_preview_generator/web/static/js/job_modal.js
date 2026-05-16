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

// Map a webhook ``source`` value to a Bootstrap-icon glyph. Falls back
// to a generic globe so unknown sources still render with consistent
// visual weight. Centralised so future sources (e.g. ``schedule``,
// ``manual``) only need one place to register.
const _SOURCE_ICON = {
    plex:     'play-circle',
    radarr:   'film',
    sonarr:   'tv',
    schedule: 'calendar-event',
    manual:   'person',
    webhook:  'plug',
};
function _sourceIcon(source) {
    return _SOURCE_ICON[(source || '').toLowerCase()] || 'globe2';
}

// Compute the wall-clock duration of a job — ``started_at`` (or
// ``created_at`` when start is missing) to ``completed_at`` (or now
// when status is ``running``/``pending``). Returns a short string
// ("6s", "1m 8s", "2h 14m") or ``''`` when no timestamps are
// available.
//
// Terminal-status guard: jobs that ended ``failed``/``cancelled`` can
// have ``completed_at`` missing (worker crash, scheduler kill). Without
// the guard, the duration chip would tick monotonically every time the
// operator re-opened the modal, reading "2h 14m" for a job that
// actually failed 6s in. For terminal jobs missing the end timestamp
// we return ``''`` instead of pretending the job is still running.
function _jobDurationLabel(job) {
    if (!job) return '';
    const startStr = job.started_at || job.created_at;
    if (!startStr) return '';
    const start = new Date(startStr).getTime();
    if (!Number.isFinite(start)) return '';
    const endStr = job.completed_at;
    let end;
    if (endStr) {
        end = new Date(endStr).getTime();
        if (!Number.isFinite(end)) return '';
    } else if (job.status === 'running' || job.status === 'pending') {
        end = Date.now();
    } else {
        // Terminal status with no completion timestamp — duration is
        // unknown; don't fabricate a runaway clock from Date.now().
        return '';
    }
    const secs = Math.max(0, Math.round((end - start) / 1000));
    return _formatAttemptDuration(secs);
}

// Operator-grade modal header. Three rows:
//   Row 1 — entity title (``library_name`` / ``retry_basename``) + status badge.
//   Row 2 — up to four conditional chips: source (when ``cfg.source``),
//           server (when ``job.server_name``), duration (when timestamps
//           are available), runs (when ``isChain``).
//   Row 3 — Job ID line lives separately in ``#logsJobId`` for copy/paste
//           and future deep-linking; not part of this helper.
// Falls back to the generic "Job Details" string when ``job`` is missing
// (``undefined`` or ``null``). Real-world trigger: the modal was re-opened
// after the job was cleaned from history mid-poll, so
// ``jobs.find(j => j.id === targetId)`` returned ``undefined``. The
// ``if (!job)`` guard handles both shapes.
function _renderModalHeader(job) {
    const headerEl = document.getElementById('logsModalHeader');
    if (!headerEl) return;
    if (!job) {
        headerEl.innerHTML = '<h5 class="modal-title mb-0"><i class="bi bi-file-text me-2"></i>Job Details</h5>';
        return;
    }
    const cfg = job.config || {};
    const isChain = !!cfg.is_retry_chain;
    const title = cfg.retry_basename || job.library_name || '(Unknown job)';
    const meta = (typeof _statusMeta === 'function')
        ? _statusMeta(job.status)
        : { label: job.status || '?', cls: 'bg-secondary', tip: '' };
    const statusBadge = `<span class="badge ${meta.cls} ms-2 align-self-center"`
        + (meta.tip ? ` title="${escapeHtmlAttr(meta.tip)}"` : '')
        + `>${escapeHtmlText(meta.label)}</span>`;

    const chips = [];
    if (cfg.source) {
        chips.push('<span class="badge bg-light text-dark border">'
            + '<i class="bi bi-' + _sourceIcon(cfg.source) + ' me-1"></i>'
            + escapeHtmlText(cfg.source) + '</span>');
    }
    if (job.server_name) {
        chips.push('<span class="badge bg-light text-dark border">'
            + escapeHtmlText(job.server_name) + '</span>');
    }
    const dur = _jobDurationLabel(job);
    if (dur) {
        chips.push('<span class="badge bg-light text-dark border">'
            + '<i class="bi bi-clock me-1"></i>' + escapeHtmlText(dur) + '</span>');
    }
    if (isChain) {
        // ``retry_attempt`` counts retries — 0 means "originating dispatch
        // only", 1 means "original + 1 retry", etc. Total runs is N+1.
        const ra = cfg.retry_attempt || 0;
        const rmax = cfg.retry_max_attempts || 5;
        const totalRuns = ra + 1;
        const runsLabel = ra
            ? totalRuns + ' run' + (totalRuns === 1 ? '' : 's')
                + ' · 1 original + ' + ra + ' retr' + (ra === 1 ? 'y' : 'ies')
            : '1 run · original only';
        chips.push('<span class="badge bg-light text-dark border">'
            + '<i class="bi bi-arrow-clockwise me-1"></i>' + escapeHtmlText(runsLabel)
            + ' <span class="text-muted">/ ' + (rmax + 1) + ' max</span></span>');
    }

    // Job-ID block on the right of the title row — small monospace,
    // copy-on-click. Keeps the ID accessible (deep-link sharing,
    // operator copy/paste, ``aria-describedby`` reference) without
    // dedicating a whole sub-row to muted text. The id stays
    // ``logsJobId`` so the modal's ``aria-describedby`` keeps working.
    const jid = escapeHtmlText(job.id || '');
    const jidBlock = '<div class="ms-auto d-flex align-items-center gap-1 text-muted small font-monospace" id="logsJobId">'
        + '<span title="Job ID">' + jid + '</span>'
        + '<button type="button" class="btn btn-link btn-sm p-0 ms-1" title="Copy Job ID" aria-label="Copy Job ID"'
        + ' onclick="onCopyJobId(\'' + escapeHtmlAttr(job.id || '') + '\', this)">'
        + '<i class="bi bi-clipboard"></i>'
        + '</button>'
        + '</div>';

    headerEl.innerHTML =
        '<div class="d-flex align-items-baseline flex-wrap gap-2">'
        +   '<h5 class="modal-title mb-0">'
        +     '<i class="bi bi-file-text me-2"></i>' + escapeHtmlText(title)
        +   '</h5>'
        +   statusBadge
        +   jidBlock
        + '</div>'
        + (chips.length
            ? '<div class="d-flex flex-wrap gap-2 small mt-1">' + chips.join('') + '</div>'
            : '');
}

// Attempt-scope subtitle above the Logs viewer — orients the reader
// when they're looking at a per-retry log instead of the originating
// dispatch's. Hidden for non-chain jobs and for the originating
// attempt (the chain head's UUID itself). Reads the currently-active
// pill via its `[data-attempt-id]` so we don't need to re-fetch the
// attempts API for a label.
function _renderLogsSubtitle() {
    const el = document.getElementById('logsSubtitle');
    if (!el) return;
    const job = jobs.find(j => j.id === _logsModalJobId);
    const isChain = !!(job && job.config && job.config.is_retry_chain);
    if (!isChain || !_logsModalAttemptId || _logsModalAttemptId === _logsModalJobId) {
        el.classList.add('d-none');
        el.innerHTML = '';
        return;
    }
    const pill = document.querySelector('button.attempts-pill[data-attempt-id="' + CSS.escape(_logsModalAttemptId) + '"]');
    if (!pill) {
        el.classList.add('d-none');
        el.innerHTML = '';
        return;
    }
    // The pill's title attribute already carries the canonical label
    // ("Run N of M (retry #N-1) · status · duration"). Reuse it so
    // the subtitle stays in lockstep with the pill copy.
    const label = pill.getAttribute('title') || pill.getAttribute('aria-label') || 'Selected attempt';
    el.classList.remove('d-none');
    el.innerHTML = '<i class="bi bi-funnel me-1"></i>Showing logs for '
        + '<strong>' + escapeHtmlText(label) + '</strong>';
}

// Copy the Job ID to the clipboard and flash the button to confirm.
// Reuses the ``navigator.clipboard.writeText`` pattern already used by
// ``copyLogs()`` (~line 1500). Falls back silently when the clipboard
// API is unavailable (HTTP context, very old browsers).
function onCopyJobId(jobId, btn) {
    if (!jobId) return;
    try {
        navigator.clipboard.writeText(jobId);
    } catch (e) { /* silently no-op */ }
    if (btn) {
        const icon = btn.querySelector('i');
        if (icon) {
            const orig = icon.className;
            icon.className = 'bi bi-clipboard-check text-success';
            setTimeout(() => { icon.className = orig; }, 1200);
        }
    }
}
window.onCopyJobId = onCopyJobId;

// Find the first BIF path on disk across all servers' publisher rows.
// Used by the "Open BIF" footer action so the operator gets one-click
// access to scrub the generated preview right from the modal. Returns
// '' when no BIF was generated (or only-skipped jobs where output_path
// isn't on the publisher row).
function _firstBifPathFromJob(job) {
    if (!job || !Array.isArray(job.publishers)) return '';
    for (let i = 0; i < job.publishers.length; i++) {
        const p = job.publishers[i];
        // Per-publisher aggregate doesn't carry output paths — those
        // live in the per-file results. ``last_bif_path`` is a forward-
        // compat field; today we fall back to scanning the file results
        // if it's absent (caller may choose to do its own lookup).
        if (p && p.last_bif_path) return p.last_bif_path;
    }
    return '';
}

// Toggle the footer's operator-action buttons based on chain state.
// Hide-vs-disable: a disabled button invites repeated clicks before the
// operator reads the tooltip; hiding clearly signals "not available
// right now". The full set of buttons is rendered hidden in the
// template — we just flip ``d-none``.
//
// State -> visible buttons:
//   * Chain pending (back-off countdown active) -> Retry now + Cancel chain + Open BIF
//   * Chain running                              -> Cancel chain + Open BIF
//   * Chain completed/failed/cancelled (terminal)-> Open BIF (only if any BIF on disk)
//   * Non-chain (single dispatch)                -> Open BIF (only if any BIF on disk)
function _updateOperatorActions(job) {
    const retryBtn = document.getElementById('opActionRetryNow');
    const cancelBtn = document.getElementById('opActionCancelChain');
    const openBifBtn = document.getElementById('opActionOpenBif');
    if (!retryBtn || !cancelBtn || !openBifBtn) return;

    // Start hidden — each branch below opts in.
    retryBtn.classList.add('d-none');
    cancelBtn.classList.add('d-none');
    openBifBtn.classList.add('d-none');
    if (!job) return;

    const cfg = job.config || {};
    const isChain = !!cfg.is_retry_chain;
    const status = job.status || '';
    const progress = job.progress || {};
    const hasPendingRetry = isChain && status === 'pending' && progress.retry_eta;
    const isActiveChain = isChain && (status === 'pending' || status === 'running');

    if (hasPendingRetry) retryBtn.classList.remove('d-none');
    if (isActiveChain) cancelBtn.classList.remove('d-none');

    const bif = _firstBifPathFromJob(job);
    if (bif) {
        openBifBtn.href = '/bif-viewer?bif=' + encodeURIComponent(bif);
        openBifBtn.classList.remove('d-none');
    } else {
        openBifBtn.removeAttribute('href');
    }
}

// "Retry now" footer button — POST /api/jobs/<id>/retry-now and refresh
// the modal state on success. The endpoint already invokes the retry
// callback on a fresh thread, so the call returns ~immediately and the
// modal's poll loop picks up the new attempt row within 5s.
async function onOperatorRetryNow() {
    if (!_logsModalJobId) return;
    const btn = document.getElementById('opActionRetryNow');
    if (btn) { btn.disabled = true; btn.classList.add('disabled'); }
    try {
        await apiPost('/api/jobs/' + encodeURIComponent(_logsModalJobId) + '/retry-now', {});
        // Force-refresh the attempts dropdown so the new firing row
        // appears without waiting the full 5s poll interval.
        _refreshAttemptsDropdown(_logsModalJobId);
        // Update operator-action visibility — once fired, the chain is
        // briefly running and ``retry_eta`` clears, so "Retry now"
        // should hide. Re-read the job from the global list.
        const refreshed = jobs.find(j => j.id === _logsModalJobId);
        _updateOperatorActions(refreshed);
    } catch (err) {
        console.error('retry-now failed:', err);
        const msg = (err && err.message) ? err.message : 'Retry-now request failed';
        alert(msg);
    } finally {
        if (btn) { btn.disabled = false; btn.classList.remove('disabled'); }
    }
}

// "Cancel chain" footer button — POST /api/jobs/<id>/cancel. The
// existing endpoint already handles chain semantics (cancel timer +
// mark child attempts cancelled). Browser ``confirm()`` prevents a
// stray click from killing a chain that's making progress.
// Overview tab + timeline + retry-reason banner + recent-log preview +
// log-level filter + SSE streaming + keyboard shortcuts were stripped in
// the simplification pass. They duplicated content already visible above
// the tabs (header / Servers strip / Attempts pills / chain-state chip)
// and added three concurrent state-update channels (poll + SSE + ticks)
// for a homelab tool. The header + above-tabs strip + Logs / Files tabs
// are sufficient. ``RetryScheduler.fire_now`` and its endpoint stay
// because they power the still-useful "Retry now" footer button.


async function onOperatorCancelChain() {
    if (!_logsModalJobId) return;
    if (!confirm('Cancel this retry chain? Any pending back-off timer will be dropped and in-flight attempts will receive a cancellation signal.')) {
        return;
    }
    const btn = document.getElementById('opActionCancelChain');
    if (btn) { btn.disabled = true; btn.classList.add('disabled'); }
    try {
        await apiPost('/api/jobs/' + encodeURIComponent(_logsModalJobId) + '/cancel', {});
        // The global jobs poll picks up the new status within ~1s;
        // refresh the attempts dropdown immediately so the modal
        // reflects the cancellation without waiting.
        _refreshAttemptsDropdown(_logsModalJobId);
        const refreshed = jobs.find(j => j.id === _logsModalJobId);
        _updateOperatorActions(refreshed);
    } catch (err) {
        console.error('cancel-chain failed:', err);
        alert((err && err.message) ? err.message : 'Cancel request failed');
    } finally {
        if (btn) { btn.disabled = false; btn.classList.remove('disabled'); }
    }
}

// Push the current modal state onto the browser history so the URL
// reflects what's visible. Enables (a) browser back/forward navigation
// inside the modal, (b) shareable URLs that deep-link a teammate
// straight to a specific attempt/tab.
function _pushModalState(jobId, attemptId, tab) {
    if (!jobId) return;
    const params = new URLSearchParams();
    params.set('job', jobId);
    if (attemptId) params.set('attempt', attemptId);
    // Elide ``tab=logs`` because Logs is the SSR-default landing tab —
    // shortest URL pleads the most-common case.
    if (tab && tab !== 'logs') params.set('tab', tab);
    const url = window.location.pathname + '?' + params.toString();
    try {
        history.pushState({ modal: 'jobDetails', jobId, attemptId, tab }, '', url);
    } catch (e) { /* old browsers / file:// — silently no-op */ }
}

function _replaceModalState(jobId, attemptId, tab) {
    if (!jobId) return;
    const params = new URLSearchParams();
    params.set('job', jobId);
    if (attemptId) params.set('attempt', attemptId);
    // Elide ``tab=logs`` because Logs is the SSR-default landing tab —
    // shortest URL pleads the most-common case.
    if (tab && tab !== 'logs') params.set('tab', tab);
    const url = window.location.pathname + '?' + params.toString();
    try {
        history.replaceState({ modal: 'jobDetails', jobId, attemptId, tab }, '', url);
    } catch (e) { /* silently no-op */ }
}

function _popModalState() {
    // On modal close, strip our query params so the back/forward
    // stack doesn't accumulate stale states.
    if (window.location.search.includes('job=')) {
        try {
            history.pushState({}, '', window.location.pathname);
        } catch (e) { /* silently no-op */ }
    }
}

// On initial page load: if the URL has ``?job=<id>`` params, auto-open
// the modal. Run after the jobs list has populated so jobs.find()
// resolves correctly.
function _autoOpenModalFromUrl() {
    const params = new URLSearchParams(window.location.search);
    const jobId = params.get('job');
    if (!jobId) return;
    const attemptId = params.get('attempt');
    const tab = params.get('tab');
    // Stash for showLogsModal to pick up via _initialDeepLinkAttempt/Tab.
    _initialDeepLinkAttempt = attemptId;
    _initialDeepLinkTab = tab;
    showLogsModal(jobId);
}
let _initialDeepLinkAttempt = null;
let _initialDeepLinkTab = null;

function showLogsModal(jobId) {
    const targetId = jobId || _lastNotifiedJobId;
    if (!targetId) return;
    _logsModalJobId = targetId;
    _logsModalAttemptId = null;
    // Deep-link override: a ?attempt= param takes precedence over the
    // default "newest attempt" selection. Consumed once per modal open.
    const _deepLinkAttempt = _initialDeepLinkAttempt;
    const _deepLinkTab = _initialDeepLinkTab;
    _initialDeepLinkAttempt = null;
    _initialDeepLinkTab = null;

    // ``#logsJobId`` is rendered inside ``_renderModalHeader`` now (no
    // standalone row). The aria-describedby on the modal still points
    // at the id; the JS-rendered element carries it.
    document.getElementById('logsSearchInput').value = '';

    // Phase H8: render the per-publisher header for this job.
    const _job = jobs.find(j => j.id === targetId);
    // Operator header — title (library / retry basename), status badge,
    // chips for source / server / duration / run count. Replaces the
    // generic "Job Details" string with the entity name so an operator
    // can identify the job at a glance.
    _renderModalHeader(_job);
    const _hdr = document.getElementById('logsModalPublishers');
    if (_hdr) _hdr.innerHTML = _job ? _renderPublishersBlock(_job) : '';
    // Clear any leftover attempt-scope subtitle from a previous modal
    // open. _loadAttemptsDropdown re-renders it for chains.
    _renderLogsSubtitle();
    // Operator-action footer buttons (Retry now / Cancel chain /
    // Open BIF) — visibility derived from current chain state. Re-run
    // on every showLogsModal so a stale "Retry now" from a previously
    // pending chain doesn't bleed into the next modal open.
    _updateOperatorActions(_job);

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
            // Stash any deep-link attempt-id so the loader picks it over
            // the default newest-selectable.
            if (_deepLinkAttempt) _pendingAttemptSelection = _deepLinkAttempt;
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

    // Tab selection — deep-link param wins. Otherwise we land on the
    // SSR default (Logs). Tier 2 had localStorage tab-restore via
    // _restoreLastTab; that's gone with the Overview tab.
    if (_deepLinkTab) {
        const btnId = _deepLinkTab === 'files' ? 'filesTab' : 'logsTab';
        const btn = document.getElementById(btnId);
        if (btn) new bootstrap.Tab(btn).show();
    }
    // Push the deep-link URL state so reload / back navigation works.
    _pushModalState(targetId, _deepLinkAttempt, _deepLinkTab);

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
            // Operator-action visibility — re-derive from the latest
            // job snapshot in the global ``jobs`` list. Without this,
            // a chain that transitioned ``pending`` → ``completed``
            // mid-modal would still show "Retry now" / "Cancel chain"
            // until the user closed and reopened.
            const _polled = jobs.find(j => j.id === targetId);
            _updateOperatorActions(_polled);
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
        _popModalState();
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
//   1. Originating dispatch (a.is_originating === true) — label is the
//      run ordinal ``1`` prefixed with a ``bi-play-fill`` icon so it shares
//      the same ordinal scheme as the retries below it. Tooltip spells out
//      "Run 1 (original dispatch)".
//   2. Retry firing — label is the run ordinal N prefixed with a
//      ``bi-arrow-clockwise`` icon (run 2 = first retry, run 3 = second
//      retry, etc.). Tooltip: "Run N (retry #N-1)".
//   3. Deleted-original sentinel (a.id === null AND a.is_originating)
//      — disabled pill labelled "Run 1 (deleted)" with greyed-out styling.
//
// The previous nomenclature ("Original" vs "Attempt 1") forced operators
// to mentally translate — "Attempt 1" actually meant *first retry*, not
// the first run. Run-based ordinals eliminate that mismatch: every pill
// carries the same N-of-M counter and the operator reads the chain as a
// sequence.
//
// Each pill carries its status colour via Bootstrap btn-outline-* classes —
// the user sees green/red/amber at a glance and can spot the failure point
// without opening every attempt. Tooltip provides the full label
// (run ordinal + status + duration) on hover.
// Bootstrap icon for a server type — mirrors the ``_MEDIA_SERVER_TYPE_ICONS``
// map in app.js but is duplicated here to avoid load-order coupling
// between the two files. Unknown types render with a generic database
// icon so the pill still gets *some* per-server visual cue.
//
// Security note: values in this map are class-name literals
// concatenated directly into the pill HTML (see
// ``_renderPendingServerChips`` below). Do NOT extend this map by
// reading user-controlled strings — keep it a hard-coded whitelist
// and only the dict-lookup result reaches the DOM. Other interpolations
// in the chip (title, aria-label) already pass through ``escapeHtml``,
// so they're safe.
const _PENDING_SERVER_ICON = {
    plex:     'bi-play-btn',
    emby:     'bi-emoji-laughing',
    jellyfin: 'bi-cup-hot',
};
function _pendingServerIcon(type) {
    return _PENDING_SERVER_ICON[(type || '').toLowerCase()] || 'bi-hdd-network';
}

// Build the per-pill chip suffix surfacing which servers were still
// pending on a given attempt. Renders one tiny vendor icon per pending
// server, inline after the run ordinal:
//
//   ↻ 2 [J]      ← Jellyfin was the holdout
//   ↻ 3 [J] [E]  ← Jellyfin AND Emby were still pending
//
// Returns an empty string when ``pending_servers`` is empty or missing
// (the originating-dispatch pill always gets an empty list from the
// /attempts endpoint, so this collapses to nothing for run 1).
function _renderPendingServerChips(pendingServers) {
    if (!Array.isArray(pendingServers) || pendingServers.length === 0) return '';
    const chips = pendingServers.map(s => {
        const icon = _pendingServerIcon(s.server_type);
        const name = s.server_name || s.server_type || '?';
        const count = s.count || 0;
        const title = escapeHtml(name + ' pending × ' + count);
        return '<span class="ms-1 attempts-pending-chip" title="' + title + '" aria-label="' + title + '">'
            + '<i class="bi ' + icon + '"></i></span>';
    });
    return chips.join('');
}

// Build the tooltip's pending-server suffix, e.g.:
//   "· pending: JellyTest × 4"
//   "· pending: JellyTest × 4, EmbyTest × 1"
function _pendingServerTooltipSuffix(pendingServers) {
    if (!Array.isArray(pendingServers) || pendingServers.length === 0) return '';
    const parts = pendingServers.map(s => {
        const name = s.server_name || s.server_type || '?';
        return name + ' × ' + (s.count || 0);
    });
    return ' · pending: ' + parts.join(', ');
}

function _renderAttemptOption(a, max) {
    const clsBase = _ATTEMPT_STATUS_CLASS[a.status] || 'btn-outline-secondary';
    const dur = _formatAttemptDuration(a.duration_sec);
    const durSuffix = dur ? ' · ' + dur : '';
    // Run ordinal: original = run 1, retry #1 = run 2, ... For the
    // originating dispatch ``retry_attempt`` is 0; numbered retries
    // are 1-based already.
    const runOrdinal = a.is_originating ? 1 : (a.retry_attempt + 1);
    const maxRuns = max + 1;
    if (a.is_originating && !a.id) {
        // Sentinel — disabled pill, no value, can't be selected.
        return '<button type="button" class="btn btn-sm btn-outline-secondary disabled" disabled'
            + ' title="Run 1 (original dispatch) is no longer available — likely cleaned by retention policy">'
            + '<i class="bi bi-slash-circle me-1"></i>Run 1 (deleted)</button>';
    }
    const pendingChips = _renderPendingServerChips(a.pending_servers);
    const pendingTip = _pendingServerTooltipSuffix(a.pending_servers);
    let label;
    let tooltip;
    if (a.is_originating) {
        label = '<i class="bi bi-play-fill me-1"></i>1';
        tooltip = 'Run 1 (original dispatch) · ' + a.status + durSuffix + pendingTip;
    } else {
        // ``bi-arrow-clockwise`` glyph tells the operator this is a retry
        // firing, not the original. The ordinal is the run number
        // (1-based across the chain), so a retry pill rendered as
        // ``↻ 2`` reads as "run 2, which is retry #1".
        label = '<i class="bi bi-arrow-clockwise me-1"></i>' + runOrdinal;
        tooltip = 'Run ' + runOrdinal + ' of ' + maxRuns
            + ' (retry #' + a.retry_attempt + ') · ' + a.status + durSuffix + pendingTip;
    }
    return '<button type="button" class="btn btn-sm ' + clsBase + ' attempts-pill"'
        + ' data-attempt-id="' + escapeHtml(a.id) + '"'
        + ' data-is-originating="' + (a.is_originating ? '1' : '0') + '"'
        + ' onclick="onAttemptSelected(this)"'
        + ' title="' + escapeHtml(tooltip) + '"'
        + ' aria-label="' + escapeHtml(tooltip) + '">' + label + pendingChips + '</button>';
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
    // Trailing info-icon (same template as the dashboard's Retry chip)
    // so users can drill into the explanation without leaving the modal.
    // The delegated click handler in app.js routes ``.info-icon`` clicks
    // to the global info modal regardless of where they appear.
    // Plex vs Jellyfin variant picked via ``_pickRetryInfoTpl`` so the
    // copy matches the chain's actual server type.
    const _tplId = (typeof _pickRetryInfoTpl === 'function')
        ? _pickRetryInfoTpl(job)
        : 'infoRetryChainJellyfinTpl';
    const _infoIcon = ' <button type="button" class="info-icon info-icon-more btn btn-link p-0 ms-1 align-baseline"'
        + ' data-explain-template="' + _tplId + '"'
        + ' data-explain-title="Why this file is auto-retrying"'
        + ' title="What is this? — click for details"'
        + ' aria-label="About retry chain"'
        + ' style="color: inherit;">'
        + '<i class="bi bi-info-circle"></i></button>';
    if (status === 'completed') {
        chip.classList.add('bg-success');
        chip.innerHTML = '<i class="bi bi-check2-circle me-1"></i>Chain completed' + _infoIcon;
    } else if (status === 'failed') {
        chip.classList.add('bg-danger');
        chip.innerHTML = '<i class="bi bi-exclamation-circle me-1"></i>Chain failed' + _infoIcon;
    } else if (status === 'cancelled') {
        chip.classList.add('bg-secondary');
        chip.innerHTML = '<i class="bi bi-slash-circle me-1"></i>Cancelled' + _infoIcon;
    } else if (status === 'running') {
        chip.classList.add('bg-info', 'text-dark');
        const label = attempt && max
            ? `Attempt ${attempt}/${max} running`
            : 'Attempt running';
        chip.innerHTML = `<i class="bi bi-lightning-charge-fill me-1"></i>${label}${_infoIcon}`;
    } else if (status === 'pending' && retryEta) {
        chip.classList.add('bg-warning', 'text-dark');
        const tick = () => {
            const remaining = Math.max(0, Math.ceil((new Date(retryEta).getTime() - Date.now()) / 1000));
            const label = _formatRetryRemaining(remaining);
            const ofMax = (attempt && max) ? ` (attempt ${attempt + 1}/${max})` : '';
            chip.innerHTML = `<i class="bi bi-hourglass-split me-1"></i>Next attempt in ${label}${ofMax}${_infoIcon}`;
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
        chip.innerHTML = '<i class="bi bi-hourglass me-1"></i>Pending' + _infoIcon;
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

// Deep-link override consumed once by ``_loadAttemptsDropdown``.
// Set by ``showLogsModal`` when the URL carries an ``?attempt=`` param;
// cleared after the loader picks it up so subsequent reloads default
// back to the newest-selectable.
let _pendingAttemptSelection = null;

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
        // the last is newest. A pending deep-link selection from a
        // ``?attempt=`` URL param takes precedence over this default.
        let defaultIdx = -1;
        if (_pendingAttemptSelection) {
            const found = attempts.findIndex(a => a.id === _pendingAttemptSelection);
            if (found >= 0) defaultIdx = found;
            _pendingAttemptSelection = null;
        }
        if (defaultIdx < 0) {
            for (let i = attempts.length - 1; i >= 0; i--) {
                if (attempts[i].id) { defaultIdx = i; break; }
            }
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
        _renderLogsSubtitle();
        _renderRetryReasonSubtitle(attempts);
        refreshLogs();
    } catch (error) {
        console.error('Failed to load attempts:', error);
        wrap.innerHTML = '<small class="text-danger">Could not load attempts — see console.</small>';
        const chip = document.getElementById('attemptsHint');
        if (chip) { chip.className = 'badge attempts-state-chip d-none'; chip.textContent = ''; }
        _logsModalAttemptId = null;
    }
}

// Render the "Retried Nx because…" subtitle beneath the Attempts pill
// row. Preferred source: the persisted ``retry_reason`` on each retry
// child (captured at spawn time in ``job_runner._spawn_retry_job``).
// Fallback: derive from the live ``pending_servers`` snapshot for
// chains predating the persistence change.
//
// Persisted source matters because once a chain SUCCEEDS,
// ``merge_chain_publishers_best_per_path`` refreshes every snapshot to
// the best status per path — so the live ``pending_servers`` becomes
// empty even though the chain DID retry (job ``4ad23f43``, 2026-05-15:
// the modal banner was blank because the retry resolved everything and
// the snapshot showed only ``published``/``skipped_output_exists``).
//
// Output examples:
//   "Retried 3× because JellyTest was still indexing"
//   "Retried 2× because Plex + JellyTest were still indexing"
//   "Retried 1× because path(s) couldn't be resolved by any server"
//   "Retried 1× because file(s) were missing on disk"
//   "Retried 2× because JellyTest was still indexing, and file(s) were missing on disk"
//
// Hidden when: no retries fired (only originating dispatch), or no
// reason data is available from either source.
function _renderRetryReasonSubtitle(attempts) {
    const el = document.getElementById('retryReasonSubtitle');
    if (!el) return;
    const hide = () => { el.className = 'd-none small text-muted mt-2'; el.textContent = ''; };
    if (!Array.isArray(attempts) || attempts.length === 0) return hide();

    // Originating-dispatch entry never carries a retry_reason (it
    // wasn't a retry — it TRIGGERED the first retry). Walk children only.
    const children = attempts.filter(a => !a.is_originating);
    if (children.length === 0) return hide();

    // Aggregate across children. PER-CHILD fallback: when a child has
    // a persisted retry_reason use it, otherwise fall back to that
    // child's live pending_servers snapshot. A chain-wide
    // ``anyPersisted`` short-circuit would silently drop legacy
    // children's data on a chain that spans the upgrade (older retries
    // pre-patch with no retry_reason; newer retries post-patch with
    // retry_reason set) — the legacy children would be invisible to
    // the banner aggregator.
    const pendingCounts = new Map();  // server_name -> attempts-blocked count
    let unresolvedAttempts = 0;
    let staleAttempts = 0;
    for (const a of children) {
        const r = a.retry_reason;
        if (r && typeof r === 'object') {
            if ((r.unresolved | 0) > 0) unresolvedAttempts += 1;
            if ((r.stale_paths | 0) > 0) staleAttempts += 1;
            const pbs = r.pending_by_server || {};
            for (const name of Object.keys(pbs)) {
                if (!name) continue;
                pendingCounts.set(name, (pendingCounts.get(name) || 0) + 1);
            }
        } else {
            // Legacy / in-flight: no persisted reason — derive blockers
            // from the live publisher snapshot. Set semantics within
            // an attempt: a server with 4 pending files counts as 1
            // "blocked on this attempt", not 4.
            const pending = Array.isArray(a.pending_servers) ? a.pending_servers : [];
            const seen = new Set();
            for (const s of pending) {
                const name = s.server_name || s.server_type || '';
                if (!name || seen.has(name)) continue;
                seen.add(name);
                pendingCounts.set(name, (pendingCounts.get(name) || 0) + 1);
            }
        }
    }

    const phrases = [];
    if (pendingCounts.size > 0) {
        // Pick the server(s) tied for most appearances. Single dominant
        // → "because <name>"; ties → list with " + ".
        const ranked = [...pendingCounts.entries()].sort((a, b) => b[1] - a[1]);
        const topCount = ranked[0][1];
        const blockers = ranked.filter(([, n]) => n === topCount).map(([name]) => name);
        const namesText = blockers.length === 1
            ? blockers[0]
            : blockers.slice(0, -1).join(', ') + ' + ' + blockers[blockers.length - 1];
        const verb = blockers.length === 1 ? 'was' : 'were';
        phrases.push(namesText + ' ' + verb + ' still indexing');
    }
    if (unresolvedAttempts > 0) {
        phrases.push("path(s) couldn't be resolved by any server");
    }
    if (staleAttempts > 0) {
        phrases.push("file(s) were missing on disk");
    }

    if (phrases.length === 0) return hide();

    const reasonText = phrases.length === 1
        ? phrases[0]
        : phrases.slice(0, -1).join(', ') + ', and ' + phrases[phrases.length - 1];
    el.className = 'small text-muted mt-2';
    // Plain text — no HTML — escapeHtml not required. Server names
    // come straight from the API which sources them from settings (no
    // user-controlled HTML enters this path).
    el.textContent = 'Retried ' + children.length + '× because ' + reasonText;
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
            _renderLogsSubtitle();
        }
        // Re-render the chain-state chip — the chain Job's status /
        // retry_eta might have changed since modal-open (new firing,
        // completion, exhaustion) and the poll caught it.
        _renderChainStateChip(chainId);
        // Same poll tick: refresh the chain-summary subtitle so a new
        // attempt that just landed updates "Retried N×…" without
        // forcing the user to close+reopen the modal.
        _renderRetryReasonSubtitle(attempts);
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
    _renderLogsSubtitle();
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
    // Update the URL's ``attempt=`` param so a copy-link points at the
    // newly-selected attempt. Tab detection: Logs is the only fallback
    // (no Overview tab post-simplification).
    const filesActive = document.getElementById('filesTabPane') &&
        document.getElementById('filesTabPane').classList.contains('active');
    _replaceModalState(_logsModalJobId, _logsModalAttemptId, filesActive ? 'files' : 'logs');
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

function onLogsTabActivated() {
    // Mirror onFilesTabActivated so the URL reflects whichever tab is
    // visible. Without this, the deep-link contract is asymmetric:
    // clicking Files updates the URL; clicking Logs leaves the URL
    // pointing at the previous tab. Sharing the URL would then re-open
    // the modal on the wrong pane.
    _replaceModalState(_logsModalJobId, _logsModalAttemptId, 'logs');
}

function onFilesTabActivated() {
    if (!_fileResultsLoaded) {
        refreshFileResults();
    }
    _replaceModalState(_logsModalJobId, _logsModalAttemptId, 'files');
}

async function refreshFileResults() {
    // Files panel ALWAYS queries the chain head's JSONL — never an
    // individual retry attempt's. Post-2026-05-13 retry children write
    // their per-file outcomes to the PARENT's JSONL (via the
    // _file_result_cb redirect in job_runner.py), so the chain head
    // accumulates the full audit trail. An individual retry child's
    // JSONL is empty.
    //
    // Logs ARE still per-attempt (each retry Job has its own log
    // file); that's why _logsTargetId still uses the selected pill
    // for the Logs tab. Files diverges because the data model
    // diverges: logs are per-run, files are per-lifecycle.
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
        var workerRaw = f.worker || '';
        var workerBadge = _compactWorkerBadge(workerRaw);
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
            + '<td>' + workerBadge + '</td>'
            + '</tr>';
    }
    tbody.innerHTML = html;
}

// Compact two-character worker badge for the Files table — "G0" / "C3".
// The full "GPU Worker 1 (NVIDIA TITAN RTX)" string was eating ~10% of
// the table's width and crowded the filename column on Sonarr/Radarr
// release-group basenames. Hover-tooltip preserves the full label so the
// info isn't lost; click target stays the table row.
function _compactWorkerBadge(worker) {
    if (!worker) return '';
    var match = String(worker).match(/^(GPU|CPU)\s+Worker\s+(\d+)/i);
    if (!match) {
        // Unknown format — fall back to the original muted-text rendering
        // so future worker classes don't silently vanish from the column.
        return '<small class="text-muted">' + escapeHtml(worker) + '</small>';
    }
    var prefix = match[1].toUpperCase() === 'GPU' ? 'G' : 'C';
    var idx = match[2];
    return '<span class="badge bg-light text-dark border font-monospace"'
        + ' title="' + escapeHtmlAttr(worker) + '" style="font-size: 0.7rem;">'
        + prefix + idx + '</span>';
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
