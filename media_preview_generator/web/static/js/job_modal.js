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

    headerEl.innerHTML =
        '<div class="d-flex align-items-baseline flex-wrap gap-2">'
        +   '<h5 class="modal-title mb-0">'
        +     '<i class="bi bi-file-text me-2"></i>' + escapeHtmlText(title)
        +   '</h5>'
        +   statusBadge
        + '</div>'
        + (chips.length
            ? '<div class="d-flex flex-wrap gap-2 small mt-1">' + chips.join('') + '</div>'
            : '');
}

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
// ----------------------------------------------------------------------
// Overview tab — Tier 2 of the modal rebuild.
// ----------------------------------------------------------------------
//
// The Overview pane is the default landing for the modal so a multi-run
// chain reads as a coherent dashboard (summary + publishers + timeline)
// instead of dropping the operator into the raw log stream. Logs and
// Files remain accessible as drill-downs.
//
// Composition (top to bottom):
//   * Summary card    — large status icon + headline metrics (duration,
//                       runs, frames if generated)
//   * Publisher matrix— per-server rows with vendor logo, status pills,
//                       last-action timestamp
//   * Attempts timeline — horizontal lane per run, status-coloured bar
//                       from started_at to completed_at, click to switch
//   * Recent log tail — last 10 lines of the current attempt's log with
//                       "Open full logs" jump button
//
// Renderer is invoked when the Overview tab is activated AND on the
// 5 s poll loop for active chains.

const _OVERVIEW_LAST_TAB_KEY = 'jobModalActiveTab';
let _overviewTickInterval = null;

function onOverviewTabActivated() {
    try { localStorage.setItem(_OVERVIEW_LAST_TAB_KEY, 'overview'); } catch (e) { /* private mode */ }
    _renderOverview();
    _replaceModalState(_logsModalJobId, _logsModalAttemptId, 'overview');
}

function onLogsTabActivated() {
    try { localStorage.setItem(_OVERVIEW_LAST_TAB_KEY, 'logs'); } catch (e) { /* private mode */ }
    _replaceModalState(_logsModalJobId, _logsModalAttemptId, 'logs');
}

// Log level filter (Tier 3.14) — drives a data-attr on #logsContent
// that the CSS in style.css uses to hide lines below the chosen
// severity. Persisted in localStorage so an operator's "I only care
// about WARN+" preference survives modal reopen.
const _LOG_LEVEL_KEY = 'jobModalLogLevel';
function onLogLevelChange(value) {
    const el = document.getElementById('logsContent');
    if (!el) return;
    if (value === 'all') {
        el.removeAttribute('data-log-level');
    } else {
        el.setAttribute('data-log-level', value);
    }
    try { localStorage.setItem(_LOG_LEVEL_KEY, value); } catch (e) { /* private mode */ }
}
function _restoreLogLevel() {
    let saved = 'all';
    try { saved = localStorage.getItem(_LOG_LEVEL_KEY) || 'all'; } catch (e) { /* private mode */ }
    const radioId = saved === 'info' ? 'logLevelInfo'
        : saved === 'warn' ? 'logLevelWarn'
        : saved === 'error' ? 'logLevelErr'
        : 'logLevelAll';
    const radio = document.getElementById(radioId);
    if (radio) {
        radio.checked = true;
        onLogLevelChange(saved);
    }
}

function _restoreLastTab() {
    let last = null;
    try { last = localStorage.getItem(_OVERVIEW_LAST_TAB_KEY); } catch (e) { /* private mode */ }
    const tabId = last === 'logs' ? 'logsTab' : (last === 'files' ? 'filesTab' : 'overviewTab');
    const btn = document.getElementById(tabId);
    if (btn) {
        const tab = new bootstrap.Tab(btn);
        tab.show();
    }
}

// Render the full Overview pane for the modal's current job.
// Safe to call repeatedly — innerHTML replacement only.
function _renderOverview() {
    const root = document.getElementById('overviewContent');
    if (!root) return;
    const job = jobs.find(j => j.id === _logsModalJobId);
    if (!job) {
        root.innerHTML = '<div class="alert alert-warning small mb-0">Job not found in current snapshot — refresh the dashboard.</div>';
        return;
    }
    root.innerHTML =
        _renderOverviewSummaryCard(job)
        + _renderOverviewReasonBanner(job)
        + _renderOverviewPublishersCard(job)
        + _renderOverviewTimelineCard(job)
        + _renderOverviewLogPreviewCard(job);
    // Wire up timeline-bar clicks to attempt switching (delegated)
    _wireOverviewTimelineClicks();
    // Start the per-second tick for live duration / countdown updates
    if (_overviewTickInterval) clearInterval(_overviewTickInterval);
    if (job.status === 'running' || job.status === 'pending') {
        _overviewTickInterval = setInterval(_tickOverviewDurations, 1000);
    }
}

function _tickOverviewDurations() {
    // Update any data-elapsed-since spans inside the Overview pane.
    // Cheaper than re-rendering the whole tree every second.
    document.querySelectorAll('#overviewContent [data-elapsed-since]').forEach(el => {
        const startStr = el.getAttribute('data-elapsed-since');
        if (typeof formatElapsed === 'function' && startStr) {
            el.textContent = formatElapsed(startStr);
        }
    });
}

function _renderOverviewSummaryCard(job) {
    const cfg = job.config || {};
    const meta = (typeof _statusMeta === 'function')
        ? _statusMeta(job.status)
        : { label: job.status || '?', cls: 'bg-secondary', tip: '' };
    const isChain = !!cfg.is_retry_chain;
    const totalRuns = isChain ? (cfg.retry_attempt || 0) + 1 : 1;
    const maxRuns = isChain ? ((cfg.retry_max_attempts || 5) + 1) : 1;
    const outcome = (job.progress && job.progress.outcome) || {};
    const generated = outcome.generated || 0;
    const skipped = (outcome.skipped_bif_exists || 0) + (outcome.skipped_output_exists || 0);
    const failed = outcome.failed || 0;

    const headline = '<div class="d-flex align-items-center gap-3 mb-2">'
        + '<span class="badge ' + meta.cls + ' fs-6 px-3 py-2">'
        +   '<i class="bi bi-circle-fill me-1" style="font-size: 0.6rem; vertical-align: middle;"></i>'
        +   escapeHtmlText(meta.label)
        + '</span>'
        + '<div class="flex-grow-1">'
        +   '<div class="h6 mb-0">' + escapeHtmlText(cfg.retry_basename || job.library_name || '(Unknown job)') + '</div>'
        +   (meta.tip ? '<small class="text-muted">' + escapeHtmlText(meta.tip) + '</small>' : '')
        + '</div>'
        + '</div>';

    const metrics = [];
    const dur = _jobDurationLabel(job);
    if (dur) {
        const live = (job.status === 'running' || job.status === 'pending') && job.started_at;
        metrics.push(_metricChip('clock', 'Duration',
            live
                ? '<span data-elapsed-since="' + escapeHtmlAttr(job.started_at) + '">' + escapeHtmlText(dur) + '</span>'
                : escapeHtmlText(dur)));
    }
    if (isChain) {
        metrics.push(_metricChip('arrow-clockwise', 'Runs', totalRuns + ' / ' + maxRuns + ' max'));
    }
    if (generated > 0) metrics.push(_metricChip('check-circle', 'Generated', String(generated)));
    if (skipped > 0)   metrics.push(_metricChip('skip-forward', 'Skipped (already on disk)', String(skipped)));
    if (failed > 0)    metrics.push(_metricChip('x-circle', 'Failed', String(failed)));
    if (cfg.source)    metrics.push(_metricChip(_sourceIcon(cfg.source), 'Source', escapeHtmlText(cfg.source)));

    return '<div class="card mb-3 job-modal-section">'
        + '<div class="card-body p-3">'
        +   headline
        +   (metrics.length
                ? '<div class="d-flex flex-wrap gap-2 small">' + metrics.join('') + '</div>'
                : '')
        + '</div>'
        + '</div>';
}

function _metricChip(icon, label, value) {
    return '<span class="badge bg-light text-dark border d-inline-flex align-items-center gap-1">'
        + '<i class="bi bi-' + icon + '"></i>'
        + '<span class="text-muted">' + escapeHtmlText(label) + ':</span>'
        + ' ' + value
        + '</span>';
}

// Inline retry-reason banner — surfaces *why* the chain is retrying
// without forcing the operator to scroll the log. Derived from the
// chain head's publishers + status + retry_eta (no extra API call).
// Renders nothing for non-chain jobs or chains in terminal state.
function _renderOverviewReasonBanner(job) {
    const cfg = job.config || {};
    const isChain = !!cfg.is_retry_chain;
    if (!isChain) return '';
    const status = job.status || '';
    if (status !== 'pending' && status !== 'running') return '';
    const publishers = Array.isArray(job.publishers) ? job.publishers : [];
    // Identify which server(s) are causing the chain to retry. The
    // most common shape: one or more servers with a
    // ``published_pending_registration`` count > 0 (Jellyfin / Plex
    // mid-scan), or ``skipped_not_indexed`` / ``skipped_not_in_library``
    // (Plex hasn't analysed the file yet).
    const stuck = [];
    for (const p of publishers) {
        const counts = (p && p.counts) || {};
        const reasons = [];
        if (counts.published_pending_registration > 0) reasons.push('trickplay registration pending');
        if (counts.skipped_not_indexed > 0) reasons.push('source not yet scanned');
        if (counts.skipped_not_in_library > 0) reasons.push('source not in library');
        if (reasons.length) {
            stuck.push({
                name: p.server_name || (p.server_type || '').toUpperCase() || 'Server',
                type: (p.server_type || '').toLowerCase(),
                reasons,
            });
        }
    }
    if (!stuck.length) return '';
    const items = stuck.map(s =>
        '<li><strong>' + escapeHtmlText(s.name) + '</strong>: ' + escapeHtmlText(s.reasons.join(' / ')) + '</li>'
    ).join('');
    const retryEta = (job.progress && job.progress.retry_eta) || null;
    const ra = cfg.retry_attempt || 0;
    const rmax = cfg.retry_max_attempts || 5;
    // ``retry_attempt = ra`` is the 1-indexed identity of the
    // CURRENTLY-QUEUED retry firing (per ``_upsert_retry_chain_job``
    // in processing/retry_queue.py — schedule() sets retry_attempt to
    // the upcoming attempt's number, not the count of completed
    // retries). Run ordinal = ra + 1 because Run 1 is the original
    // (retry_attempt=0); retry #1 is Run 2. Max runs = rmax + 1 for
    // the same reason (original is not a retry).
    //
    // Earlier draft used ``ra + 2`` which is off-by-one (caught by
    // architecture review HIGH finding) — would have shipped "Run 3
    // is queued" while attempt #1 was scheduled.
    const headline = retryEta
        ? 'Run ' + (ra + 1) + ' of ' + (rmax + 1) + ' is queued — once the upstream catches up, this chain will close.'
        : 'Waiting for the upstream server to catch up.';
    return '<div class="alert alert-warning small d-flex mb-3 job-modal-section" role="status">'
        + '<i class="bi bi-hourglass-split me-2" style="font-size: 1.1rem;"></i>'
        + '<div class="flex-grow-1">'
        +   '<strong>Why this chain is still going:</strong> '
        +   escapeHtmlText(headline)
        +   '<ul class="mb-0 mt-1" style="padding-left: 1.25rem;">' + items + '</ul>'
        + '</div>'
        + '</div>';
}

function _renderOverviewPublishersCard(job) {
    // Reuse the existing per-publisher block renderer — keeps the
    // Overview consistent with the header strip a user sees in the
    // logs tab. The block already labels itself "Chain totals" for
    // chains, so we hide its own header here and supply a card header
    // for visual hierarchy.
    const body = (typeof _renderPublishersBlock === 'function')
        ? _renderPublishersBlock(job)
        : '';
    if (!body) return '';
    return '<div class="card mb-3 job-modal-section">'
        + '<div class="card-header py-2 bg-body-tertiary">'
        +   '<i class="bi bi-broadcast me-1"></i>'
        +   '<strong>Server results</strong>'
        + '</div>'
        + '<div class="card-body p-3">' + body + '</div>'
        + '</div>';
}

function _renderOverviewTimelineCard(job) {
    const cfg = job.config || {};
    const isChain = !!cfg.is_retry_chain;
    // The timeline is most valuable for chains; for single-run jobs it
    // would be one bar, which is just visual noise.
    if (!isChain) return '';
    return '<div class="card mb-3 job-modal-section">'
        + '<div class="card-header py-2 bg-body-tertiary d-flex align-items-center justify-content-between">'
        +   '<div><i class="bi bi-bar-chart-steps me-1"></i><strong>Attempts timeline</strong></div>'
        +   '<small class="text-muted">Click a bar to switch attempts</small>'
        + '</div>'
        + '<div class="card-body p-3">'
        +   '<div id="attemptsTimeline" class="attempts-timeline" role="navigation" aria-label="Retry attempts timeline">'
        +     '<div class="text-muted small">Loading timeline&hellip;</div>'
        +   '</div>'
        + '</div>'
        + '</div>';
}

function _renderOverviewLogPreviewCard(job) {
    const last = _rawLogs.slice(-10);
    if (!last.length) {
        // Don't render an empty card; cleaner than a "no logs" message.
        return '';
    }
    const lines = last.map(line => colorizeLogLine(line)).join('\n');
    return '<div class="card mb-3 job-modal-section">'
        + '<div class="card-header py-2 bg-body-tertiary d-flex align-items-center justify-content-between">'
        +   '<div><i class="bi bi-terminal me-1"></i><strong>Recent log</strong> <small class="text-muted">(last ' + last.length + ' lines)</small></div>'
        +   '<button type="button" class="btn btn-sm btn-link p-0" onclick="onSwitchToLogsTab()">'
        +     'Open full logs <i class="bi bi-chevron-right"></i></button>'
        + '</div>'
        + '<div class="card-body p-2">'
        +   '<pre class="log-viewer mb-0" style="max-height: 200px; overflow-y: auto; font-size: 0.8rem;">' + lines + '</pre>'
        + '</div>'
        + '</div>';
}

function onSwitchToLogsTab() {
    const btn = document.getElementById('logsTab');
    if (btn) {
        const tab = new bootstrap.Tab(btn);
        tab.show();
    }
}

// Populate the timeline once attempts data has been fetched. Called by
// the existing attempts-dropdown load path so we don't duplicate the
// API call. The timeline shares the active-attempt selection with the
// pill row.
function _renderAttemptsTimeline(attempts, max_attempts) {
    const root = document.getElementById('attemptsTimeline');
    if (!root) return;
    if (!attempts || !attempts.length) {
        root.innerHTML = '<div class="text-muted small">No attempts yet.</div>';
        return;
    }
    // Compute the time window: chain start to chain end (or now if active)
    const starts = attempts.map(a => _toMs(a.created_at)).filter(t => t > 0);
    const ends = attempts.map(a => _toMs(a.completed_at) || Date.now()).filter(t => t > 0);
    const tMin = Math.min.apply(null, starts);
    const tMax = Math.max.apply(null, ends);
    const span = Math.max(1, tMax - tMin);
    // Render lanes — one row per attempt
    let html = '<div class="attempts-timeline-axis small text-muted mb-1 d-flex justify-content-between">'
        + '<span>' + escapeHtmlText(_formatTimelineTimestamp(tMin)) + '</span>'
        + '<span>' + escapeHtmlText(_formatTimelineTimestamp(tMax)) + ' (' + escapeHtmlText(_formatAttemptDuration(Math.round(span / 1000))) + ')</span>'
        + '</div>';
    for (let i = 0; i < attempts.length; i++) {
        const a = attempts[i];
        const start = _toMs(a.created_at) || tMin;
        const end = _toMs(a.completed_at) || Date.now();
        const leftPct = ((start - tMin) / span) * 100;
        const widthPct = Math.max(0.5, ((end - start) / span) * 100);
        const statusCls = _ATTEMPT_STATUS_CLASS[a.status] || 'btn-outline-secondary';
        const fillCls = statusCls.replace('btn-outline-', 'attempts-timeline-bar-');
        const isActive = (_logsModalAttemptId === a.id) || (_logsModalAttemptId === null && a.is_originating);
        const dur = _formatAttemptDuration(a.duration_sec);
        const runOrdinal = a.is_originating ? 1 : (a.retry_attempt + 1);
        const label = (a.is_originating ? 'Run 1 (original)' : 'Run ' + runOrdinal + ' (retry #' + a.retry_attempt + ')')
            + ' · ' + a.status + (dur ? ' · ' + dur : '');
        const safeId = escapeHtmlAttr(a.id || '');
        const deleted = a.is_originating && !a.id;
        html += '<div class="attempts-timeline-lane d-flex align-items-center gap-2 mb-1">'
            + '<span class="attempts-timeline-label text-muted small" style="width: 70px; flex-shrink: 0;">'
            +   'Run ' + runOrdinal
            + '</span>'
            + '<div class="attempts-timeline-track flex-grow-1 position-relative">'
            +   (deleted
                ? '<div class="attempts-timeline-bar attempts-timeline-bar-secondary" style="width: 100%; opacity: 0.4;" title="Run 1 (deleted by retention)">deleted</div>'
                : '<button type="button" class="attempts-timeline-bar ' + fillCls + (isActive ? ' active' : '') + '"'
                    + ' style="left: ' + leftPct.toFixed(2) + '%; width: ' + widthPct.toFixed(2) + '%;"'
                    + ' data-attempt-id="' + safeId + '"'
                    + ' title="' + escapeHtmlAttr(label) + '"'
                    + ' aria-label="' + escapeHtmlAttr(label) + '">'
                    + (a.is_originating ? '<i class="bi bi-play-fill"></i>' : '<i class="bi bi-arrow-clockwise"></i>')
                + '</button>')
            + '</div>'
            + '<span class="attempts-timeline-duration text-muted small font-monospace" style="width: 60px; text-align: right; flex-shrink: 0;">'
            +   escapeHtmlText(dur || '—')
            + '</span>'
            + '</div>';
    }
    root.innerHTML = html;
}

function _toMs(isoStr) {
    if (!isoStr) return 0;
    const t = new Date(isoStr).getTime();
    return Number.isFinite(t) ? t : 0;
}

function _formatTimelineTimestamp(ms) {
    if (!ms) return '';
    const d = new Date(ms);
    // Hours:minutes:seconds, local time — enough granularity for retry
    // chains (which complete in minutes to hours).
    const pad = (n) => String(n).padStart(2, '0');
    return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
}

function _wireOverviewTimelineClicks() {
    const root = document.getElementById('attemptsTimeline');
    if (!root) return;
    root.addEventListener('click', function (ev) {
        const bar = ev.target.closest('button.attempts-timeline-bar');
        if (!bar || bar.disabled) return;
        const attemptId = bar.getAttribute('data-attempt-id');
        if (!attemptId) return;
        // Reuse the pill-row's selection handler — keep both UIs in sync.
        const pill = document.querySelector('button.attempts-pill[data-attempt-id="' + CSS.escape(attemptId) + '"]');
        if (pill) {
            pill.click();
        } else {
            // Pill row not rendered (e.g. Overview shown before attempts
            // loaded). Set the state directly.
            _logsModalAttemptId = attemptId;
            refreshLogs();
        }
        _renderOverview();
    });
}

// ----------------------------------------------------------------------
// Keyboard shortcuts (Tier 3.12)
// ----------------------------------------------------------------------
// Power-user navigation. Bindings:
//   [ / ]   previous / next attempt (chain only)
//   1 / 2 / 3   switch to Overview / Logs / Files tab
//   /       focus the active tab's search input
//   j / k   scroll the logs viewport line-by-line (vim-style)
//   g / G   top / bottom of the logs viewport
//   c       copy the current log buffer to clipboard
//   r       refresh logs
//   ?       open the shortcuts cheatsheet popover
//
// Handler registered on modal-shown, deregistered on modal-hidden so
// other contexts (settings page, dashboard tables) aren't affected.

let _jobModalKeyHandler = null;

function _enableJobModalKeyboard() {
    if (_jobModalKeyHandler) return;  // idempotent
    _jobModalKeyHandler = _onJobModalKeydown;
    document.addEventListener('keydown', _jobModalKeyHandler);
}

function _disableJobModalKeyboard() {
    if (!_jobModalKeyHandler) return;
    document.removeEventListener('keydown', _jobModalKeyHandler);
    _jobModalKeyHandler = null;
}

function _onJobModalKeydown(ev) {
    // Don't intercept when the user is typing in an input. The search
    // boxes + URL fields would otherwise lose every character.
    const tag = (ev.target && ev.target.tagName) || '';
    const isInput = tag === 'INPUT' || tag === 'TEXTAREA' || (ev.target && ev.target.isContentEditable);
    // Modifier-aware: ignore when Ctrl / Meta / Alt are pressed so we
    // don't fight browser shortcuts (Cmd-C copy, etc.). Shift is OK —
    // we use it for `G` (Shift-g).
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;

    // Allow `/` even when an input is focused so the operator can
    // re-focus a search box from anywhere in the modal. Everything
    // else suppresses on input focus.
    if (isInput && ev.key !== 'Escape') return;

    switch (ev.key) {
        case '[':
            _shortcutSwitchAttempt(-1);
            ev.preventDefault();
            break;
        case ']':
            _shortcutSwitchAttempt(+1);
            ev.preventDefault();
            break;
        case '1':
            _shortcutSwitchTab('overviewTab');
            ev.preventDefault();
            break;
        case '2':
            _shortcutSwitchTab('logsTab');
            ev.preventDefault();
            break;
        case '3':
            _shortcutSwitchTab('filesTab');
            ev.preventDefault();
            break;
        case '/':
            _shortcutFocusSearch();
            ev.preventDefault();
            break;
        case 'j':
            _shortcutScrollLogs(20);
            ev.preventDefault();
            break;
        case 'k':
            _shortcutScrollLogs(-20);
            ev.preventDefault();
            break;
        case 'g':
            _shortcutScrollLogs('top');
            ev.preventDefault();
            break;
        case 'G':
            _shortcutScrollLogs('bottom');
            ev.preventDefault();
            break;
        case 'c':
            if (typeof copyLogs === 'function') copyLogs();
            ev.preventDefault();
            break;
        case 'r':
            if (typeof refreshLogs === 'function') refreshLogs();
            ev.preventDefault();
            break;
        case '?':
            _shortcutShowCheatsheet();
            ev.preventDefault();
            break;
        default:
            // No binding — let the event through.
            return;
    }
}

function _shortcutSwitchAttempt(direction) {
    const wrap = document.getElementById('attemptsDropdown');
    if (!wrap) return;
    const pills = Array.from(wrap.querySelectorAll('button.attempts-pill'));
    if (!pills.length) return;
    const activeIdx = pills.findIndex(p => p.classList.contains('active'));
    const nextIdx = Math.max(0, Math.min(pills.length - 1,
        (activeIdx >= 0 ? activeIdx : 0) + direction));
    if (nextIdx === activeIdx || nextIdx < 0) return;
    pills[nextIdx].click();
}

function _shortcutSwitchTab(tabId) {
    const btn = document.getElementById(tabId);
    if (!btn) return;
    const tab = new bootstrap.Tab(btn);
    tab.show();
}

function _shortcutFocusSearch() {
    // Focus the search input on the currently active tab.
    const filesActive = document.getElementById('filesTabPane') &&
        document.getElementById('filesTabPane').classList.contains('active');
    const targetId = filesActive ? 'fileResultsSearch' : 'logsSearchInput';
    const el = document.getElementById(targetId);
    if (el) el.focus();
}

function _shortcutScrollLogs(arg) {
    const el = document.getElementById('logsContent');
    if (!el) return;
    if (arg === 'top') {
        el.scrollTo({ top: 0, behavior: 'smooth' });
    } else if (arg === 'bottom') {
        el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
    } else {
        // arg is a pixel delta (positive = down, negative = up).
        el.scrollBy({ top: arg, behavior: 'auto' });
    }
}

function _shortcutShowCheatsheet() {
    // Render the cheatsheet as a Bootstrap modal-within-modal. Built
    // inline rather than as a Jinja partial so the shortcut table
    // stays next to the handler that implements it — a future binding
    // addition surfaces the documentation right next to the code.
    let modal = document.getElementById('jobModalShortcutsModal');
    if (!modal) {
        const html =
            '<div class="modal fade" id="jobModalShortcutsModal" tabindex="-1">'
          +   '<div class="modal-dialog modal-dialog-centered">'
          +     '<div class="modal-content">'
          +       '<div class="modal-header py-2">'
          +         '<h6 class="modal-title"><i class="bi bi-keyboard me-2"></i>Keyboard shortcuts</h6>'
          +         '<button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>'
          +       '</div>'
          +       '<div class="modal-body">'
          +         '<table class="table table-sm mb-0">'
          +           '<tbody>'
          +             _kbRow('[', ']', 'Switch attempt (prev / next)')
          +             _kbRow('1', '2 / 3', 'Switch tab (Overview / Logs / Files)')
          +             _kbRow('/', '', 'Focus search on active tab')
          +             _kbRow('j', 'k', 'Scroll logs down / up')
          +             _kbRow('g', 'Shift-G', 'Top / bottom of logs')
          +             _kbRow('c', '', 'Copy current log buffer to clipboard')
          +             _kbRow('r', '', 'Refresh logs')
          +             _kbRow('?', '', 'Show this cheatsheet')
          +           '</tbody>'
          +         '</table>'
          +       '</div>'
          +     '</div>'
          +   '</div>'
          + '</div>';
        document.body.insertAdjacentHTML('beforeend', html);
        modal = document.getElementById('jobModalShortcutsModal');
    }
    new bootstrap.Modal(modal).show();
}

function _kbRow(key1, key2, label) {
    const kbd = (k) => k
        ? '<kbd class="border bg-body-tertiary text-body px-2 py-1 rounded">' + escapeHtmlText(k) + '</kbd>'
        : '';
    const keys = [kbd(key1), kbd(key2)].filter(Boolean).join(' <span class="text-muted">/</span> ');
    return '<tr><td class="text-nowrap">' + keys + '</td><td>' + escapeHtmlText(label) + '</td></tr>';
}

// ----------------------------------------------------------------------
// SSE log streaming (Tier 3.11)
// ----------------------------------------------------------------------
// Replaces the 5 s polling cadence with sub-second push for the
// currently-targeted attempt's log file. Polling stays armed as the
// fallback channel — if SSE errors out (proxy timeout, server
// thread starvation), we silently degrade.

let _logStreamEventSource = null;
let _logStreamFallbackActive = false;

function _startLogStream(jobId) {
    _stopLogStream();
    if (typeof EventSource === 'undefined') {
        // Old browsers (or environments without SSE) — polling is the
        // only path. Leave the polling interval armed; do nothing.
        _logStreamFallbackActive = true;
        _updateStreamingChip('polling');
        return;
    }
    const targetId = _logsTargetId() || jobId;
    if (!targetId) return;
    const url = '/api/jobs/' + encodeURIComponent(targetId)
        + '/logs/stream?offset=' + (_logsKnownCount || 0);
    try {
        _logStreamEventSource = new EventSource(url);
    } catch (e) {
        console.debug('EventSource construction failed; falling back to polling.', e);
        _logStreamFallbackActive = true;
        _updateStreamingChip('polling');
        return;
    }
    _logStreamEventSource.addEventListener('line', (ev) => {
        // Push the new line through the same renderer the polling
        // path uses so colourisation / level classes / search filter
        // / auto-scroll all behave identically.
        const line = ev.data || '';
        _rawLogs.push(line);
        _logsKnownCount = _rawLogs.length;
        _logsTotalLines = Math.max(_logsTotalLines, _logsKnownCount);
        const container = document.getElementById('logsContent');
        if (container) {
            if (container.textContent === 'No logs available') container.innerHTML = '';
            container.insertAdjacentHTML('beforeend', colorizeLogLine(line) + '\n');
            const auto = document.getElementById('logsAutoScroll');
            if (auto && auto.checked) container.scrollTop = container.scrollHeight;
        }
    });
    _logStreamEventSource.addEventListener('hb', () => {
        // Heartbeat — no payload, just confirms the connection is alive.
        _updateStreamingChip('live');
    });
    _logStreamEventSource.addEventListener('reconnect', () => {
        // Server-initiated graceful close after 60 s connection cap.
        // Re-open immediately so the client doesn't pay EventSource's
        // ~3 s default reconnect backoff.
        _restartLogStream();
    });
    _logStreamEventSource.onerror = () => {
        // Persistent failure (network blip, auth lost, server gone).
        // Drop to polling-only mode so the modal still gets updates,
        // and surface the change in the footer chip.
        console.debug('SSE log stream errored; falling back to 5 s polling.');
        _stopLogStream();
        _logStreamFallbackActive = true;
        _updateStreamingChip('polling');
    };
    _updateStreamingChip('live');
}

function _stopLogStream() {
    if (_logStreamEventSource) {
        try { _logStreamEventSource.close(); } catch (e) { /* already closed */ }
        _logStreamEventSource = null;
    }
    _logStreamFallbackActive = false;
}

function _restartLogStream() {
    if (!_logsModalJobId) return;
    _startLogStream(_logsTargetId() || _logsModalJobId);
}

function _updateStreamingChip(mode) {
    // Renders a small status chip in the modal footer ('live' green dot
    // while SSE is connected; amber 'polling' on fallback). Created
    // lazily so the chip doesn't appear on non-streaming jobs.
    const footer = document.querySelector('#logsModal .modal-footer');
    if (!footer) return;
    let chip = document.getElementById('logStreamModeChip');
    if (!chip) {
        chip = document.createElement('span');
        chip.id = 'logStreamModeChip';
        chip.className = 'badge me-2';
        // Insert before the first action button so it sits at the
        // left of the footer's right cluster.
        const firstBtn = footer.querySelector('.btn');
        if (firstBtn) footer.insertBefore(chip, firstBtn);
        else footer.appendChild(chip);
    }
    if (mode === 'live') {
        chip.className = 'badge bg-success me-2';
        chip.innerHTML = '<i class="bi bi-circle-fill me-1" style="font-size: 0.5rem; vertical-align: middle;"></i>live';
        chip.title = 'SSE stream connected — log lines push sub-second';
    } else {
        chip.className = 'badge bg-warning text-dark me-2';
        chip.innerHTML = '<i class="bi bi-arrow-clockwise me-1"></i>polling';
        chip.title = 'SSE unavailable — falling back to 5 s polling';
    }
}

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
    if (tab && tab !== 'overview') params.set('tab', tab);
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
    if (tab && tab !== 'overview') params.set('tab', tab);
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

    document.getElementById('logsJobId').textContent = `Job ID: ${targetId}`;
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

    // Tab selection — deep-link param wins, else fall back to the
    // user's last-selected tab from localStorage (default Overview).
    if (_deepLinkTab) {
        const btnId = _deepLinkTab === 'logs' ? 'logsTab'
            : _deepLinkTab === 'files' ? 'filesTab'
            : 'overviewTab';
        const btn = document.getElementById(btnId);
        if (btn) new bootstrap.Tab(btn).show();
    } else {
        _restoreLastTab();
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
    // SSE-first log streaming (Tier 3.11): open an EventSource for
    // active jobs so new lines push sub-second. The 5 s polling loop
    // stays as a fallback (kicked off when EventSource errors out)
    // AND for everything that doesn't stream (attempts dropdown,
    // file results, operator-action visibility).
    if (isRunning || isChainRow) {
        _startLogStream(targetId);
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
            // If Overview is the active tab, re-render its summary +
            // recent-log preview so the dashboard stays live without
            // forcing the user back to Logs.
            const _overviewActive = document.getElementById('overviewTabPane') &&
                document.getElementById('overviewTabPane').classList.contains('active');
            if (_overviewActive) _renderOverview();
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
        if (_overviewTickInterval) {
            clearInterval(_overviewTickInterval);
            _overviewTickInterval = null;
        }
        _disableJobModalKeyboard();
        _stopLogStream();
        _popModalState();
        _logsModalJobId = null;
        _logsModalAttemptId = null;
    }, { once: true });

    // Render Overview once the modal is open (so any DOM refs inside
    // Overview cards resolve correctly) — separate from the tab
    // restore because the Bootstrap Tab.show() doesn't fire the
    // ``shown`` event synchronously.
    _renderOverview();
    // Restore the log-level filter selection from localStorage.
    _restoreLogLevel();
    // Register keyboard shortcuts (Tier 3.12) — see _onJobModalKeydown
    // for the binding table.
    _enableJobModalKeyboard();
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
    let label;
    let tooltip;
    if (a.is_originating) {
        label = '<i class="bi bi-play-fill me-1"></i>1';
        tooltip = 'Run 1 (original dispatch) · ' + a.status + durSuffix;
    } else {
        // ``bi-arrow-clockwise`` glyph tells the operator this is a retry
        // firing, not the original. The ordinal is the run number
        // (1-based across the chain), so a retry pill rendered as
        // ``↻ 2`` reads as "run 2, which is retry #1".
        label = '<i class="bi bi-arrow-clockwise me-1"></i>' + runOrdinal;
        tooltip = 'Run ' + runOrdinal + ' of ' + maxRuns
            + ' (retry #' + a.retry_attempt + ') · ' + a.status + durSuffix;
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
        // Feed the Overview's timeline component with the same data.
        // Both the pill row and the timeline render from the
        // attempts list — they're two views of the same state.
        _renderAttemptsTimeline(attempts, max);
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
        // Re-render the Overview timeline with the fresh attempts data
        // so the bars reflect new firings without a manual refresh.
        _renderAttemptsTimeline(attempts, max);
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
    // Restart the SSE log stream so it points at the new attempt's
    // log file (the stream is per-targetId).
    if (typeof _restartLogStream === 'function') _restartLogStream();
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
    // Re-render the Overview pane only if it's the active tab —
    // otherwise we'd arm a 1-second tick interval against a hidden
    // DOM (single-instance via clear-before-set, but still wasted
    // CPU on every modal-open with an active retry chain).
    const overviewActive = document.getElementById('overviewTabPane') &&
        document.getElementById('overviewTabPane').classList.contains('active');
    if (overviewActive && typeof _renderOverview === 'function') _renderOverview();
    // Update the URL's ``attempt=`` param so a copy-link points at the
    // newly-selected attempt.
    const activeTab = document.getElementById('logsTabPane') &&
        document.getElementById('logsTabPane').classList.contains('active')
        ? 'logs'
        : (document.getElementById('filesTabPane') &&
           document.getElementById('filesTabPane').classList.contains('active')
            ? 'files' : 'overview');
    _replaceModalState(_logsModalJobId, _logsModalAttemptId, activeTab);
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
    try { localStorage.setItem(_OVERVIEW_LAST_TAB_KEY, 'files'); } catch (e) { /* private mode */ }
    _replaceModalState(_logsModalJobId, _logsModalAttemptId, 'files');
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
