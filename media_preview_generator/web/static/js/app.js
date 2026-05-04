/**
 * Media Preview Generator - Dashboard JavaScript
 */

// Global state
let socket = null;
let libraries = [];
let jobs = [];
let schedules = [];
let _lastNotifiedJobId = null;
let processingPaused = false;
const expandedJobFileRows = new Set();
const expandedActiveJobFiles = new Set();
let cachedWorkerConfigCounts = null;
// Tracks previous fallback state per worker_id so we show a toast exactly
// once when a worker switches from GPU to CPU (not on every poll tick).
const _fallbackStateByWorker = new Map();
let cachedGpuConfig = null;
let cachedDetectedGpus = null;
let _elapsedTimerInterval = null;
let jobsLoadedOnce = false;
let jobPage = 1;
let jobPerPage = parseInt(localStorage.getItem('jobPerPage') || '50', 10);
let jobTotalPages = 1;
let jobTotal = 0;


/**
 * Escape HTML special characters to prevent XSS attacks.
 * @param {string} str - String to escape
 * @returns {string} - Escaped string safe for innerHTML
 */
function escapeHtml(str) {
    if (str === null || str === undefined) {
        return '';
    }
    const div = document.createElement('div');
    div.textContent = String(str);
    return div.innerHTML;
}

/**
 * Sanitize a small fragment of server-rendered HTML before it hits innerHTML.
 * Allows the formatting tags the server uses in notifications (<br>, <strong>,
 * <em>, <code>, <span>, <div>, <ul>/<li>, <a>) and strips everything else —
 * including every event handler attribute (`onclick="..."`), inline-script
 * tags, <iframe>, and any `javascript:` URI. Defence in depth: the server
 * already escapes interpolated values via html.escape(), but a client-side
 * whitelist keeps the notification channel safe even if a future caller
 * forgets to escape something.
 * @param {string} html - HTML fragment
 * @returns {string} - Sanitized HTML fragment
 */
function sanitizeNotificationHtml(html) {
    if (!html) return '';
    var ALLOWED_TAGS = {
        'BR': [],
        'STRONG': [],
        'EM': [],
        'B': [],
        'I': [],
        'U': [],
        'CODE': [],
        'PRE': [],
        'UL': ['class'],
        'OL': ['class'],
        'LI': ['class'],
        'SPAN': ['class'],
        'DIV': ['class'],
        'P': ['class'],
        'SMALL': ['class'],
        // HTML5 disclosure widget — used by the migration card's "What
        // changed" expander. Without these, the unwrap path below would
        // collapse the entire <details> subtree (including allowed <ul>
        // /<li>) into one run-on text node.
        'DETAILS': ['class', 'open'],
        'SUMMARY': ['class'],
        'A': ['href', 'class', 'target', 'rel']
    };
    var template = document.createElement('template');
    template.innerHTML = html;
    var walker = document.createTreeWalker(template.content, NodeFilter.SHOW_ELEMENT);
    var toRemove = [];
    while (walker.nextNode()) {
        var el = walker.currentNode;
        var allowed = ALLOWED_TAGS[el.tagName];
        if (!allowed) {
            toRemove.push(el);
            continue;
        }
        // Strip attributes not on the whitelist; always strip anything that
        // looks like javascript:/data: URIs on href.
        for (var i = el.attributes.length - 1; i >= 0; i--) {
            var attr = el.attributes[i];
            if (allowed.indexOf(attr.name) === -1) {
                el.removeAttribute(attr.name);
                continue;
            }
            if (attr.name === 'href') {
                var v = attr.value.trim().toLowerCase();
                if (v.indexOf('javascript:') === 0 || v.indexOf('data:') === 0) {
                    el.removeAttribute('href');
                }
            }
        }
    }
    // Unwrap disallowed elements by promoting their children, not by
    // collapsing to textContent — otherwise an unknown wrapper element
    // would destroy the structure of every allowed descendant inside it.
    toRemove.forEach(function (el) {
        var parent = el.parentNode;
        if (!parent) return;
        while (el.firstChild) {
            parent.insertBefore(el.firstChild, el);
        }
        parent.removeChild(el);
    });
    return template.innerHTML;
}

var _libraryTypeLabels = {movie: 'Movies', show: 'TV Shows', sports: 'Sports', other_videos: 'Other Videos'};
var _libraryTypeIcons = {movie: 'bi-film', show: 'bi-tv', sports: 'bi-trophy', other_videos: 'bi-camera-video'};

function libraryTypeLabel(lib) {
    return _libraryTypeLabels[lib.display_type] || _libraryTypeLabels[lib.type] || lib.type;
}

function libraryTypeIcon(lib) {
    return _libraryTypeIcons[lib.display_type] || _libraryTypeIcons[lib.type] || 'bi-folder';
}

async function copyToClipboard(text, successMessage = 'Copied to clipboard', errorMessage = 'Failed to copy to clipboard') {
    const stringValue = String(text ?? '');

    try {
        if (window.isSecureContext && navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
            await navigator.clipboard.writeText(stringValue);
            showToast('Copied', successMessage, 'success');
            return true;
        }
    } catch (error) {
        console.debug('Clipboard API unavailable, trying fallback copy.', error);
    }

    // Legacy fallback for non-secure contexts and stricter browser policies.
    // We attach a copy listener so clipboard data is set even if selection-based
    // copying is unreliable in modals or restricted environments.
    const activeElement = document.activeElement;
    const selection = window.getSelection();
    const storedRanges = [];
    if (selection) {
        for (let i = 0; i < selection.rangeCount; i += 1) {
            storedRanges.push(selection.getRangeAt(i));
        }
    }

    const textarea = document.createElement('textarea');
    textarea.value = stringValue;
    textarea.setAttribute('readonly', '');
    textarea.style.position = 'fixed';
    textarea.style.top = '0';
    textarea.style.left = '0';
    textarea.style.opacity = '0';
    textarea.style.pointerEvents = 'none';
    document.body.appendChild(textarea);
    textarea.focus({ preventScroll: true });
    textarea.select();
    textarea.setSelectionRange(0, textarea.value.length);

    let copied = false;
    const onCopy = (event) => {
        if (!event.clipboardData) {
            return;
        }
        event.clipboardData.setData('text/plain', stringValue);
        event.preventDefault();
        copied = true;
    };

    document.addEventListener('copy', onCopy, true);
    try {
        copied = document.execCommand('copy') || copied;
    } catch (error) {
        copied = false;
    } finally {
        document.removeEventListener('copy', onCopy, true);
        document.body.removeChild(textarea);
        if (selection) {
            selection.removeAllRanges();
            storedRanges.forEach((range) => selection.addRange(range));
        }
        if (activeElement && typeof activeElement.focus === 'function') {
            activeElement.focus({ preventScroll: true });
        }
    }

    if (copied) {
        showToast('Copied', successMessage, 'success');
    } else {
        showToast('Error', errorMessage, 'danger');
    }

    return copied;
}

// Initialize dashboard
function initDashboard() {
    connectSocket();

    const perPageSelect = document.getElementById('jobPerPageSelect');
    if (perPageSelect) {
        perPageSelect.value = String(jobPerPage);
    }

    loadJobs().then(() => loadWorkerStatuses());
    refreshStatus();
    loadLibraries();
    loadSchedules().then(() => maybeAutoOpenScheduleEdit());
    loadJobStats();
    loadWorkerConfigCounts();
    loadProcessingState();
    loadPendingWebhooks();
    requestNotificationPermission();

    // Set up auto-refresh
    // System status includes cached GPU detection — poll less frequently
    setInterval(refreshStatus, 120000);
    setInterval(loadJobStats, 10000);
    setInterval(loadJobs, 5000);
    setInterval(loadWorkerStatuses, 1000);
    setInterval(loadPendingWebhooks, 3000);
    setInterval(tickPendingWebhookCountdowns, 1000);

    // Flush deferred job-queue updates when a priority dropdown closes
    document.addEventListener('hidden.bs.dropdown', function () {
        if (_jobQueueUpdatePending) {
            updateJobQueue();
        }
    });
}

// SocketIO Connection
function connectSocket() {
    // Polling-only — matches allow_upgrades=False on the server. WebSocket
    // pinned a gunicorn thread per browser tab and dead CLOSE_WAIT sockets
    // exhausted the pool. Skip the WS upgrade attempt entirely so we don't
    // pay the failed-handshake round-trip on every reconnect.
    socket = io('/jobs', {
        transports: ['polling'],
        reconnection: true,
        reconnectionAttempts: 10,
        reconnectionDelay: 1000
    });

    socket.on('connect', function() {
        console.log('Connected to SocketIO');
        // Reload data on reconnect to get current state
        loadJobs();
        loadWorkerStatuses();
        loadJobStats();
    });

    socket.on('disconnect', function() {
        console.log('Disconnected from SocketIO');
    });

    socket.on('connect_error', function(error) {
        console.error('SocketIO connection error:', error);
    });

    // Job events
    socket.on('job_created', function(job) {
        console.log('Job created:', job);
        loadJobs();
        loadJobStats();
        showToast('Job Created', `Job ${job.id.substring(0, 8)} created`, 'info');
    });

    socket.on('job_updated', function(job) {
        loadJobs();
    });

    socket.on('job_started', function(job) {
        console.log('Job started:', job);
        loadJobs();
        loadJobStats();
    });

    socket.on('job_progress', function(data) {
        updateJobProgress(data.job_id, data.progress);
    });

    socket.on('worker_update', function(data) {
        const workers = data.workers || [];
        if (workers.length > 0) {
            updateWorkerStatuses(workers);
        } else {
            updateWorkerStatuses([], { keepBadgeCounts: true });
        }
    });

    socket.on('job_completed', function(job) {
        console.log('Job completed:', job);
        loadJobs();
        loadJobStats();
        loadWorkerStatuses();
        removeActiveJob(job.id);
        if (job.error) {
            showToast('Job completed with warnings', job.error, 'warning');
            showNotification('Job completed with warnings', job.error, 'warning');
        } else {
            showToast('Job Completed', `Job ${job.id.substring(0, 8)} completed successfully`, 'success');
            showNotification('Job Completed', `Processing finished for ${job.library_name || 'All Libraries'}`, 'success');
        }
    });

    socket.on('job_failed', function(job) {
        console.log('Job failed:', job);
        loadJobs();
        loadJobStats();
        loadWorkerStatuses();
        removeActiveJob(job.id);
        showToast('Job Failed', `Job ${job.id.substring(0, 8)} failed: ${job.error}`, 'danger');
        showNotification('Job Failed', job.error || 'Unknown error', 'error');
    });

    socket.on('job_cancelled', function(job) {
        console.log('Job cancelled:', job);
        loadJobs();
        loadJobStats();
        loadWorkerStatuses();
        removeActiveJob(job.id);
        showToast('Job Cancelled', `Job ${job.id.substring(0, 8)} cancelled`, 'warning');
    });

    socket.on('job_paused', function(data) {
        console.log('Job paused:', data);
        loadJobs();
    });

    socket.on('job_resumed', function(data) {
        console.log('Job resumed:', data);
        loadJobs();
    });

    socket.on('processing_paused_changed', function(data) {
        processingPaused = !!data.paused;
        renderGlobalPauseResume();
        loadJobs();
        // D21 — keep the Quiet Hours card badge ("on" vs "paused now")
        // in sync the moment the global pause flag flips, whether from
        // a quiet-hours boundary cron or the manual Pause All button.
        if (typeof window._refreshQuietHoursBadge === 'function') {
            // Update the cached config too so the badge has the latest value
            // before re-rendering.
            if (window._quietHoursConfig) {
                window._quietHoursConfig.currently_in_quiet_window = !!data.paused
                    && !!window._quietHoursConfig.enabled;
            }
            window._refreshQuietHoursBadge();
        }
    });

}

// API Helpers

/**
 * Read the CSRF token from the <meta name="csrf-token"> tag injected by Flask.
 * @returns {string} The CSRF token, or empty string if not found.
 */
function getCsrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute('content') : '';
}

// Extract a user-friendly error message from a non-OK fetch Response.
// Prefers the backend's `{ error: "..." }` field when present, so callers
// surface "Could not connect to Plex at ..." instead of "HTTP 502: BAD GATEWAY".
async function _extractApiError(response) {
    const text = await response.text().catch(() => '');
    if (text) {
        try {
            const json = JSON.parse(text);
            if (json && typeof json.error === 'string' && json.error.trim()) {
                return json.error.trim();
            }
        } catch (_e) {
            // Non-JSON body — fall through to the generic status line.
        }
    }
    return `HTTP ${response.status}: ${response.statusText}`;
}

async function apiGet(url) {
    const response = await fetch(url);
    if (!response.ok) {
        if (response.status === 401) {
            console.error('Authentication failed, redirecting to login');
            window.location.href = '/login';
            throw new Error('Authentication required');
        }
        throw new Error(await _extractApiError(response));
    }
    return response.json();
}

async function apiPost(url, data = {}) {
    const response = await fetch(url, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': getCsrfToken()
        },
        body: JSON.stringify(data)
    });
    if (!response.ok) {
        throw new Error(await _extractApiError(response));
    }
    return response.json();
}

async function apiDelete(url) {
    const response = await fetch(url, {
        method: 'DELETE',
        headers: { 'X-CSRFToken': getCsrfToken() }
    });
    if (!response.ok) {
        throw new Error(await _extractApiError(response));
    }
    return response.json();
}

async function apiPut(url, data = {}) {
    const response = await fetch(url, {
        method: 'PUT',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': getCsrfToken()
        },
        body: JSON.stringify(data)
    });
    if (!response.ok) {
        throw new Error(await _extractApiError(response));
    }
    return response.json();
}

// Load Functions
async function refreshStatus() {
    // Each section is independently try-caught so a failure in one
    // (e.g. /api/system/config) does not cascade to others.

    // --- System status (GPU info + running job) ---
    try {
        const status = await apiGet('/api/system/status');
        updateSystemStatus(status);
    } catch (error) {
        console.error('Failed to refresh system status:', error);
        const statusEl = document.getElementById('systemStatus');
        if (statusEl && !error.message.includes('Authentication')) {
            statusEl.innerHTML = '<div class="text-danger small"><i class="bi bi-exclamation-triangle me-2"></i>Failed to load status</div>';
        }
    }

    // --- Media servers status (one row per configured server) ---
    await updateMediaServersStatus();

    // --- Worker thread counts ---
    // Keep config counts cached for idle-state badge rendering. Actual live
    // worker badges are updated by /api/jobs/workers to avoid race/flicker.
    try {
        await loadWorkerConfigCounts(true);
        renderDashboardGpuConfig();
    } catch (e) {
        console.warn('Failed to load worker config:', e);
    }
}

function normalizeWorkerConfigCounts(config) {
    // gpu_threads is the total across all enabled GPUs (computed from gpu_config)
    return {
        gpu_threads: Number(config?.gpu_threads ?? 0),
        cpu_threads: Number(config?.cpu_threads ?? 1)
    };
}

async function loadWorkerConfigCounts(forceRefresh = false) {
    if (cachedWorkerConfigCounts && !forceRefresh) {
        return cachedWorkerConfigCounts;
    }

    try {
        const config = await apiGet('/api/system/config');
        cachedWorkerConfigCounts = normalizeWorkerConfigCounts(config);
        cachedGpuConfig = config.gpu_config || [];
        renderNoWorkersWarning(config.config_warning || '');
        return cachedWorkerConfigCounts;
    } catch (e) {
        console.warn('Failed to cache worker config counts:', e);
    }

    return cachedWorkerConfigCounts;
}

// Tracks whether the last /api/libraries call failed so modal renderers
// (which read the cached `libraries` array) can tell "no libraries" apart
// from "couldn't load libraries" and show the right message.
let librariesLoadError = null;

// Map a backend status string from /api/system/media-servers into a
// (label, badge-class) pair so the dashboard renders consistently.
const _MEDIA_SERVER_STATUS_BADGES = {
    connected:     { label: 'Connected',     cls: 'bg-success' },
    unreachable:   { label: "Can't reach",   cls: 'bg-warning text-dark' },
    unauthorised:  { label: 'Auth failed',   cls: 'bg-warning text-dark' },
    misconfigured: { label: 'Misconfigured', cls: 'bg-danger' },
    disabled:      { label: 'Disabled',      cls: 'bg-secondary' },
};

const _MEDIA_SERVER_TYPE_ICONS = {
    plex:     'bi-play-btn',
    emby:     'bi-emoji-laughing',
    jellyfin: 'bi-cup-hot',
};

// Vendor SVG logos shipped under /static/images/vendors/. Returns an
// <img> tag for the given server type, or null when the type is unknown
// (callers fall back to the Bootstrap icon).
function _vendorLogo(type, size) {
    const stype = (type || '').toLowerCase();
    if (!['plex', 'emby', 'jellyfin'].includes(stype)) return null;
    const px = Number(size) || 18;
    // alt="" + aria-hidden so screen-readers + copy/paste don't double-announce
    // the vendor name (visible badge text already says "Plex"/"Emby"/etc.).
    // Without this, users reading the row see "plexPlex 1 not indexed yet"
    // instead of just "Plex 1 not indexed yet" because the alt-text bleeds
    // into rendered textContent in many AT and copy paths.
    return (
        `<img src="/static/images/vendors/${stype}.svg" alt="" aria-hidden="true" ` +
        `width="${px}" height="${px}" class="vendor-logo" ` +
        `style="vertical-align: -3px; margin-right: 4px;">`
    );
}

// Refresh the dashboard "Media Servers" rows from /api/system/media-servers.
// Renders one row per configured server with vendor icon + status badge.
// Empty state nudges the user to /servers.
async function updateMediaServersStatus() {
    const container = document.getElementById('mediaServersStatus');
    const emptyCard = document.getElementById('noServersEmptyState');
    if (!container) return;

    let payload;
    try {
        payload = await apiGet('/api/system/media-servers');
    } catch (e) {
        console.warn('Failed to load media-server status:', e);
        container.innerHTML =
            '<div class="text-danger small">' +
            '<i class="bi bi-exclamation-triangle me-2"></i>' +
            'Failed to load media-server status' +
            '</div>';
        return;
    }

    const servers = (payload && payload.servers) || [];
    if (servers.length === 0) {
        // Show the dashboard empty-state card (Phase H1) and a small inline note.
        if (emptyCard) emptyCard.classList.remove('d-none');
        container.innerHTML =
            '<div class="small text-muted">' +
            '<i class="bi bi-hdd-network me-2"></i>' +
            'No media servers configured. ' +
            '<a href="/servers">Add one</a>.' +
            '</div>';
        return;
    }
    // Hide the empty-state once at least one server is configured.
    if (emptyCard) emptyCard.classList.add('d-none');

    const rows = servers.map(s => {
        const badge = _MEDIA_SERVER_STATUS_BADGES[s.status]
            || { label: s.status || 'Unknown', cls: 'bg-secondary' };
        const typeLabel = (s.type || '').toUpperCase();
        const tooltip = s.error ? ` title="${escapeHtmlAttr(s.error)}"` : '';
        const url = s.url ? `<div class="small text-muted text-truncate" style="max-width: 100%;">${escapeHtmlText(s.url)}</div>` : '';
        // Prefer the vendor SVG logo; fall back to the Bootstrap icon when
        // the server type is unknown (defensive — should never happen for
        // configured servers).
        const logo = _vendorLogo(s.type, 18) ||
            `<i class="bi ${_MEDIA_SERVER_TYPE_ICONS[s.type] || 'bi-hdd-network'} me-2"></i>`;
        return `
            <div class="d-flex justify-content-between align-items-start mb-2">
                <div class="d-flex flex-column" style="min-width: 0;">
                    <span>${logo}<strong>${escapeHtmlText(s.name || typeLabel || 'Server')}</strong></span>
                    ${url}
                </div>
                <span class="badge ${badge.cls}"${tooltip}>${escapeHtmlText(badge.label)}</span>
            </div>
        `;
    }).join('');

    container.innerHTML = rows;
}

function escapeHtmlText(str) {
    if (str == null) return '';
    const div = document.createElement('div');
    div.textContent = String(str);
    return div.innerHTML;
}

function escapeHtmlAttr(str) {
    return escapeHtmlText(str).replace(/"/g, '&quot;');
}

async function loadLibraries() {
    try {
        const data = await apiGet('/api/libraries');
        libraries = data.libraries || [];
        librariesLoadError = null;
        await updateLibraryList();
        updateMediaServersStatus();
    } catch (error) {
        console.error('Failed to load libraries:', error);
        librariesLoadError = error.message || 'Unknown error';
        updateMediaServersStatus();

        const listEl = document.getElementById('libraryList');
        if (!listEl) return;

        // Dashboard Quick Actions teaser stays short and actionable; the
        // Settings page gets the full backend-supplied detail since it's a
        // dedicated troubleshooting surface.
        const parentCardHeader = listEl.closest('.card')?.querySelector('.card-header')?.textContent || '';
        const isDashboardTeaser = parentCardHeader.includes('Quick Actions');

        if (isDashboardTeaser) {
            listEl.innerHTML =
                '<div class="text-warning small d-flex align-items-start gap-2">' +
                '<i class="bi bi-exclamation-triangle-fill mt-1"></i>' +
                '<span>Can\'t load libraries right now. ' +
                '<a href="/settings" class="text-decoration-none">Check your Plex connection</a>.</span>' +
                '</div>';
        } else {
            listEl.innerHTML =
                `<div class="text-danger small">Failed to load libraries. ${escapeHtml(librariesLoadError)}</div>`;
        }
    }
}

async function loadJobs() {
    try {
        const data = await apiGet(`/api/jobs?page=${jobPage}&per_page=${jobPerPage}`);
        jobs = data.jobs || [];
        jobTotal = data.total || 0;
        jobTotalPages = data.pages || 1;
        if (jobPage > jobTotalPages) {
            jobPage = jobTotalPages;
        }
        jobsLoadedOnce = true;
        updateJobQueue();
        renderJobPagination();

        // Update active jobs section (supports multiple running jobs)
        const runningJobs = jobs.filter(j => j.status === 'running');
        updateActiveJobs(runningJobs);

        // Replay any progress events that arrived before the DOM was ready.
        for (const jid of Object.keys(_pendingProgress)) {
            updateJobProgress(jid, _pendingProgress[jid]);
        }
    } catch (error) {
        console.error('Failed to load jobs:', error);
        // Show empty state instead of error - jobs list may just be unavailable temporarily
        const tbody = document.getElementById('jobQueue');
        if (tbody && !error.message.includes('Authentication')) {
            // Show a less alarming message
            tbody.innerHTML = `
                <tr>
                    <td colspan="7" class="text-center text-muted py-4">
                        <i class="bi bi-hourglass-split me-2"></i>Loading job queue...
                    </td>
                </tr>
            `;
        }
    }
}

async function loadSchedules() {
    try {
        const data = await apiGet('/api/schedules');
        schedules = data.schedules || [];
        updateScheduleList();
    } catch (error) {
        console.error('Failed to load schedules:', error);
    }
}

// Check for ?editSchedule=<id> in the URL on page load — when a user
// clicked "Edit" on a scanner from the Triggers tab we want to scroll
// to the schedules table and open the edit modal for that specific
// schedule.  Only does work when the full schedule table is on the
// page (the Automation page); on the Dashboard it redirects to /automation
// so stale links still land in the right place.
function maybeAutoOpenScheduleEdit() {
    try {
        const params = new URLSearchParams(window.location.search);
        const editId = params.get('editSchedule');
        if (!editId) return;

        const scheduleTable = document.getElementById('scheduleList');
        if (!scheduleTable) {
            // Dashboard or any page without the full table — redirect to
            // the Automation page (Schedules tab) and let it handle the edit.
            window.location.replace(
                '/automation?tab=schedules&editSchedule=' + encodeURIComponent(editId) + '#schedules'
            );
            return;
        }

        // Scroll the schedules card into view with a bit of breathing
        // room below the sticky navbar.
        const row = scheduleTable.closest('.card');
        const navbar = document.querySelector('.navbar.sticky-top');
        const navHeight = navbar ? navbar.offsetHeight : 56;
        const target = (row || scheduleTable).getBoundingClientRect().top + window.scrollY - navHeight - 16;
        window.scrollTo({ top: target < 0 ? 0 : target, behavior: 'smooth' });

        if (schedules.some(s => s.id === editId)) {
            // Tiny delay so the scroll animation starts before the modal
            // opens — feels smoother than a dead-snap.
            setTimeout(() => showEditScheduleModal(editId), 200);
        } else {
            console.warn('editSchedule=' + editId + ' not found in schedules list');
            showToast('Not found', 'Schedule not found — it may have been deleted.', 'warning');
        }

        // Clean the query param from the URL so a refresh doesn't re-trigger.
        if (window.history && window.history.replaceState) {
            const url = new URL(window.location.href);
            url.searchParams.delete('editSchedule');
            window.history.replaceState(null, '', url.pathname + url.search + url.hash);
        }
    } catch (e) {
        console.error('maybeAutoOpenScheduleEdit failed:', e);
    }
}


async function loadJobStats() {
    // Job Statistics card lives only on the dashboard; bail when its
    // elements are absent so SocketIO reconnect from any other page
    // doesn't throw on the first getElementById().textContent assignment.
    if (!document.getElementById('statPending')) {
        return;
    }
    try {
        const stats = await apiGet('/api/jobs/stats');
        document.getElementById('statPending').textContent = stats.pending || 0;
        document.getElementById('statRunning').textContent = stats.running || 0;
        document.getElementById('statCompleted').textContent = stats.completed || 0;
        document.getElementById('statFailed').textContent = stats.failed || 0;
        document.getElementById('statCancelled').textContent = stats.cancelled || 0;
        document.getElementById('statTotal').textContent = stats.total || 0;
    } catch (error) {
        console.error('Failed to load job stats:', error);
    }
}

async function loadProcessingState() {
    try {
        const data = await apiGet('/api/processing/state');
        processingPaused = !!data.paused;
        renderGlobalPauseResume();
    } catch (error) {
        console.error('Failed to load processing state:', error);
    }
}

async function loadPendingWebhooks() {
    const row = document.getElementById('pendingWebhooksRow');
    const content = document.getElementById('pendingWebhooksContent');
    if (!row || !content) return;

    try {
        const data = await apiGet('/api/webhooks/pending');
        const pending = data.pending || [];
        if (pending.length === 0) {
            row.classList.add('d-none');
            return;
        }
        row.classList.remove('d-none');
        const now = Date.now();
        const parts = pending.map(function (p) {
            const remainingSec = Math.max(0, Number(p.remaining_seconds) || 0);
            const deadline = now + remainingSec * 1000;
            const initial = Math.max(0, Math.ceil(remainingSec));
            const source = escapeHtml(p.source || 'webhook');
            const label = p.file_count === 1 && p.first_title
                ? escapeHtml(p.first_title)
                : `${p.file_count} file(s)`;
            // "Fire now" button — short-circuits the debounce delay so the
            // user can dispatch immediately when they don't want to wait.
            // Encoded key stays attribute-safe (the key includes the source +
            // path basename which can contain quotes / spaces).
            const keyAttr = escapeHtmlAttr(p.key || '');
            const fireBtn = p.key
                ? ` <button type="button" class="btn btn-sm btn-outline-primary py-0 px-2 ms-2 pending-fire-now"
                           data-key="${keyAttr}" title="Skip the delay and dispatch this batch now"
                           style="font-size: 0.72rem; line-height: 1.4;">
                       <i class="bi bi-lightning-charge-fill me-1"></i>Fire now
                   </button>`
                : '';
            return `<strong>${source}</strong>: ${label} — starting in <strong class="pending-webhook-countdown" data-deadline="${deadline}">${initial}s</strong>${fireBtn}`;
        });
        content.innerHTML = parts.join(' &middot; ');
    } catch (error) {
        row.classList.add('d-none');
    }
}

function tickPendingWebhookCountdowns() {
    const now = Date.now();
    document.querySelectorAll('.pending-webhook-countdown[data-deadline]').forEach(function (el) {
        const deadline = Number(el.getAttribute('data-deadline'));
        if (!Number.isFinite(deadline)) return;
        const remaining = Math.max(0, Math.ceil((deadline - now) / 1000));
        const text = remaining + 's';
        if (el.textContent !== text) el.textContent = text;
    });
}

// Click delegation for "Fire now" buttons inside the pending-webhooks
// banner. POSTs /api/webhooks/pending/<key>/fire-now, then immediately
// reloads the banner (the row should disappear because the batch was
// dispatched) and the jobs list (the new job should appear).
document.addEventListener('click', async function (e) {
    const btn = e.target.closest && e.target.closest('.pending-fire-now');
    if (!btn) return;
    e.preventDefault();
    const key = btn.getAttribute('data-key') || '';
    if (!key) return;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1" role="status"></span>Firing…';
    try {
        await apiPost('/api/webhooks/pending/' + encodeURIComponent(key) + '/fire-now');
        showToast('Webhook Fired', 'Skipped the delay — dispatching now.', 'success');
        // Refresh the banner (row should disappear) and the jobs list
        // (the new job should appear within ~1s).
        await loadPendingWebhooks();
        await loadJobs();
    } catch (error) {
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-lightning-charge-fill me-1"></i>Fire now';
        showToast('Error', 'Could not fire webhook: ' + (error && error.message || error), 'danger');
    }
});

function renderGlobalPauseResume() {
    const pauseTitle = 'Pause all processing. No new jobs will start; active job will stop dispatching new tasks after current ones finish.';
    const resumeTitle = 'Resume processing. New jobs can start and dispatch will continue.';
    // Hide the verbose "Pause Processing" / "Resume Processing" label on
    // mobile (xs <576px) — keep the icon. Title attr + aria-label keep
    // it accessible and tooltip-discoverable. On desktop the full label
    // returns via .d-sm-inline.
    const pauseBtn = `<button class="btn btn-sm btn-outline-warning text-nowrap" onclick="pauseProcessing()" title="${escapeHtml(pauseTitle)}" aria-label="Pause processing">
        <i class="bi bi-pause-fill"></i><span class="d-none d-sm-inline ms-1">Pause Processing</span>
    </button>`;
    const resumeBtn = `<button class="btn btn-sm btn-outline-success text-nowrap" onclick="resumeProcessing()" title="${escapeHtml(resumeTitle)}" aria-label="Resume processing">
        <i class="bi bi-play-fill"></i><span class="d-none d-sm-inline ms-1">Resume Processing</span>
    </button>`;
    const html = processingPaused ? resumeBtn : pauseBtn;
    const elCurrent = document.getElementById('globalPauseResumeCurrentJob');
    const elQueue = document.getElementById('globalPauseResumeQueue');
    if (elCurrent) elCurrent.innerHTML = html;
    if (elQueue) elQueue.innerHTML = html;
}

async function pauseProcessing() {
    try {
        await apiPost('/api/processing/pause');
        processingPaused = true;
        renderGlobalPauseResume();
        await loadJobs();
        showToast('Processing Paused', 'No new jobs will start; active job will finish current tasks then idle.', 'warning');
    } catch (error) {
        showToast('Error', 'Failed to pause processing: ' + error.message, 'danger');
    }
}

async function resumeProcessing() {
    try {
        await apiPost('/api/processing/resume');
        processingPaused = false;
        renderGlobalPauseResume();
        await loadJobs();
        showToast('Processing Resumed', 'New jobs can start and dispatch will continue.', 'success');
    } catch (error) {
        showToast('Error', 'Failed to resume processing: ' + error.message, 'danger');
    }
}

// Update Functions
function updateSystemStatus(status) {
    let html = '';

    // Cache detected GPUs for the GPU Workers section
    if (status.gpus && status.gpus.length > 0) {
        cachedDetectedGpus = status.gpus;
    } else {
        cachedDetectedGpus = [];
    }

    // Status row — matches the .system-section-title pattern of its
    // sibling sections (Media Servers, Worker Pool) so it doesn't read
    // as an orphan h6 between them.
    html += '<h6 class="system-section-title">Status</h6>';
    html += '<div class="d-flex align-items-center">';
    if (status.running_job) {
        html += `<span class="badge bg-primary">Processing</span>`;
    } else if (status.pending_jobs > 0) {
        html += `<span class="badge bg-secondary">${status.pending_jobs} job(s) pending</span>`;
    } else {
        html += `<span class="badge bg-success">Idle</span>`;
    }
    html += '</div>';

    document.getElementById('systemStatus').innerHTML = html;

    // Timezone + Vulkan warnings live in the bell-icon notification
    // center (see loadNotifications()), not as dashboard banners.

    renderDashboardGpuConfig();
}

// Copy the plain-text Vulkan diagnostic bundle from /api/system/vulkan/debug
// to the clipboard. Bound to the "Copy diagnostic bundle" button inside
// the dashboard and settings-page vulkan warning banners.
function copyVulkanDiagnosticBundle(btn) {
    var originalHtml = btn ? btn.innerHTML : '';
    function restore(html, ms) {
        if (!btn) return;
        setTimeout(function () { btn.innerHTML = originalHtml; }, ms || 2000);
        btn.innerHTML = html;
    }
    fetch('/api/system/vulkan/debug')
        .then(function (r) {
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.text();
        })
        .then(function (text) {
            if (navigator.clipboard && navigator.clipboard.writeText) {
                return navigator.clipboard.writeText(text).then(function () { return text; });
            }
            // Fallback for older browsers / non-secure contexts.
            var ta = document.createElement('textarea');
            ta.value = text;
            ta.setAttribute('readonly', '');
            ta.style.position = 'absolute';
            ta.style.left = '-9999px';
            document.body.appendChild(ta);
            ta.select();
            document.execCommand('copy');
            document.body.removeChild(ta);
            return text;
        })
        .then(function () {
            restore('<i class="bi bi-check2 me-1"></i>Copied to clipboard');
        })
        .catch(function (err) {
            console.error('copyVulkanDiagnosticBundle failed:', err);
            restore('<i class="bi bi-x-circle me-1"></i>Copy failed — see console', 4000);
        });
}


function renderNoWorkersWarning(message) {
    let banner = document.getElementById('noWorkersWarningBanner');
    if (!message) {
        if (banner) banner.classList.add('d-none');
        return;
    }
    if (!banner) {
        const row = document.createElement('div');
        row.id = 'noWorkersWarningRow';
        row.className = 'row';
        row.innerHTML = `<div class="col-12 mb-3">
            <div class="alert alert-warning mb-0 d-flex align-items-center" id="noWorkersWarningBanner">
                <i class="bi bi-exclamation-triangle-fill me-2"></i>
                <span id="noWorkersWarningText"></span>
            </div>
        </div>`;
        const workerSection = document.getElementById('workerStatusContainer');
        if (workerSection) {
            workerSection.closest('.row').before(row);
        }
        banner = document.getElementById('noWorkersWarningBanner');
    }
    const textEl = document.getElementById('noWorkersWarningText');
    if (textEl) textEl.textContent = message;
    banner.classList.remove('d-none');
}

function renderDashboardGpuConfig() {
    const container = document.getElementById('gpuWorkerConfig');
    if (!container) return;

    const gpus = cachedDetectedGpus || [];
    const gpuConfig = cachedGpuConfig || [];

    if (gpus.length === 0) {
        container.innerHTML = '<span class="text-muted small">No GPUs detected</span>';
        return;
    }

    const configByDevice = {};
    gpuConfig.forEach(c => { if (c.device) configByDevice[c.device] = c; });

    let html = '<h6 class="mb-2"><i class="bi bi-gpu-card me-2"></i>GPU Workers</h6>';
    for (const gpu of gpus) {
        const device = gpu.device || '';
        const saved = configByDevice[device] || {};
        const safeDevice = escapeHtml(device);
        const isFailed = gpu.status === 'failed';

        const fullNameTitle = escapeHtml(`${gpu.name || 'GPU'}${device ? ' · ' + device : ''}`);
        if (isFailed) {
            const errorTitle = escapeHtml(gpu.error || 'GPU unusable');
            const errorDetail = escapeHtml(gpu.error_detail || '');
            html += `<div class="d-flex justify-content-between align-items-center mb-2">`;
            html += `<span class="text-truncate me-2" style="max-width: 70%;" title="${fullNameTitle}">`;
            html += `<span class="badge bg-primary me-1" style="font-size: 0.65em;">${escapeHtml(gpu.type).toUpperCase()}</span>`;
            html += `${escapeHtml(gpu.name)} <span class="badge bg-danger">failed</span>`;
            html += `</span>`;
            html += `<span><i class="bi bi-exclamation-triangle-fill text-danger" style="cursor:pointer;" `;
            html += `data-bs-toggle="popover" data-bs-trigger="click" data-bs-placement="left" `;
            html += `data-bs-title="${errorTitle}" `;
            html += `data-bs-content="${errorDetail}"></i></span>`;
            html += `</div>`;
            continue;
        }

        const enabled = saved.enabled !== undefined ? saved.enabled : true;
        const workers = saved.workers !== undefined ? saved.workers : 1;
        const statusBadge = enabled
            ? '<span class="badge bg-success">enabled</span>'
            : '<span class="badge bg-secondary">disabled</span>';

        html += `<div class="d-flex justify-content-between align-items-center mb-2">`;
        html += `<span class="text-truncate me-2" style="max-width: 55%;" title="${fullNameTitle}">`;
        html += `<span class="badge bg-primary me-1" style="font-size: 0.65em;">${escapeHtml(gpu.type).toUpperCase()}</span>`;
        html += `${escapeHtml(gpu.name)} ${statusBadge}`;
        html += `</span>`;
        if (enabled) {
            html += `<span class="d-flex align-items-center gap-1">`;
            html += `<button type="button" class="btn btn-sm btn-outline-secondary gpu-scale-btn" onclick="scaleGpuWorkers('${safeDevice}', -1)" title="Remove one worker"${workers <= 0 ? ' disabled' : ''}><i class="bi bi-dash-lg"></i></button>`;
            html += `<span class="badge bg-primary gpu-worker-badge" data-device="${safeDevice}" style="min-width: 1.5rem;">${workers}</span>`;
            html += `<button type="button" class="btn btn-sm btn-outline-success gpu-scale-btn" onclick="scaleGpuWorkers('${safeDevice}', 1)" title="Add one worker"><i class="bi bi-plus-lg"></i></button>`;
            html += `</span>`;
        } else {
            html += `<button type="button" class="btn btn-sm btn-outline-success" onclick="scaleGpuWorkers('${safeDevice}', 1)" title="Enable with 1 worker"><i class="bi bi-power me-1"></i>Enable</button>`;
        }
        html += `</div>`;
    }
    html += `<div class="mt-1"><a href="/settings#gpu-configuration" class="small text-decoration-none"><i class="bi bi-gear me-1"></i>Configure GPUs in Settings</a></div>`;
    container.innerHTML = html;

    container.querySelectorAll('[data-bs-toggle="popover"]').forEach(el => {
        new bootstrap.Popover(el, { html: false });
    });
}

async function scaleGpuWorkers(device, direction) {
    const detectedGpu = (cachedDetectedGpus || []).find(g => g.device === device);
    if (detectedGpu && detectedGpu.status === 'failed') return;

    const gpuConfig = cachedGpuConfig ? JSON.parse(JSON.stringify(cachedGpuConfig)) : [];
    let entry = gpuConfig.find(e => e.device === device);

    if (!entry) {
        if (!detectedGpu) return;
        entry = {
            device: device,
            name: detectedGpu.name || 'GPU',
            type: detectedGpu.type || '',
            enabled: true,
            workers: 1,
            ffmpeg_threads: 2
        };
        gpuConfig.push(entry);
    }

    const prevWorkers = entry.workers || 0;
    const prevEnabled = entry.enabled !== false;
    const newWorkers = Math.max(0, prevWorkers + direction);
    if (newWorkers === prevWorkers) return;
    entry.workers = newWorkers;
    if (newWorkers === 0) {
        entry.enabled = false;
    } else if (newWorkers > 0) {
        entry.enabled = true;
    }

    try {
        const saveResult = await apiPost('/api/settings', { gpu_config: gpuConfig });
        cachedGpuConfig = gpuConfig;
        cachedWorkerConfigCounts = null;
        await loadWorkerConfigCounts(true);
        renderDashboardGpuConfig();
        await Promise.all([loadJobs(), loadWorkerStatuses(), refreshStatus()]);

        if (saveResult.warning) {
            showToast('Warning', saveResult.warning, 'warning');
        } else {
            showToast('Workers Updated', `GPU workers for ${device} set to ${newWorkers}`, 'success');
        }
    } catch (error) {
        entry.workers = prevWorkers;
        entry.enabled = prevEnabled;
        renderDashboardGpuConfig();
        showToast('Error', `Failed to update GPU workers: ${error.message}`, 'danger');
    }
}

async function updateLibraryList() {
    const listEl = document.getElementById('libraryList');
    if (!listEl) return;

    // Phase H6: replaced the flat per-library list with a compact summary.
    // The full per-server library detail lives on /servers; the new New Job
    // modal handles per-server picking. This card just orients the user.
    if (!libraries || libraries.length === 0) {
        listEl.innerHTML =
            '<div class="text-muted small">' +
            'No libraries enabled. <a href="/servers" class="text-decoration-none">Add or enable libraries on the Servers page</a>.' +
            '</div>';
        return;
    }

    const distinctServers = new Set();
    for (const l of libraries) {
        if (l && l.server_id) distinctServers.add(l.server_id);
    }
    const serverCount = distinctServers.size || 1;
    const libCount = libraries.length;

    listEl.innerHTML =
        `<div class="d-flex justify-content-between align-items-center">` +
        `<span class="small"><strong>${libCount}</strong> librar${libCount === 1 ? 'y' : 'ies'} ` +
        `across <strong>${serverCount}</strong> server${serverCount === 1 ? '' : 's'}</span>` +
        `<a href="/servers" class="small text-decoration-none">Manage <i class="bi bi-arrow-right-short"></i></a>` +
        `</div>` +
        `<div class="form-text mt-1">Use <strong>Start New Job</strong> above to scan a specific server / libraries.</div>`;
}

// Refresh the Schedules library checkbox group when the user picks a different
// server in the modal. Hits /api/libraries?server_id=<id> when a specific
// server is chosen so non-Plex servers (Emby/Jellyfin) are also covered.
async function onScheduleServerChange() {
    const sel = document.getElementById('scheduleServer');
    if (!sel) return;
    const serverId = sel.value;
    try {
        const url = serverId
            ? `/api/libraries?server_id=${encodeURIComponent(serverId)}`
            : '/api/libraries';
        const data = await apiGet(url);
        libraries = data.libraries || [];
        _renderScheduleLibraryList(libraries, serverId || null);
    } catch (e) {
        console.warn('Failed to refresh libraries for server change:', e);
        showToast('Schedules', 'Could not load libraries for the selected server', 'warning');
    }
}

// Phase H7: render the Schedules modal library checkbox group. Same group-by-
// server pattern as the New Job modal (H6). When pinned to one server, render
// flat. Each checkbox is disabled while "All Libraries" is checked.
function _renderScheduleLibraryList(libs, filterServerId) {
    const listEl = document.getElementById('scheduleLibraryList');
    if (!listEl) return;
    if (!libs || libs.length === 0) {
        listEl.innerHTML = '<div class="text-muted small">No libraries available for this selection.</div>';
        return;
    }
    const allDisabled = document.getElementById('scheduleLibraryAll').checked;
    const renderRow = (lib, indent) => `
        <div class="form-check ${indent ? 'ms-2' : ''}">
            <input class="form-check-input schedule-library-checkbox" type="checkbox"
                   value="${lib.id}" id="schedLib_${lib.id}" ${allDisabled ? 'disabled' : ''}>
            <label class="form-check-label" for="schedLib_${lib.id}">
                ${escapeHtml(lib.name)} <span class="text-muted small">(${libraryTypeLabel(lib)})</span>
            </label>
        </div>
    `;
    if (filterServerId) {
        listEl.innerHTML = libs.map(l => renderRow(l, false)).join('');
        return;
    }
    const groups = new Map();
    for (const lib of libs) {
        const key = lib.server_id || '__legacy__';
        if (!groups.has(key)) {
            groups.set(key, { server_name: lib.server_name || '', server_type: lib.server_type || '', libs: [] });
        }
        groups.get(key).libs.push(lib);
    }
    const sections = [];
    for (const [_, grp] of groups) {
        const stype = (grp.server_type || '').toLowerCase();
        const logo = _vendorLogo(stype, 14) || '';
        const head = `<div class="text-muted small mt-2 mb-1">${logo}<strong>${escapeHtml(grp.server_name || stype.toUpperCase() || 'Server')}</strong></div>`;
        sections.push(head + grp.libs.map(l => renderRow(l, true)).join(''));
    }
    listEl.innerHTML = sections.join('');
}

function onScheduleLibraryAllChange(checkbox) {
    document.querySelectorAll('.schedule-library-checkbox').forEach(cb => {
        cb.disabled = checkbox.checked;
        if (checkbox.checked) cb.checked = false;
    });
}

function setScheduleLibrariesChecked(checked) {
    const allCb = document.getElementById('scheduleLibraryAll');
    if (allCb && allCb.checked) {
        allCb.checked = false;
        onScheduleLibraryAllChange(allCb);
    }
    document.querySelectorAll('.schedule-library-checkbox').forEach(cb => {
        cb.disabled = false;
        cb.checked = checked;
    });
}

// Populate the Schedules modal's "Media Server" dropdown from /api/servers.
async function _populateScheduleServerPicker(currentServerId) {
    const sel = document.getElementById('scheduleServer');
    if (!sel) return;
    try {
        const data = await apiGet('/api/servers');
        const servers = (data.servers || []).filter(s => s.enabled !== false);
        sel.innerHTML = '<option value="">All servers</option>';
        for (const s of servers) {
            const opt = document.createElement('option');
            opt.value = s.id;
            opt.textContent = `${s.name} (${(s.type || '').toUpperCase()})`;
            if (currentServerId && currentServerId === s.id) opt.selected = true;
            sel.appendChild(opt);
        }
    } catch (e) {
        console.warn('Could not load servers for schedule picker:', e);
    }
}

function toggleJobFiles(jobId) {
    const detailRow = document.getElementById('job-detail-' + jobId);
    const btn = document.getElementById('job-files-toggle-' + jobId);
    if (!detailRow || !btn) return;
    const isExpanded = !detailRow.classList.contains('d-none');
    detailRow.classList.toggle('d-none');
    detailRow.setAttribute('aria-hidden', isExpanded ? 'true' : 'false');
    if (isExpanded) {
        expandedJobFileRows.delete(jobId);
    } else {
        expandedJobFileRows.add(jobId);
    }
    const icon = btn.querySelector('i');
    if (icon) {
        icon.classList.toggle('bi-chevron-down', isExpanded);
        icon.classList.toggle('bi-chevron-up', !isExpanded);
    }
    btn.setAttribute('aria-expanded', isExpanded ? 'false' : 'true');
}

function toggleActiveJobFiles(jobId) {
    const filesDiv = document.getElementById('active-job-files-' + jobId);
    const btn = filesDiv ? filesDiv.previousElementSibling : null;
    if (!filesDiv) return;
    const isExpanded = !filesDiv.classList.contains('d-none');
    filesDiv.classList.toggle('d-none');
    if (isExpanded) {
        expandedActiveJobFiles.delete(jobId);
    } else {
        expandedActiveJobFiles.add(jobId);
    }
    if (btn) {
        const icon = btn.querySelector('i');
        if (icon) {
            icon.classList.toggle('bi-chevron-down', isExpanded);
            icon.classList.toggle('bi-chevron-up', !isExpanded);
        }
    }
}

const PRIORITY_LABELS = {1: 'High', 2: 'Normal', 3: 'Low'};
const PRIORITY_BADGE_CLASS = {1: 'bg-danger', 2: 'bg-primary', 3: 'bg-secondary'};
const PRIORITY_DOT_CLASS = {1: 'priority-dot-high', 2: 'priority-dot-normal', 3: 'priority-dot-low'};

function renderPriorityCell(job) {
    const pri = job.priority || 2;
    const label = PRIORITY_LABELS[pri] || 'Normal';
    const badgeClass = PRIORITY_BADGE_CLASS[pri] || 'bg-primary';
    const isActive = job.status === 'running' || job.status === 'pending';
    if (!isActive) {
        return `<span class="badge ${badgeClass} priority-badge">${label}</span>`;
    }
    const items = [1, 2, 3].map(function (p) {
        const active = p === pri ? ' active' : '';
        const dot = `<span class="priority-dot ${PRIORITY_DOT_CLASS[p]}"></span>`;
        return `<li><a class="dropdown-item${active}" href="#" onclick="setJobPriority('${escapeHtml(job.id)}', ${p}); return false;">${dot}${PRIORITY_LABELS[p]}</a></li>`;
    }).join('');
    return `<div class="dropdown d-inline-block">
        <button class="badge ${badgeClass} border-0 dropdown-toggle priority-btn" type="button" data-bs-toggle="dropdown" aria-expanded="false" style="cursor:pointer;">${label}</button>
        <ul class="dropdown-menu">${items}</ul>
    </div>`;
}

async function setJobPriority(jobId, priority) {
    // Update in-memory array so deferred table rebuilds stay consistent
    const jobObj = jobs.find(function (j) { return j.id === jobId; });
    const oldPriority = jobObj ? jobObj.priority : null;
    if (jobObj) jobObj.priority = priority;

    // Optimistic DOM update for immediate visual feedback
    const row = document.getElementById('job-row-' + jobId);
    if (row) {
        const btn = row.querySelector('.priority-btn');
        if (btn) {
            for (const cls of Object.values(PRIORITY_BADGE_CLASS)) btn.classList.remove(cls);
            btn.classList.add(PRIORITY_BADGE_CLASS[priority] || 'bg-primary');
            btn.textContent = PRIORITY_LABELS[priority] || 'Normal';
        }
    }

    try {
        await apiPost('/api/jobs/' + jobId + '/priority', {priority: priority});
    } catch (err) {
        if (jobObj && oldPriority !== null) jobObj.priority = oldPriority;
        loadJobs();
        showToast('Error', 'Failed to update priority', 'danger');
    }
}

// Render a small per-server badge for jobs/schedules tables. Returns an
// empty string when the row has no server attribution (back-compat with
// jobs created before the multi-server transition).
function _serverBadge(item) {
    const stype = (item && (item.server_type || (item.server && item.server.type) || '')).toLowerCase();
    const sname = item && (item.server_name || (item.server && item.server.name) || '');
    if (stype || sname) {
        const palette = { plex: 'bg-warning text-dark', emby: 'bg-success', jellyfin: 'bg-info text-dark' };
        const cls = palette[stype] || 'bg-secondary';
        const label = sname || stype.toUpperCase() || 'Server';
        const tooltip = stype ? `${stype.toUpperCase()}` : '';
        // Prepend a tiny vendor logo when we have a known type (12px, fits a badge);
        // colour palette stays as the colour-blind-friendly fallback.
        const logoTag = _vendorLogo(stype, 12);
        const logo = logoTag ? logoTag.replace('margin-right: 4px;', 'margin-right: 3px; vertical-align: -2px;') : '';
        return ` <span class="badge ${cls} ms-1" title="${escapeHtmlAttr(tooltip)}">${logo}${escapeHtmlText(label)}</span>`;
    }
    // Fallback for non-server-pinned jobs: surface the trigger source (Sonarr,
    // Radarr, manual scan, schedule etc.) so the user can tell "what server it
    // ran on" / "where the work came from" — D2. Without this, every webhook
    // job and every "All Servers" scan was an unlabelled row.
    const cfg = (item && item.config) || {};
    const src = String(cfg.source || '').trim().toLowerCase();
    const triggerPalette = {
        radarr:      { cls: 'bg-warning text-dark', label: 'Radarr' },
        sonarr:      { cls: 'bg-info text-dark',    label: 'Sonarr' },
        sportarr:    { cls: 'bg-info text-dark',    label: 'Sportarr' },
        tdarr:       { cls: 'bg-secondary',         label: 'Tdarr' },
        plex:        { cls: 'bg-warning text-dark', label: 'Plex Direct' },
        emby:        { cls: 'bg-success',           label: 'Emby Webhook' },
        jellyfin:    { cls: 'bg-info text-dark',    label: 'Jellyfin Webhook' },
        custom:      { cls: 'bg-secondary',         label: 'Custom Webhook' },
        scheduled:   { cls: 'bg-secondary',         label: 'Scheduled' },
        recently_added: { cls: 'bg-secondary',      label: 'Recently Added' },
    };
    if (src && triggerPalette[src]) {
        const t = triggerPalette[src];
        return ` <span class="badge ${t.cls} ms-1" title="Triggered by ${escapeHtmlAttr(t.label)}">${escapeHtmlText(t.label)}</span>`;
    }
    if (src) {
        // Unknown source — render as-is so the user still sees something
        return ` <span class="badge bg-secondary ms-1" title="Trigger: ${escapeHtmlAttr(src)}">${escapeHtmlText(src)}</span>`;
    }
    return '';
}

// D14 — single source of truth for status chip label + color, shared
// across every UI surface that renders an outcome:
//   * file-outcome chips in the Files panel  (job_modal.js → STATUS_META)
//   * file-outcome summary buttons           (job_modal.js → STATUS_META)
//   * per-server aggregate badges            (this file → _renderPublishersBlock)
//   * per-server pills inside file rows      (job_modal.js → _renderFileServerPills)
//
// Both ProcessingResult AND PublisherStatus / MultiServerStatus enums
// resolve through this map so that semantically-equivalent statuses
// (e.g. file `skipped_bif_exists` ≡ publisher `skipped_output_exists`)
// always render the same label and color. Equivalences below.
const STATUS_META = {
    // Success — file generated this run OR successfully published to a server.
    generated:              { label: 'Generated',     cls: 'bg-success', tip: 'Preview was generated' },
    published:              { label: 'Generated',     cls: 'bg-success', tip: 'Preview was published to this server' },

    // Output already on disk; source unchanged — nothing to redo.
    skipped_bif_exists:     { label: 'Already Existed', cls: 'bg-info text-dark', tip: 'Output already on disk and source unchanged' },
    skipped_output_exists:  { label: 'Already Existed', cls: 'bg-info text-dark', tip: 'Output already on disk on this server and source unchanged' },
    skipped:                { label: 'Already Existed', cls: 'bg-info text-dark', tip: 'Output already on disk' },

    // Media server knows the file exists but hasn't completed its analysis
    // pass yet (the bundle hash we need to write the BIF doesn't exist yet).
    // Retry queue will try again on slow backoff.
    skipped_not_indexed:    { label: 'Not Scanned Yet', cls: 'bg-warning text-dark', tip: 'Media server hasn\'t finished scanning / analysing this file yet — will retry' },
    not_indexed:            { label: 'Not Scanned Yet', cls: 'bg-warning text-dark', tip: 'Media server hasn\'t finished scanning / analysing this file yet — will retry' },
    // Media server doesn't know about this file at all (path isn't in any
    // of its libraries, OR it just hasn't been picked up by a scan yet).
    // We've nudged a scan and the retry queue will try again.
    skipped_not_in_library: { label: 'Not In Library', cls: 'bg-warning text-dark', tip: 'This server doesn\'t know about this file — nudged a scan and will retry. If it never appears, the file is outside every library root configured on this server.' },

    // Hard failures.
    failed:                 { label: 'Failed',        cls: 'bg-danger', tip: 'Processing failed' },
    no_frames:              { label: 'No Frames',     cls: 'bg-danger', tip: 'FFmpeg produced no frames (file may be unreadable)' },

    // No server owns this file path.
    no_media_parts:         { label: 'No Server Owner', cls: 'bg-light text-dark border', tip: 'No configured server claims this file path' },
    no_owners:              { label: 'No Server Owner', cls: 'bg-light text-dark border', tip: 'No configured server claims this file path' },

    // Legacy / pipeline-specific outcomes.
    skipped_file_not_found: { label: 'Not Found',     cls: 'bg-warning text-dark', tip: 'File not found on disk' },
    skipped_excluded:       { label: 'Excluded',      cls: 'bg-secondary', tip: 'Path matched an exclusion rule' },
    skipped_invalid_hash:   { label: 'Invalid Hash',  cls: 'bg-warning text-dark', tip: 'Could not compute the path hash' },
    unresolved_plex:        { label: 'Not In Plex',   cls: 'bg-danger', tip: 'Could not find this item in Plex after lookup' },
};
window.STATUS_META = STATUS_META;

function _statusMeta(key) {
    return STATUS_META[key] || { label: key || '?', cls: 'bg-secondary', tip: '' };
}
window._statusMeta = _statusMeta;

// Back-compat alias retained while older call sites still reference the
// old name. New code should call _statusMeta() directly.
const _PUBLISHER_STATUS_BADGES = STATUS_META;

// Frame-provenance badges so users can see when one webhook's frames
// were reused across a sibling-server publish (no second FFmpeg) vs
// when this publisher's output was already on disk vs when FFmpeg
// just ran for this dispatch.
const _FRAME_SOURCE_BADGES = {
    cache_hit:      { label: 'Frames reused', cls: 'bg-info text-dark', tip: 'Frames came from the cache — FFmpeg did not run for this dispatch' },
    output_existed: { label: 'Already on disk', cls: 'bg-light text-dark border', tip: 'Output was already on disk and unchanged; nothing to re-publish' },
    extracted:      null, // no badge for "extracted" — it's the boring default
};

function _renderPublishersBlock(job) {
    // D12 — per-server aggregate (one row per registered server with
    // status counts), NOT per-file. Per-file × per-server attribution
    // lives in the Files panel; rendering it here on jobs with hundreds
    // of files would stack hundreds of rows in the Active Jobs and
    // History sections. Each entry: {server_id, server_name, server_type,
    // counts: {published: N, failed: M, ...}}.
    const rows = (job && Array.isArray(job.publishers)) ? job.publishers : [];
    if (!rows.length) return '';
    const lines = rows.map(function (entry) {
        const stype = (entry.server_type || '').toLowerCase();
        const logo = _vendorLogo(stype, 12) || '';
        const sname = entry.server_name || stype.toUpperCase() || 'Server';
        const counts = (entry && typeof entry.counts === 'object' && entry.counts) ? entry.counts : {};
        const statusOrder = ['published', 'skipped_output_exists', 'skipped_not_indexed', 'not_indexed', 'skipped_not_in_library', 'skipped', 'no_owners', 'no_frames', 'failed'];
        const seen = new Set();
        const ordered = statusOrder.filter(function (k) { seen.add(k); return counts[k] > 0; })
            .concat(Object.keys(counts).filter(function (k) { return !seen.has(k) && counts[k] > 0; }));
        if (!ordered.length) return '';
        const badges = ordered.map(function (status) {
            const meta = _statusMeta(status);
            const tip = meta.tip ? ` title="${escapeHtmlAttr(meta.tip)}"` : '';
            return `<span class="badge ${meta.cls}"${tip}>${escapeHtmlText(meta.label)} × ${counts[status]}</span>`;
        }).join(' ');
        // Stack server-name pill, an arrow separator, and the per-status
        // badges with explicit gap-2 spacing so the visual hierarchy is
        // unambiguous. Without this, "Plex" + "1 not indexed yet" run
        // together visually and users misread it as "Plex1 not indexed yet"
        // (and the SVG alt-text used to compound the confusion — see
        // _vendorLogo for the alt="" aria-hidden fix).
        return (
            `<div class="mt-1 d-flex flex-wrap align-items-center gap-2">` +
            `<span class="badge bg-light text-dark border">${logo}${escapeHtmlText(sname)}</span>` +
            `<span class="text-muted small" aria-hidden="true">→</span>` +
            badges +
            `</div>`
        );
    }).filter(Boolean).join('');
    if (!lines) return '';
    return `<div class="mt-3 pt-2 border-top"><strong class="me-2">Servers:</strong>${lines}</div>`;
}

let _jobQueueUpdatePending = false;

function updateJobQueue() {
    const tbody = document.getElementById('jobQueue');
    // The Job Queue table only exists on the dashboard. SocketIO connect/job
    // events fire on every page, so bail when the target DOM is absent —
    // otherwise reconnect from /settings or /servers crashes with
    // "Cannot set properties of null (setting 'innerHTML')".
    if (!tbody) {
        return;
    }

    // Defer rebuild while a priority dropdown is open to avoid destroying it
    if (tbody.querySelector('.dropdown-menu.show')) {
        _jobQueueUpdatePending = true;
        return;
    }
    // Defer while the user is hovering inside the table — a wholesale
    // tbody.innerHTML rebuild mid-hover destroys the button under the
    // cursor and the click never lands. Symptom from the field: the
    // red Cancel-job X "flashes red" on hover but never actually fires
    // the cancel because the row gets re-rendered between mousedown
    // and mouseup. The :hover check is a Bootstrap pattern (CSS
    // pseudo-class is queryable via :is(...:hover) on tbody.matches);
    // we use a `querySelector(':hover')` that walks any descendant
    // currently hovered. Safe because we'll rebuild on the next tick.
    if (tbody.matches(':hover') || tbody.querySelector(':hover')) {
        _jobQueueUpdatePending = true;
        return;
    }
    _jobQueueUpdatePending = false;

    if (jobs.length === 0) {
        if (jobTotal === 0) {
            tbody.innerHTML = `
                <tr>
                    <td colspan="7" class="text-center text-muted py-5">
                        <i class="bi bi-inbox fs-2 d-block mb-2 opacity-50"></i>
                        <div>Nothing queued.</div>
                        <div class="small">New jobs will show up here once you start one.</div>
                    </td>
                </tr>
            `;
        } else {
            tbody.innerHTML = `
                <tr>
                    <td colspan="7" class="text-center text-muted py-4">
                        No jobs on this page
                    </td>
                </tr>
            `;
        }
        return;
    }

    let html = '';

    const activeJobIds = new Set(jobs.map((job) => String(job.id)));
    for (const expandedId of Array.from(expandedJobFileRows)) {
        if (!activeJobIds.has(expandedId)) {
            expandedJobFileRows.delete(expandedId);
        }
    }

    for (const job of jobs) {
        const statusBadge = getStatusBadge(job.status, job.paused, job.error, job.progress && job.progress.outcome);
        const progress = job.progress.percent.toFixed(1);
        const created = formatRelativeTime(job.created_at);
        let actionButtons = '';

        // Use a btn-group so spacing is consistent between adjacent
        // icon buttons and the row's right edge — previously buttons
        // had ad-hoc me-1 margins that produced different gaps depending
        // on which set was rendered.
        if (job.status === 'running' || job.status === 'pending') {
            actionButtons = `<div class="btn-group btn-group-sm icon-btn-group" role="group">
                <button class="btn btn-outline-danger" onclick="cancelJob('${escapeHtml(job.id)}')" title="Cancel" aria-label="Cancel job">
                    <i class="bi bi-x-lg"></i>
                </button>
            </div>`;
        } else {
            actionButtons = `<div class="btn-group btn-group-sm icon-btn-group" role="group">
                <button class="btn btn-outline-secondary" onclick="showLogsModal('${escapeHtml(job.id)}')" title="View logs" aria-label="View logs">
                    <i class="bi bi-file-text"></i>
                </button>
                <button class="btn btn-outline-secondary" onclick="reprocessJob('${escapeHtml(job.id)}')" title="Re-run" aria-label="Re-run job">
                    <i class="bi bi-arrow-repeat"></i>
                </button>
                <button class="btn btn-outline-danger" onclick="deleteJob('${escapeHtml(job.id)}')" title="Delete" aria-label="Delete job">
                    <i class="bi bi-trash"></i>
                </button>
            </div>`;
        }

        let webhookBasenames = job.config && Array.isArray(job.config.webhook_basenames) && job.config.webhook_basenames.length > 0
            ? job.config.webhook_basenames
            : [];
        if (webhookBasenames.length === 0 && job.config && Array.isArray(job.config.webhook_paths) && job.config.webhook_paths.length > 0) {
            webhookBasenames = job.config.webhook_paths.map(function (p) { return p.split('/').pop() || p; });
        }
        const hasMultiFile = webhookBasenames.length > 1;
        // Phase H5: also show the toggle when publisher rows exist, so single-file
        // jobs surface their per-server publish breakdown.
        const hasPublishers = Array.isArray(job.publishers) && job.publishers.length > 0;
        const hasExpandableDetail = hasMultiFile || hasPublishers;
        const isFilesExpanded = expandedJobFileRows.has(String(job.id));
        const libraryTitle = webhookBasenames.length > 0
            ? ` title="${escapeHtml(webhookBasenames.join(', '))}"`
            : '';
        const toggleTitle = hasMultiFile ? 'Show files' : 'Show publishers';
        const filesToggleBtn = hasExpandableDetail
            ? ` <button type="button" class="btn btn-sm btn-link p-0 ms-1 align-baseline" id="job-files-toggle-${escapeHtml(job.id)}"
                        onclick="toggleJobFiles('${escapeHtml(job.id)}')" aria-expanded="${isFilesExpanded ? 'true' : 'false'}" aria-controls="job-detail-${escapeHtml(job.id)}" title="${toggleTitle}">
                   <i class="bi ${isFilesExpanded ? 'bi-chevron-up' : 'bi-chevron-down'}"></i>
                 </button>`
            : '';
        const isRetry = !!(job.config && job.config.is_retry);
        const retryAttempt = job.config && typeof job.config.retry_attempt === 'number' ? job.config.retry_attempt : 0;
        const maxRetries = job.config && typeof job.config.max_retries === 'number' ? job.config.max_retries : 0;
        const retryLabel = isRetry && maxRetries > 0
            ? ` <span class="badge bg-secondary ms-1" title="Retry job">Retry ${retryAttempt}/${maxRetries}</span>`
            : '';
        const priorityCell = renderPriorityCell(job);
        const scheduledAt = job.config && job.config.scheduled_at;
        const isWaitingRetry = job.status === 'pending' && isRetry && scheduledAt;
        let progressCell;
        if (isWaitingRetry) {
            const remaining = Math.max(0, Math.ceil((new Date(scheduledAt).getTime() - Date.now()) / 1000));
            const label = remaining > 0 ? `Retry starting in ${remaining}s` : 'Starting...';
            progressCell = `<span class="text-warning small" data-scheduled-at="${escapeHtml(scheduledAt)}"><i class="bi bi-hourglass-split me-1"></i>${label}</span>`;
        } else {
            // Color the bar by status — blue (primary) is reserved for
            // running. Completed/failed/cancelled get the matching outcome
            // colour so the bar reinforces the status pill rather than
            // contradicting it (a 100% blue bar next to a green
            // "Completed" pill was confusing the eye).
            const barClass = ({
                completed: 'bg-success',
                failed: 'bg-danger',
                cancelled: 'bg-secondary',
                running: 'progress-bar-striped progress-bar-animated',
                pending: 'bg-secondary',
            })[job.status] || '';
            progressCell = `<div class="progress" data-status="${escapeHtml(job.status)}" style="height: 20px;">
                        <div class="progress-bar ${barClass}" role="progressbar"
                             style="width: ${progress}%">${progress}%</div>
                    </div>`;
        }
        html += `
            <tr id="job-row-${escapeHtml(job.id)}" class="job-row">
                <td class="d-none d-lg-table-cell text-muted small font-monospace align-middle"><code class="bg-transparent p-0">${escapeHtml(job.id.substring(0, 8))}</code></td>
                <td class="align-middle"${libraryTitle}>
                    <div class="d-flex align-items-center flex-wrap gap-2">
                        <span class="fw-medium">${escapeHtml(job.library_name) || 'All Libraries'}</span>
                        ${_serverBadge(job)}${retryLabel}${filesToggleBtn}
                    </div>
                </td>
                <td class="align-middle">${statusBadge}</td>
                <td class="align-middle d-none d-md-table-cell">${priorityCell}</td>
                <td class="align-middle">${progressCell}</td>
                <td class="align-middle d-none d-lg-table-cell text-muted small">${created}</td>
                <td class="align-middle text-end text-nowrap">
                    ${actionButtons}
                </td>
            </tr>
        `;
        if (hasExpandableDetail) {
            const filesList = hasMultiFile
                ? webhookBasenames.map(function (b) { return `<div class="text-muted">${escapeHtml(b)}</div>`; }).join('')
                : '';
            const overflow = hasMultiFile && job.config.path_count > webhookBasenames.length
                ? `<div class="text-muted mt-1">(+${job.config.path_count - webhookBasenames.length} more)</div>`
                : '';
            const filesBlock = hasMultiFile
                ? `<strong>Files:</strong><div class="mt-1">${filesList}${overflow}</div>`
                : '';
            // Phase H5: per-server publisher block. Empty for legacy jobs.
            const publishersBlock = _renderPublishersBlock(job);
            html += `
            <tr id="job-detail-${escapeHtml(job.id)}" class="${isFilesExpanded ? '' : 'd-none'} job-files-detail" aria-hidden="${isFilesExpanded ? 'false' : 'true'}">
                <td colspan="7" class="bg-body-tertiary small py-2 ps-4">
                    ${filesBlock}
                    ${publishersBlock}
                </td>
            </tr>
            `;
        }
    }

    tbody.innerHTML = html;

    // Initialize Bootstrap tooltips on status badges
    tbody.querySelectorAll('[data-bs-toggle="tooltip"]').forEach(function (el) {
        new bootstrap.Tooltip(el);
    });

    if (document.querySelector('[data-scheduled-at]')) {
        _ensureElapsedTimer();
    }
}

function renderJobPagination() {
    const footer = document.getElementById('jobPaginationFooter');
    const info = document.getElementById('jobPaginationInfo');
    const controls = document.getElementById('jobPaginationControls');
    const perPageSelect = document.getElementById('jobPerPageSelect');

    if (!footer) return;

    if (jobTotal === 0) {
        footer.classList.add('d-none');
        return;
    }
    footer.classList.remove('d-none');

    perPageSelect.value = String(jobPerPage);

    const start = (jobPage - 1) * jobPerPage + 1;
    const end = Math.min(jobPage * jobPerPage, jobTotal);
    info.textContent = `Showing ${start}\u2013${end} of ${jobTotal}`;

    let pagesHtml = '';
    pagesHtml += `<li class="page-item ${jobPage <= 1 ? 'disabled' : ''}">
        <a class="page-link" href="#" onclick="goToJobPage(${jobPage - 1}); return false;" aria-label="Previous">&lsaquo;</a></li>`;

    const maxVisible = 5;
    let rangeStart = Math.max(1, jobPage - Math.floor(maxVisible / 2));
    let rangeEnd = Math.min(jobTotalPages, rangeStart + maxVisible - 1);
    if (rangeEnd - rangeStart + 1 < maxVisible) {
        rangeStart = Math.max(1, rangeEnd - maxVisible + 1);
    }

    if (rangeStart > 1) {
        pagesHtml += `<li class="page-item"><a class="page-link" href="#" onclick="goToJobPage(1); return false;">1</a></li>`;
        if (rangeStart > 2) {
            pagesHtml += `<li class="page-item disabled"><span class="page-link">&hellip;</span></li>`;
        }
    }

    for (let p = rangeStart; p <= rangeEnd; p++) {
        pagesHtml += `<li class="page-item ${p === jobPage ? 'active' : ''}">
            <a class="page-link" href="#" onclick="goToJobPage(${p}); return false;">${p}</a></li>`;
    }

    if (rangeEnd < jobTotalPages) {
        if (rangeEnd < jobTotalPages - 1) {
            pagesHtml += `<li class="page-item disabled"><span class="page-link">&hellip;</span></li>`;
        }
        pagesHtml += `<li class="page-item"><a class="page-link" href="#" onclick="goToJobPage(${jobTotalPages}); return false;">${jobTotalPages}</a></li>`;
    }

    pagesHtml += `<li class="page-item ${jobPage >= jobTotalPages ? 'disabled' : ''}">
        <a class="page-link" href="#" onclick="goToJobPage(${jobPage + 1}); return false;" aria-label="Next">&rsaquo;</a></li>`;

    controls.innerHTML = pagesHtml;
}

function goToJobPage(page) {
    if (page < 1 || page > jobTotalPages) return;
    jobPage = page;
    loadJobs();
}

function changeJobPerPage(value) {
    jobPerPage = parseInt(value, 10) || 50;
    jobPage = 1;
    localStorage.setItem('jobPerPage', String(jobPerPage));
    loadJobs();
}

function updateActiveJobs(runningJobs) {
    const container = document.getElementById('activeJobsContainer');
    const countBadge = document.getElementById('activeJobsCount');

    // Same defer-on-hover guard as updateJobQueue: the wholesale
    // ``container.innerHTML = html`` rebuild every poll destroys the
    // Cancel-job button mid-hover, making the icon flicker (loses
    // hover state for ~1 frame) and — when the rebuild lands between
    // mousedown and mouseup — ate the click entirely. The hover
    // check defers the rebuild until the cursor moves out; pending
    // updates land on the next tick.
    if (container && (container.matches(':hover') || container.querySelector(':hover'))) {
        return;
    }

    if (!runningJobs || runningJobs.length === 0) {
        countBadge.textContent = 'Idle';
        countBadge.className = 'badge bg-secondary';
        // Compact one-line empty state — the previous big-icon block was
        // ~200px of vertical space spent saying nothing. Match the
        // template's static empty-state markup so first paint and the
        // post-load JS render are visually identical.
        container.innerHTML = `
            <div class="text-muted small d-flex align-items-center justify-content-center gap-2 py-1">
                <i class="bi bi-inbox"></i>
                <span>Idle &mdash; no jobs in flight.</span>
            </div>
        `;
        _stopElapsedTimer();
        refreshWorkerScaleButtons();
        return;
    }

    countBadge.textContent = `${runningJobs.length} running`;
    countBadge.className = 'badge bg-primary pulse';

    let html = '';
    for (const job of runningJobs) {
        const jid = escapeHtml(job.id);
        const isPaused = !!job.paused;
        const statusBadge = isPaused
            ? '<span class="badge bg-warning text-dark">Paused</span>'
            : '<span class="badge bg-primary pulse">Running</span>';
        const progress = job.progress.percent.toFixed(1);

        // Build collapsible file list (matching Job Queue pattern)
        let webhookFilesHtml = '';
        let webhookFiles = job.config && Array.isArray(job.config.webhook_basenames) && job.config.webhook_basenames.length > 0
            ? job.config.webhook_basenames
            : null;
        if (!webhookFiles && job.config && Array.isArray(job.config.webhook_paths) && job.config.webhook_paths.length > 0) {
            webhookFiles = job.config.webhook_paths.map(function (p) { return p.split('/').pop() || p; });
        }
        if (webhookFiles && webhookFiles.length > 1) {
            const filesId = `active-job-files-${jid}`;
            const isExpanded = expandedActiveJobFiles.has(String(job.id));
            const pathCount = (job.config && typeof job.config.path_count === 'number') ? job.config.path_count : webhookFiles.length;
            const filesList = webhookFiles.map(function (b) { return `<div class="text-muted">${escapeHtml(b)}</div>`; }).join('');
            const overflow = pathCount > webhookFiles.length
                ? `<div class="text-muted mt-1">(+${pathCount - webhookFiles.length} more)</div>`
                : '';
            webhookFilesHtml = `
                <div class="mt-1 small">
                    <strong>Files:</strong> ${pathCount} file(s)
                    <button type="button" class="btn btn-sm btn-link p-0 ms-1 align-baseline" onclick="toggleActiveJobFiles('${jid}')"
                            aria-expanded="${isExpanded}" title="Show files">
                        <i class="bi ${isExpanded ? 'bi-chevron-up' : 'bi-chevron-down'}"></i>
                    </button>
                    <div id="${filesId}" class="${isExpanded ? '' : 'd-none'} mt-1 ms-3">${filesList}${overflow}</div>
                </div>`;
        }
        // Single-file jobs intentionally omit the "File:" line here — the
        // library_name already contains the filename (e.g.
        // "Manual: Foo.mkv", "Sonarr: Show S01E01") so showing it twice
        // is redundant noise. Multi-file jobs keep the expandable list.

        // Start time and elapsed
        const startedLine = job.started_at
            ? `<span class="text-muted small"><i class="bi bi-clock me-1"></i>Started ${formatDate(job.started_at)} (<span data-elapsed-since="${escapeHtml(job.started_at)}">${formatElapsed(job.started_at)}</span>)</span>`
            : '';

        const activePri = job.priority || 2;
        const activePriBadge = `<span class="badge ${PRIORITY_BADGE_CLASS[activePri] || 'bg-primary'} priority-badge ms-1">${PRIORITY_LABELS[activePri] || 'Normal'}</span>`;

        html += `
        <div class="active-job-card mb-3 p-3 border rounded" id="active-job-${jid}">
            <div class="d-flex justify-content-between align-items-start mb-2 gap-2 flex-wrap">
                <div class="d-flex align-items-center flex-wrap gap-2 min-w-0">
                    <span><strong>Job:</strong> <code>${jid.substring(0, 8)}</code></span>
                    ${statusBadge}${activePriBadge}
                </div>
                <div class="btn-group btn-group-sm icon-btn-group flex-shrink-0" role="group">
                    <button class="btn btn-outline-info" onclick="showLogsModal('${jid}')" title="View Logs" aria-label="View logs">
                        <i class="bi bi-file-text"></i>
                    </button>
                    <button class="btn btn-outline-danger" onclick="cancelJob('${jid}')" title="Cancel job" aria-label="Cancel job">
                        <i class="bi bi-x-lg"></i>
                    </button>
                </div>
            </div>
            <div class="mb-2 small">
                <strong>Library:</strong> ${escapeHtml(job.library_name) || 'All Libraries'}${_serverBadge(job)}${webhookFilesHtml}
            </div>
            ${_renderPublishersBlock(job)}
            ${startedLine ? `<div class="mb-2">${startedLine}</div>` : ''}
            <div class="progress" style="height: 24px;">
                <div class="progress-bar progress-bar-striped progress-bar-animated"
                     role="progressbar" style="width: ${progress}%" id="activeJobProgress-${jid}">
                    ${progress}%
                </div>
            </div>
            <div class="d-flex justify-content-between mt-1 small text-muted">
                <span id="activeJobItem-${jid}">${escapeHtml(job.progress.current_item) || 'Starting...'}</span>
                <span id="activeJobItems-${jid}">Items: ${job.progress.processed_items || 0} / ${job.progress.total_items || '?'}</span>
            </div>
        </div>`;
    }

    container.innerHTML = html;
    _ensureElapsedTimer();
    refreshWorkerScaleButtons();
}

function removeActiveJob(jobId) {
    const el = document.getElementById('active-job-' + jobId);
    if (el) el.remove();

    const container = document.getElementById('activeJobsContainer');
    if (container && container.querySelectorAll('.active-job-card').length === 0) {
        updateActiveJobs([]);
    }
}

// Cache latest progress per job so events arriving before the active-job
// DOM card is created can be replayed once loadJobs() renders it.
const _pendingProgress = {};

function updateJobProgress(jobId, progress) {
    const progressBar = document.getElementById('activeJobProgress-' + jobId);
    if (!progressBar) {
        // DOM not ready yet — cache for replay after next loadJobs().
        _pendingProgress[jobId] = progress;
        return;
    }
    // DOM is ready — clear any pending cache for this job.
    delete _pendingProgress[jobId];

    const percent = progress.percent.toFixed(1);
    progressBar.style.width = `${percent}%`;
    progressBar.textContent = `${percent}%`;

    const itemEl = document.getElementById('activeJobItem-' + jobId);
    if (itemEl && progress.current_item) {
        itemEl.textContent = progress.current_item;
    }

    const itemsEl = document.getElementById('activeJobItems-' + jobId);
    if (itemsEl) {
        itemsEl.textContent = `Items: ${progress.processed_items || 0} / ${progress.total_items || '?'}`;
    }

    const row = document.getElementById(`job-row-${jobId}`);
    if (row) {
        const queueBar = row.querySelector('.progress-bar');
        if (queueBar) {
            queueBar.style.width = `${percent}%`;
            queueBar.textContent = `${percent}%`;
        }
    }
}

// Worker Status Functions

// In-place worker card updates. Previously this function rebuilt the
// entire #workerStatusContainer via innerHTML on every poll, which (a)
// flickered the panel even when nothing changed and (b) the idle vs
// processing branches rendered DIFFERENT child markup so the card
// HEIGHT changed every time a worker flipped state — visible as the
// whole panel shifting up/down 30+ pixels per second on a busy job.
//
// Fix:
//   1. Render each card with the SAME DOM shape regardless of status
//      (title row + progress bar + footer line). Idle simply puts an
//      em-dash in the title and keeps the rest at zero — the row
//      height never changes.
//   2. Update text/class in place against a per-(type,id) cached card
//      so successive polls don't blow away DOM nodes the user might
//      be hovering / selecting.
//   3. Workers that disappear from the snapshot are removed by id;
//      new ones are appended. The common case (4 stable rows) is a
//      pure text/class diff on existing nodes.
function updateWorkerStatuses(workers, options = {}) {
    const {
        fallbackCounts = null,
        keepBadgeCounts = false
    } = options;
    const container = document.getElementById('workerStatusContainer');
    // Bail when the dashboard's worker container isn't on the current page
    // — otherwise SocketIO reconnect from /settings et al. crashes here.
    if (!container) {
        return;
    }
    const cpuWorkersEl = document.getElementById('cpuWorkers');

    if (!workers || workers.length === 0) {
        container.innerHTML = `
            <div class="text-muted text-center py-3">
                <span>No active workers</span>
            </div>
        `;
        if (keepBadgeCounts) {
            return;
        }
        const counts = fallbackCounts || cachedWorkerConfigCounts;
        if (counts) {
            if (cpuWorkersEl) cpuWorkersEl.textContent = String(counts.cpu_threads);
        }
        refreshWorkerScaleButtons();
        return;
    }

    const cpuCount = workers.filter(w => w.worker_type === 'CPU').length;
    if (cpuWorkersEl) cpuWorkersEl.textContent = String(cpuCount);

    // Workers panel header count badge — "N active / M slots". Gives the
    // user a glance-able signal of how busy the queue is without forcing
    // them to count rows. Hidden when there's nothing to count.
    const headerBadge = document.getElementById('workersHeaderCount');
    if (headerBadge) {
        const total = workers.length;
        const active = workers.filter(w => w.status === 'processing').length;
        if (total === 0) {
            headerBadge.textContent = '—';
            headerBadge.className = 'badge bg-secondary';
        } else if (active === 0) {
            headerBadge.textContent = `${total} idle`;
            headerBadge.className = 'badge bg-secondary';
        } else {
            headerBadge.textContent = `${active} of ${total} active`;
            headerBadge.className = 'badge bg-primary';
        }
        // Match the all-caps card-header style suppression we set in HTML
        // so the badge text stays case-as-typed.
        headerBadge.style.textTransform = 'none';
        headerBadge.style.letterSpacing = '0';
    }

    // Surface GPU→CPU fallback transitions as a warning toast (once per switch).
    for (const w of workers) {
        const prev = _fallbackStateByWorker.get(w.worker_id) || false;
        const now = !!w.fallback_active;
        if (now && !prev) {
            const title = w.current_title || 'this file';
            const reason = w.fallback_reason || 'GPU processing failed';
            showToast(
                'Switched to CPU',
                `${w.worker_name} fell back to CPU for "${title}" — ${reason}`,
                'warning'
            );
        }
        _fallbackStateByWorker.set(w.worker_id, now);
    }

    // Ensure the row container exists; build it once on the first call.
    let row = container.querySelector(':scope > .row.g-3');
    if (!row) {
        container.innerHTML = '<div class="row g-3"></div>';
        row = container.querySelector(':scope > .row.g-3');
    }

    const seenKeys = new Set();
    for (const worker of workers) {
        const key = `${worker.worker_type}_${worker.worker_id}`;
        seenKeys.add(key);
        let col = row.querySelector(`:scope > [data-worker-key="${CSS.escape(key)}"]`);
        if (!col) {
            // First sighting of this slot — stamp the static DOM shape
            // once. From here on we only mutate text/class on existing
            // nodes, so there's no flicker.
            col = document.createElement('div');
            col.className = 'col-md-6';
            col.dataset.workerKey = key;
            col.innerHTML = `
                <div class="card bg-body-tertiary workers-panel-card" data-card data-status="idle">
                    <div class="card-body py-2">
                        <div class="d-flex justify-content-between align-items-center mb-2">
                            <span class="text-truncate" data-name-wrap>
                                <i class="bi" data-icon></i>
                                <span data-name></span>
                                <span class="badge bg-warning text-dark ms-1 d-none" data-fallback-badge>
                                    <i class="bi bi-arrow-down-circle me-1"></i>CPU fallback
                                </span>
                            </span>
                            <span class="badge" data-status-badge></span>
                        </div>
                        <div class="small text-warning text-truncate mb-1 d-none" data-fallback-note>
                            <i class="bi bi-exclamation-triangle me-1"></i><span data-fallback-reason></span>
                        </div>
                        <div class="small text-truncate mb-1" data-title></div>
                        <div class="progress" data-progress-wrap style="height: 6px;">
                            <div class="progress-bar" data-progress style="width: 0%"></div>
                        </div>
                        <div class="d-flex justify-content-between small text-muted mt-1" data-metrics>
                            <span data-percent>0.0%</span>
                            <span data-speed>0.0x</span>
                            <span>ETA: <span data-eta>-</span></span>
                        </div>
                    </div>
                </div>
            `;
            row.appendChild(col);
            // Cosmetic: slightly tighter icon spacing.
            col.querySelector('[data-icon]').classList.add('me-2');
        }
        _patchWorkerCard(col, worker);
    }

    // Drop any cards for workers that vanished (a job ending, a pool
    // resize). The legacy code did this implicitly via innerHTML
    // rebuild; here we do it explicitly.
    for (const col of Array.from(row.children)) {
        if (!seenKeys.has(col.dataset.workerKey)) {
            col.remove();
        }
    }
    refreshWorkerScaleButtons();
}

function _patchWorkerCard(col, worker) {
    const fallbackActive = !!worker.fallback_active;
    const isProcessing = worker.status === 'processing';
    const card = col.querySelector('[data-card]');
    const icon = col.querySelector('[data-icon]');
    const nameEl = col.querySelector('[data-name]');
    const fallbackBadge = col.querySelector('[data-fallback-badge]');
    const fallbackNote = col.querySelector('[data-fallback-note]');
    const fallbackReason = col.querySelector('[data-fallback-reason]');
    const statusEl = col.querySelector('[data-status-badge]');
    const titleEl = col.querySelector('[data-title]');
    const progressWrap = col.querySelector('[data-progress-wrap]');
    const progress = col.querySelector('[data-progress]');
    const metrics = col.querySelector('[data-metrics]');
    const percent = col.querySelector('[data-percent]');
    const speed = col.querySelector('[data-speed]');
    const eta = col.querySelector('[data-eta]');

    // Attribute on the card itself so the .workers-panel-card[data-status]
    // CSS rule can flip the row's accent without re-rendering anything.
    if (card.getAttribute('data-status') !== worker.status) {
        card.setAttribute('data-status', worker.status);
    }
    // Hide the progress bar + footer metrics when idle — a wall of
    // "0.0% / 0.0x / ETA: -" rows on an 8-worker setup is just noise.
    // Visibility (not display) keeps the card height pinned so the
    // panel never shifts vertically between idle and processing.
    progressWrap.style.visibility = isProcessing ? 'visible' : 'hidden';
    metrics.style.visibility = isProcessing ? 'visible' : 'hidden';

    // Card border (warning ring on fallback)
    card.classList.toggle('border-warning', fallbackActive);

    // Icon (gpu-card vs cpu, fallback flips to cpu)
    const iconClass = fallbackActive
        ? 'bi-cpu'
        : (worker.worker_type === 'GPU' ? 'bi-gpu-card' : 'bi-cpu');
    if (!icon.classList.contains(iconClass)) {
        icon.className = `bi me-2 ${iconClass}`;
    }

    // Name (only update if changed — avoids tearing during text selection)
    if (nameEl.textContent !== worker.worker_name) {
        nameEl.textContent = worker.worker_name;
    }

    // Fallback badge + note
    fallbackBadge.classList.toggle('d-none', !fallbackActive);
    if (fallbackActive) {
        fallbackBadge.title = worker.fallback_reason || 'CPU fallback';
    }
    const showFallbackNote = fallbackActive && !!worker.fallback_reason;
    fallbackNote.classList.toggle('d-none', !showFallbackNote);
    if (showFallbackNote && fallbackReason.textContent !== worker.fallback_reason) {
        fallbackReason.textContent = worker.fallback_reason;
        fallbackNote.title = worker.fallback_reason;
    }

    // Status badge — colour AND text
    const statusColor = isProcessing ? 'bg-primary' : 'bg-secondary';
    if (!statusEl.classList.contains(statusColor)) {
        statusEl.className = `badge ${statusColor}`;
    }
    if (statusEl.textContent !== worker.status) {
        statusEl.textContent = worker.status;
    }

    // Title row — render the SAME DOM whether idle or processing so
    // the card height never changes. Idle uses an em-dash placeholder
    // (text-muted) instead of a wholly different "Idle - waiting"
    // single-line box that resized the card.
    let titleHTML;
    if (isProcessing) {
        const lib = worker.library_name
            ? `<span class="text-muted">${escapeHtml(worker.library_name)}</span> <i class="bi bi-chevron-right small text-muted"></i> `
            : '';
        titleHTML = `${lib}${escapeHtml(worker.current_title) || 'Processing…'}`;
        titleEl.title = worker.current_title || '';
        titleEl.classList.remove('text-muted');
    } else {
        titleHTML = '<span class="text-muted">— idle</span>';
        titleEl.title = 'Worker is idle';
    }
    if (titleEl.innerHTML !== titleHTML) {
        titleEl.innerHTML = titleHTML;
    }

    // Progress bar — width + colour. Visibility kept (just zero width)
    // when idle so the row height stays the same.
    // ``ffmpeg_started`` distinguishes pre-FFmpeg setup work
    // (resolving item-ids, unpacking a sibling BIF, publishing — all
    // 0% / 0.0x) from FFmpeg-actually-running. When still in the
    // pre-FFmpeg phase we show "Working…" instead of "0.0% / 0.0x"
    // so the user can tell the worker isn't stuck.
    const ffmpegStarted = !!worker.ffmpeg_started;
    const progressPercent = isProcessing ? (worker.progress_percent || 0) : 0;
    const showProgress = isProcessing && ffmpegStarted;
    const desiredWidth = showProgress ? `${progressPercent.toFixed(1)}%` : '0%';
    if (progress.style.width !== desiredWidth) {
        progress.style.width = desiredWidth;
    }
    progress.classList.toggle('bg-warning', fallbackActive);

    // Footer numbers — hide percent/speed/ETA until FFmpeg has reported,
    // showing a neutral "Working…" instead so 0.0% / 0.0x doesn't read
    // as a stuck worker during pre-FFmpeg phases (cross-server BIF
    // reuse, item-id lookups, publishing).
    if (isProcessing && !ffmpegStarted) {
        if (percent.textContent !== 'Working…') percent.textContent = 'Working…';
        if (speed.textContent !== '—') speed.textContent = '—';
        if (eta.textContent !== '-') eta.textContent = '-';
    } else {
        const percentText = `${progressPercent.toFixed(1)}%`;
        if (percent.textContent !== percentText) percent.textContent = percentText;
        const speedText = isProcessing ? (worker.speed || '0.0x') : '—';
        if (speed.textContent !== speedText) speed.textContent = speedText;
        const etaText = isProcessing ? (worker.eta || '-') : '-';
        if (eta.textContent !== etaText) eta.textContent = etaText;
    }
}

async function loadWorkerStatuses() {
    try {
        const data = await apiGet('/api/jobs/workers');
        const workers = data.workers || [];

        if (workers.length === 0) {
            if (!jobsLoadedOnce) {
                // First load — avoid flash of config defaults before jobs are fetched.
                updateWorkerStatuses([], { keepBadgeCounts: true });
                return;
            }
            // No pool exists yet (no job has ever run); show config defaults.
            const fallbackCounts = await loadWorkerConfigCounts(false);
            updateWorkerStatuses([], { fallbackCounts });
            return;
        }

        updateWorkerStatuses(workers);
    } catch (error) {
        console.error('Failed to load worker statuses:', error);
        const container = document.getElementById('workerStatusContainer');
        if (container && !error.message.includes('Authentication')) {
            const fallbackCounts = await loadWorkerConfigCounts(false);
            updateWorkerStatuses([], { fallbackCounts });
        }
    }
}


// Browser Notifications
let notificationsEnabled = false;

async function requestNotificationPermission() {
    if (!('Notification' in window)) return;

    if (Notification.permission === 'granted') {
        notificationsEnabled = true;
    } else if (Notification.permission !== 'denied') {
        const permission = await Notification.requestPermission();
        notificationsEnabled = (permission === 'granted');
    }
}

function showNotification(title, body, type = 'info') {
    if (!notificationsEnabled) return;

    const icon = type === 'success' ? '✅' : type === 'error' ? '❌' : type === 'warning' ? '⚠️' : 'ℹ️';

    new Notification(`${icon} ${title}`, {
        body: body,
        icon: '/static/images/icon.png',
        tag: 'plex-preview-generator'
    });
}

// Action Functions
function showNewJobModal() {
    // Phase H6: server picker → multi-select libraries grouped by server.
    document.getElementById('jobLibraryAll').checked = true;
    const sortByEl = document.getElementById('jobSortBy');
    if (sortByEl) sortByEl.value = '';

    _populateJobServerPicker();
    // Render libraries from the current global cache; refreshes on server change.
    _renderJobLibraryList(libraries, '');

    const modal = new bootstrap.Modal(document.getElementById('newJobModal'));
    modal.show();
}

async function _populateJobServerPicker() {
    const sel = document.getElementById('jobServerScope');
    if (!sel) return;
    try {
        const data = await apiGet('/api/servers');
        const servers = (data.servers || []).filter(s => s.enabled !== false);
        sel.innerHTML = '<option value="">All servers (show every library)</option>';
        servers.forEach(s => {
            const opt = document.createElement('option');
            opt.value = s.id;
            opt.textContent = `${s.name} (${(s.type || '').toUpperCase()})`;
            sel.appendChild(opt);
        });
        sel.value = '';
    } catch (e) {
        // Picker is optional — leave the default in place.
    }
}

async function onJobServerChange() {
    const sel = document.getElementById('jobServerScope');
    if (!sel) return;
    const serverId = sel.value;
    try {
        const url = serverId ? `/api/libraries?server_id=${encodeURIComponent(serverId)}` : '/api/libraries';
        const data = await apiGet(url);
        libraries = data.libraries || [];
        _renderJobLibraryList(libraries, serverId);
    } catch (e) {
        showToast('New Job', 'Could not load libraries for the selected server', 'warning');
    }
}

function _renderJobLibraryList(libs, filterServerId) {
    const listEl = document.getElementById('jobLibraryList');
    if (!listEl) return;

    // Visual confirmation that the picker actually filtered the list. Reads
    // the current selection's text directly from the picker so it matches
    // exactly what the user clicked, including the (PLEX) / (EMBY) suffix.
    const captionEl = document.getElementById('jobLibraryListCaption');
    if (captionEl) {
        if (filterServerId) {
            const sel = document.getElementById('jobServerScope');
            const serverLabel = sel ? sel.options[sel.selectedIndex].textContent : filterServerId;
            captionEl.innerHTML = `<i class="bi bi-funnel me-1"></i>Filtered to <strong>${escapeHtml(serverLabel)}</strong> &mdash; ${(libs || []).length} librar${(libs || []).length === 1 ? 'y' : 'ies'}`;
        } else {
            const distinctServers = new Set();
            for (const l of libs || []) {
                if (l && l.server_id) distinctServers.add(l.server_id);
            }
            const serverCount = distinctServers.size;
            captionEl.innerHTML = serverCount > 0
                ? `<i class="bi bi-collection me-1"></i>Showing all servers &mdash; ${(libs || []).length} libraries across ${serverCount} server${serverCount === 1 ? '' : 's'}`
                : '';
        }
    }
    if (!libs || libs.length === 0) {
        if (librariesLoadError) {
            listEl.innerHTML =
                '<div class="text-warning small d-flex align-items-start gap-2">' +
                '<i class="bi bi-exclamation-triangle-fill mt-1"></i>' +
                '<span>Can\'t load libraries right now. ' +
                '<a href="/servers" class="text-decoration-none">Check the Servers page</a>.</span>' +
                '</div>';
        } else {
            listEl.innerHTML = '<div class="text-muted small">No libraries available for this selection.</div>';
        }
        return;
    }

    // Group by server when "All servers" is selected — disambiguates the
    // same-named libraries on Plex+Emby. When pinned to one server, render
    // a flat list (no point in single-group headers).
    if (filterServerId) {
        listEl.innerHTML = libs.map(lib => `
            <div class="form-check">
                <input class="form-check-input job-library-checkbox" type="checkbox"
                       value="${lib.id}" id="jobLib_${lib.id}" disabled>
                <label class="form-check-label" for="jobLib_${lib.id}">
                    ${escapeHtml(lib.name)} <span class="text-muted small">(${libraryTypeLabel(lib)})</span>
                </label>
            </div>
        `).join('');
        return;
    }

    // Group by server for the "All servers" view.
    const groups = new Map();
    for (const lib of libs) {
        const key = lib.server_id || '__legacy__';
        if (!groups.has(key)) {
            groups.set(key, { server_name: lib.server_name || '', server_type: lib.server_type || '', libs: [] });
        }
        groups.get(key).libs.push(lib);
    }
    const sections = [];
    for (const [_, grp] of groups) {
        const stype = (grp.server_type || '').toLowerCase();
        const logo = _vendorLogo(stype, 14) || '';
        const head = `<div class="text-muted small mt-2 mb-1">${logo}<strong>${escapeHtml(grp.server_name || stype.toUpperCase() || 'Server')}</strong></div>`;
        const rows = grp.libs.map(lib => `
            <div class="form-check ms-2">
                <input class="form-check-input job-library-checkbox" type="checkbox"
                       value="${lib.id}" id="jobLib_${lib.id}" disabled>
                <label class="form-check-label" for="jobLib_${lib.id}">
                    ${escapeHtml(lib.name)} <span class="text-muted small">(${libraryTypeLabel(lib)})</span>
                </label>
            </div>
        `).join('');
        sections.push(head + rows);
    }
    listEl.innerHTML = sections.join('');
}

function toggleAllLibraries(checkbox) {
    const libraryCheckboxes = document.querySelectorAll('.job-library-checkbox');
    libraryCheckboxes.forEach(cb => {
        cb.disabled = checkbox.checked;
        if (checkbox.checked) {
            cb.checked = false;
        }
    });
}

// Phase H6: Select-all / None affordance for the multi-select picker.
function setAllLibrariesChecked(checked) {
    const allCb = document.getElementById('jobLibraryAll');
    if (allCb && allCb.checked) {
        allCb.checked = false;
        toggleAllLibraries(allCb);
    }
    document.querySelectorAll('.job-library-checkbox').forEach(cb => {
        cb.disabled = false;
        cb.checked = checked;
    });
}

async function startNewJob() {
    const allLibrariesCheckbox = document.getElementById('jobLibraryAll');
    const forceRegenerate = document.getElementById('jobRegenerateAll').checked;

    let selectedLibraryIds = [];
    let libraryName = 'All Libraries';

    if (!allLibrariesCheckbox.checked) {
        // Get selected library checkboxes
        const selectedCheckboxes = document.querySelectorAll('.job-library-checkbox:checked');
        var selectedIdsLocal = Array.from(selectedCheckboxes).map(cb => cb.value);

        if (selectedIdsLocal.length === 0) {
            showToast('Error', 'Please select at least one library', 'warning');
            return;
        }

        selectedLibraryIds = selectedIdsLocal;
        // Build a display name from the looked-up library names.
        if (selectedIdsLocal.length === 1) {
            const lib = libraries.find(l => l.id === selectedIdsLocal[0]);
            libraryName = lib ? lib.name : 'Selected Library';
        } else {
            libraryName = `${selectedIdsLocal.length} Libraries`;
        }
    }

    const priority = parseInt(document.getElementById('jobPriority').value, 10) || 2;
    const sortByEl = document.getElementById('jobSortBy');
    const sortBy = sortByEl ? sortByEl.value : '';

    const jobConfig = { force_generate: forceRegenerate };
    if (sortBy) {
        jobConfig.sort_by = sortBy;
    }

    const serverScope = document.getElementById('jobServerScope');
    const serverId = serverScope ? serverScope.value : '';

    // Phase H6 Fix-4: send library_ids as the canonical wire shape
    // (api_jobs.create_job already accepts both array shapes; new code prefers
    // IDs over name lookups, which avoids the comma-join hack).
    const jobPayload = {
        library_ids: selectedLibraryIds,
        library_name: libraryName,
        priority: priority,
        config: jobConfig,
    };
    if (serverId) jobPayload.server_id = serverId;

    // Retry once on transient network errors ("Failed to fetch" from
    // server congestion).
    let lastError;
    for (let attempt = 0; attempt < 2; attempt++) {
        try {
            const result = await apiPost('/api/jobs', jobPayload);

            bootstrap.Modal.getInstance(document.getElementById('newJobModal')).hide();
            loadJobs();
            loadJobStats();
            showToast('Job Started', 'Processing job has been started', 'success');
            return;  // success — exit
        } catch (error) {
            lastError = error;
            if (attempt === 0 && error.message === 'Failed to fetch') {
                // Brief pause before retry
                await new Promise(r => setTimeout(r, 500));
                continue;
            }
            break;
        }
    }
    showToast('Error', 'Failed to start job: ' + lastError.message, 'danger');
}

async function _populateManualServerScopePicker() {
    const sel = document.getElementById('manualServerScope');
    if (!sel) return;
    try {
        const data = await apiGet('/api/servers');
        const servers = (data.servers || []).filter(s => s.enabled !== false);
        sel.innerHTML = '<option value="">All servers (publish to whoever owns the file)</option>';
        servers.forEach(s => {
            const opt = document.createElement('option');
            opt.value = s.id;
            opt.textContent = `${s.name} (${(s.type || '').toUpperCase()})`;
            sel.appendChild(opt);
        });
    } catch (e) {
        // Picker is optional — leave the default "All servers" option in place.
    }
}

function showManualTriggerModal() {
    document.getElementById('manualFilePaths').value = '';
    document.getElementById('manualForceRegenerate').checked = false;
    document.getElementById('manualPriority').value = '2';
    const sel = document.getElementById('manualServerScope');
    if (sel) sel.value = '';
    _populateManualServerScopePicker();
    new bootstrap.Modal(document.getElementById('manualTriggerModal')).show();
}

async function startManualJob() {
    const raw = document.getElementById('manualFilePaths').value.trim();
    if (!raw) {
        showToast('Error', 'Please enter at least one file path', 'warning');
        return;
    }

    const paths = raw.split('\n').map(p => p.trim()).filter(p => p.length > 0);
    if (paths.length === 0) {
        showToast('Error', 'Please enter at least one file path', 'warning');
        return;
    }

    const forceRegenerate = document.getElementById('manualForceRegenerate').checked;
    const manualPriority = parseInt(document.getElementById('manualPriority').value, 10) || 2;
    const serverSel = document.getElementById('manualServerScope');
    const serverId = serverSel ? serverSel.value : '';

    try {
        const payload = {
            file_paths: paths,
            force_regenerate: forceRegenerate,
            priority: manualPriority,
        };
        if (serverId) payload.server_id = serverId;
        await apiPost('/api/jobs/manual', payload);
        bootstrap.Modal.getInstance(document.getElementById('manualTriggerModal')).hide();
        loadJobs();
        loadJobStats();
        const label = paths.length === 1 ? paths[0].split('/').pop() : `${paths.length} files`;
        showToast('Job Started', `Processing ${label}`, 'success');
    } catch (error) {
        showToast('Error', 'Failed to start manual job: ' + error.message, 'danger');
    }
}

async function cancelJob(jobId) {
    if (!confirm('Are you sure you want to cancel this job?')) return;

    try {
        await apiPost(`/api/jobs/${jobId}/cancel`);
        loadJobs();
        loadJobStats();
    } catch (error) {
        showToast('Error', 'Failed to cancel job: ' + error.message, 'danger');
    }
}

async function pauseJob(jobId) {
    try {
        await apiPost(`/api/jobs/${jobId}/pause`);
        await loadJobs();
        showToast('Paused', 'Job paused. Running tasks will finish before dispatch continues.', 'warning');
    } catch (error) {
        showToast('Error', 'Failed to pause job: ' + error.message, 'danger');
    }
}

async function resumeJob(jobId) {
    try {
        await apiPost(`/api/jobs/${jobId}/resume`);
        await loadJobs();
        showToast('Resumed', 'Job resumed', 'success');
    } catch (error) {
        showToast('Error', 'Failed to resume job: ' + error.message, 'danger');
    }
}

async function scaleWorkers(jobId, workerType, delta) {
    const endpoint = delta > 0 ? 'add' : 'remove';
    const count = Math.abs(delta);

    try {
        const result = await apiPost(`/api/jobs/${jobId}/workers/${endpoint}`, {
            worker_type: workerType,
            count
        });
        await Promise.all([loadJobs(), loadWorkerStatuses(), refreshStatus()]);
        if (endpoint === 'add') {
            showToast('Workers Updated', `Added ${result.added} ${workerType} worker(s)`, 'success');
        } else {
            const scheduledRemoval = result.scheduled_removal || 0;
            const unavailable = result.unavailable || 0;
            if (scheduledRemoval > 0 || unavailable > 0) {
                showToast(
                    'Workers Updated',
                    `Removed ${result.removed} ${workerType}; ${scheduledRemoval} scheduled after current tasks; ${unavailable} unavailable`,
                    'warning'
                );
            } else {
                showToast('Workers Updated', `Removed ${result.removed} ${workerType} worker(s)`, 'info');
            }
        }
    } catch (error) {
        showToast('Error', `Failed to ${endpoint} ${workerType} worker(s): ${error.message}`, 'danger');
    }
}

function refreshWorkerScaleButtons() {
    const buttons = document.querySelectorAll('.worker-scale-btn');
    buttons.forEach((btn) => {
        const direction = parseInt(btn.getAttribute('data-direction'), 10);
        if (direction === 1) {
            btn.disabled = false;
            return;
        }
        const workerType = btn.getAttribute('data-worker-type');
        btn.disabled = getWorkerCountForType(workerType) <= 0;
    });
}

function getWorkerCountForType(workerType) {
    if (workerType !== 'CPU') return 0;
    const el = document.getElementById('cpuWorkers');
    if (!el) return 0;
    const n = parseInt(el.textContent, 10);
    return Number.isNaN(n) ? 0 : n;
}

function settingsKeyForWorkerType(workerType) {
    if (workerType === 'CPU') return 'cpu_threads';
    return null;
}

async function scaleWorkersGlobal(workerType, direction) {
    const currentCount = getWorkerCountForType(workerType);
    const newCount = Math.max(0, currentCount + direction);
    if (newCount === currentCount) return;

    const settingsKey = settingsKeyForWorkerType(workerType);

    try {
        const saveResult = await apiPost('/api/settings', { [settingsKey]: newCount });
        cachedWorkerConfigCounts = null;
        await loadWorkerConfigCounts(true);

        const badgeEl = workerType === 'CPU' ? document.getElementById('cpuWorkers') : null;
        if (badgeEl) badgeEl.textContent = String(newCount);
        refreshWorkerScaleButtons();

        if (saveResult.warning) {
            showToast('Warning', saveResult.warning, 'warning');
        }

        const endpoint = direction > 0 ? 'add' : 'remove';
        try {
            const result = await apiPost(`/api/workers/${endpoint}`, {
                worker_type: workerType,
                count: 1
            });
            await Promise.all([loadJobs(), loadWorkerStatuses(), refreshStatus()]);
            if (endpoint === 'add') {
                showToast('Workers Updated', `Added ${result.added} ${workerType} worker(s)`, 'success');
            } else {
                const scheduled = result.scheduled_removal || 0;
                if (scheduled > 0) {
                    showToast('Workers Updated', `Removed ${result.removed} ${workerType}; ${scheduled} scheduled after current tasks`, 'warning');
                } else {
                    showToast('Workers Updated', `Removed ${result.removed} ${workerType} worker(s)`, 'info');
                }
            }
        } catch (scaleErr) {
            if (!saveResult.warning) {
                showToast('Setting Saved', `${workerType} workers set to ${newCount}`, 'success');
            }
        }
    } catch (error) {
        const badgeEl = workerType === 'CPU' ? document.getElementById('cpuWorkers') : null;
        if (badgeEl) badgeEl.textContent = String(currentCount);
        refreshWorkerScaleButtons();
        showToast('Error', `Failed to update ${workerType} workers: ${error.message}`, 'danger');
    }
}

async function deleteJob(jobId) {
    if (!confirm('Are you sure you want to delete this job?')) return;

    try {
        await apiDelete(`/api/jobs/${jobId}`);
        loadJobs();
        loadJobStats();
    } catch (error) {
        showToast('Error', 'Failed to delete job: ' + error.message, 'danger');
    }
}

async function reprocessJob(jobId) {
    try {
        const job = await apiPost(`/api/jobs/${jobId}/reprocess`);
        loadJobs();
        loadJobStats();
        showToast('Reprocess Started', `Job ${job.id.substring(0, 8)} created`, 'success');
    } catch (error) {
        const msg = error.message || '';
        if (msg.includes('409') || msg.includes('running or pending')) {
            showToast('Cannot reprocess', 'Job is still running or pending', 'warning');
        } else {
            showToast('Error', 'Failed to reprocess job: ' + msg, 'danger');
        }
    }
}

async function clearCompletedJobs() {
    if (!confirm('Clear all completed, failed, and cancelled jobs?')) return;

    try {
        const result = await apiPost('/api/jobs/clear');
        loadJobs();
        loadJobStats();
        showToast('Jobs Cleared', `Cleared ${result.cleared} jobs`, 'info');
    } catch (error) {
        showToast('Error', 'Failed to clear jobs: ' + error.message, 'danger');
    }
}

async function clearJobsByStatus() {
    const checkboxes = document.querySelectorAll('.clear-status-cb:checked');
    const statuses = Array.from(checkboxes).map(cb => cb.value);

    if (statuses.length === 0) {
        showToast('Warning', 'Select at least one status to clear', 'warning');
        return;
    }

    const labels = statuses.join(', ');
    if (!confirm(`Clear all ${labels} jobs?`)) return;

    try {
        const result = await apiPost('/api/jobs/clear', { statuses });
        loadJobs();
        loadJobStats();
        showToast('Jobs Cleared', `Cleared ${result.cleared} ${labels} jobs`, 'info');
    } catch (error) {
        showToast('Error', 'Failed to clear jobs: ' + error.message, 'danger');
    }
}

// Helper Functions
function _buildOutcomeTooltip(outcome) {
    if (!outcome || typeof outcome !== 'object') return '';
    // D14 — pull labels from the unified STATUS_META so the tooltip
    // matches the file-outcome chip and the per-server pill.
    var keys = ['generated', 'skipped_bif_exists', 'skipped_not_indexed',
                'skipped_file_not_found', 'skipped_excluded',
                'skipped_invalid_hash', 'failed', 'no_media_parts'];
    var lines = [];
    for (var i = 0; i < keys.length; i++) {
        var count = outcome[keys[i]];
        if (count && count > 0) {
            lines.push(_statusMeta(keys[i]).label + ': ' + count.toLocaleString());
        }
    }
    return lines.length > 0 ? lines.join('&#10;') : 'No items processed';
}

function getStatusBadge(status, paused, error, outcome) {
    if (paused === undefined) paused = false;
    if (error === undefined) error = null;
    if (outcome === undefined) outcome = null;

    var tooltipText = _buildOutcomeTooltip(outcome);
    if (error) {
        tooltipText = tooltipText
            ? tooltipText + '&#10;' + error
            : error;
    }
    var tooltipAttrs = tooltipText
        ? ' data-bs-toggle="tooltip" data-bs-placement="top" data-bs-html="false" title="' + tooltipText + '"'
        : '';

    // Modern status indicator: a coloured dot + neutral label reads
    // cleaner than a stack of full-fill pills in a dense table. The
    // CSS lives under .status-dot in style.css.
    function dot(cls, label) {
        return '<span class="status-dot ' + cls + '"' + tooltipAttrs + '>' + label + '</span>';
    }

    if (status === 'running' && paused) return dot('status-warning', 'Paused');
    if (status === 'completed' && error) return dot('status-warning', 'Completed with warnings');

    var clsMap = {
        'pending':   'status-pending',
        'running':   'status-running',
        'completed': 'status-completed',
        'failed':    'status-failed',
        'cancelled': 'status-cancelled'
    };
    var labelMap = {
        'pending':   'Pending',
        'running':   'Running',
        'completed': 'Completed',
        'failed':    'Failed',
        'cancelled': 'Cancelled'
    };
    return dot(clsMap[status] || 'status-pending', labelMap[status] || status);
}

function formatDate(dateStr) {
    if (!dateStr) return '-';
    const date = new Date(dateStr);
    return date.toLocaleString();
}

// Relative time with absolute date as a tooltip. The dashboard's
// "Created" column used to show an awkward "5/2/2026, 6:01:38 PM"
// that was hard to scan at a glance — relative reads in 1 word
// ("2 min ago") and the absolute is one hover away. Falls back to
// a short locale string for anything older than a week.
function formatRelativeTime(dateStr) {
    if (!dateStr) return '<span class="text-muted">—</span>';
    const date = new Date(dateStr);
    const now = Date.now();
    const diffSec = Math.round((now - date.getTime()) / 1000);
    let label;
    if (diffSec < 5) label = 'just now';
    else if (diffSec < 60) label = `${diffSec}s ago`;
    else if (diffSec < 3600) label = `${Math.round(diffSec / 60)} min ago`;
    else if (diffSec < 86400) label = `${Math.round(diffSec / 3600)} h ago`;
    else if (diffSec < 86400 * 7) label = `${Math.round(diffSec / 86400)} d ago`;
    else label = date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    const abs = date.toLocaleString();
    return `<span class="text-nowrap" title="${escapeHtml(abs)}">${label}</span>`;
}

function formatElapsed(startDateStr) {
    if (!startDateStr) return '';
    const elapsed = Math.max(0, Math.floor((Date.now() - new Date(startDateStr).getTime()) / 1000));
    const h = Math.floor(elapsed / 3600);
    const m = Math.floor((elapsed % 3600) / 60);
    const s = elapsed % 60;
    if (h > 0) return `${h}h ${m}m ${s}s`;
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
}

function _updateElapsedTimers() {
    document.querySelectorAll('[data-elapsed-since]').forEach(function (el) {
        el.textContent = formatElapsed(el.getAttribute('data-elapsed-since'));
    });
    document.querySelectorAll('[data-scheduled-at]').forEach(function (el) {
        var scheduled = new Date(el.getAttribute('data-scheduled-at')).getTime();
        var remaining = Math.max(0, Math.ceil((scheduled - Date.now()) / 1000));
        if (remaining > 0) {
            el.innerHTML = '<i class="bi bi-hourglass-split me-1"></i>Retry starting in ' + remaining + 's';
        } else {
            el.innerHTML = '<i class="bi bi-hourglass-split me-1"></i>Starting...';
        }
    });
}

function _ensureElapsedTimer() {
    if (_elapsedTimerInterval) return;
    _elapsedTimerInterval = setInterval(_updateElapsedTimers, 1000);
}

function _stopElapsedTimer() {
    if (!_elapsedTimerInterval) return;
    if (document.querySelector('[data-scheduled-at]')) return;
    clearInterval(_elapsedTimerInterval);
    _elapsedTimerInterval = null;
}

function showToast(title, message, type = 'info') {
    const toast = document.getElementById('toastNotification');
    const toastTitle = document.getElementById('toastTitle');
    const toastBody = document.getElementById('toastBody');
    const toastIcon = document.getElementById('toastIcon');

    toastTitle.textContent = title;
    toastBody.textContent = message;

    // Update icon based on type
    const icons = {
        'success': 'bi-check-circle text-success',
        'danger': 'bi-exclamation-circle text-danger',
        'warning': 'bi-exclamation-triangle text-warning',
        'info': 'bi-info-circle text-info'
    };
    toastIcon.className = `bi ${icons[type] || icons.info} me-2`;

    const bsToast = new bootstrap.Toast(toast);
    bsToast.show();
}

// "What's New" changelog popup (runs on every page via base.html)
async function checkWhatsNew() {
    try {
        const resp = await fetch('/api/system/whats-new');
        if (!resp.ok) return;
        const data = await resp.json();
        if (!data.has_new || !data.entries || data.entries.length === 0) return;

        const body = document.getElementById('whatsNewBody');
        if (!body) return;

        let html = '';
        for (const entry of data.entries) {
            const date = entry.date ? new Date(entry.date).toLocaleDateString() : '';
            html += `<div class="mb-4">`;
            html += `<h5 class="d-flex align-items-center gap-2">`;
            html += `<span class="badge bg-primary">${escapeHtml(entry.version)}</span>`;
            html += `<span>${escapeHtml(entry.name)}</span>`;
            if (date) html += `<small class="text-muted ms-auto">${escapeHtml(date)}</small>`;
            html += `</h5>`;
            if (entry.body) {
                html += `<div class="changelog-body text-break">${_renderMarkdownBasic(entry.body)}</div>`;
            }
            if (entry.url) {
                html += `<a href="${escapeHtml(entry.url)}" target="_blank" class="small text-decoration-none">`;
                html += `<i class="bi bi-box-arrow-up-right me-1"></i>View on GitHub</a>`;
            }
            html += `</div>`;
        }
        body.innerHTML = html;

        const modalEl = document.getElementById('whatsNewModal');
        const modal = new bootstrap.Modal(modalEl);
        modal.show();

        modalEl.addEventListener('hidden.bs.modal', async function () {
            try { await fetch('/api/system/whats-new/dismiss', { method: 'POST', headers: { 'X-CSRFToken': getCsrfToken() } }); }
            catch (e) { console.warn('Failed to dismiss what\'s new:', e); }
        }, { once: true });
    } catch (e) {
        console.debug('What\'s new check skipped:', e);
    }
}

function _renderMarkdownBasic(md) {
    // GitHub release bodies arrive with CRLF and frequently use leading
    // whitespace under top-level headings — handle both before any line-anchored
    // regex runs, otherwise headings/lists silently fail to match.
    let html = escapeHtml(md).replace(/\r\n/g, '\n').replace(/\r/g, '\n');
    // Headings: tolerate up to 4 leading spaces (CommonMark allows 0–3, GitHub
    // bodies sometimes go further when authors hand-indent under a section).
    html = html.replace(/^[ \t]{0,4}### (.+)$/gm, '<h6 class="mt-3 mb-1">$1</h6>');
    html = html.replace(/^[ \t]{0,4}## (.+)$/gm, '<h5 class="mt-3 mb-1">$1</h5>');
    // Inline code BEFORE bold so a `**foo**` inside `code` doesn't get bolded.
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    // Markdown links — extremely common in release notes (PRs, issues, docs).
    // Restrict the URL to safe schemes so a hand-crafted body can't drop a
    // javascript:/data: link into the modal.
    html = html.replace(/\[([^\]]+)\]\(((?:https?:|mailto:)[^)\s]+)\)/g, function (_m, text, url) {
        return '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + text + '</a>';
    });
    html = html.replace(/^[ \t]*[*-] (.+)$/gm, '<li>$1</li>');
    html = html.replace(/(<li>.*<\/li>\n?)+/g, function (m) { return '<ul class="mb-2">' + m + '</ul>'; });
    html = html.replace(/\n{2,}/g, '<br><br>');
    html = html.replace(/\n/g, '<br>');
    return html;
}

// checkWhatsNew() is called from the dashboard page (index.html) only,
// to avoid hitting the GitHub API on every page navigation.


// ============================================================================
// J6 — Settings > Backups panel
// ============================================================================
//
// Lists every (live, .bak) pair this app maintains and lets the user one-click
// swap them. The panel only shows on Settings (the trigger is the panel
// element itself; absent on every other page).

function _formatBackupTime(epoch) {
    if (!epoch) return '—';
    const d = new Date(epoch * 1000);
    return d.toLocaleString();
}

function _formatBackupLabel(b) {
    if (b.legacy) return 'Previous version';
    // timestamp is YYYYMMDD-HHMMSS (UTC). Render as local time.
    const ts = b.timestamp || '';
    if (ts.length === 15) {
        const iso = `${ts.slice(0, 4)}-${ts.slice(4, 6)}-${ts.slice(6, 8)}T${ts.slice(9, 11)}:${ts.slice(11, 13)}:${ts.slice(13, 15)}Z`;
        const d = new Date(iso);
        if (!Number.isNaN(d.getTime())) return d.toLocaleString();
    }
    return _formatBackupTime(b.mtime);
}

async function refreshBackupsPanel() {
    const panel = document.getElementById('backupRestorePanel');
    if (!panel) return;
    panel.innerHTML = '<div class="text-muted small"><span class="spinner-border spinner-border-sm me-1"></span>Checking for backups…</div>';
    try {
        const data = await apiGet('/api/settings/backups');
        const files = data.files || [];
        if (!files.length) {
            panel.innerHTML = '<div class="text-muted small">No backup-tracked config files found.</div>';
            return;
        }
        // D17 — one row per managed file: header + dropdown + Restore button.
        // Replaces the old vertical-list-of-rows that grew with retention,
        // making the panel feel cluttered for users on installs that save
        // settings often (every webhook → webhook_history.json → +1 row).
        const blocks = files.map((f, idx) => {
            const liveAge = _formatBackupTime(f.live_mtime);
            const headerBadge = f.bak_newer
                ? '<span class="badge bg-warning text-dark ms-2" title="Most recent backup is newer than the live file">backup is newer</span>'
                : (f.has_bak ? '' : '<span class="badge bg-secondary ms-2">no backups yet</span>');

            const backups = f.backups || [];
            const selectId = 'backupSelect-' + idx;
            const selectHtml = backups.length
                ? `
                    <div class="d-flex align-items-center gap-2 mt-2">
                        <select id="${selectId}" class="form-select form-select-sm" style="max-width: 320px;">
                            ${backups.map((b) => {
                                const label = escapeHtmlText(_formatBackupLabel(b))
                                    + (b.legacy ? ' (legacy)' : '');
                                return `<option value="${escapeHtmlAttr(b.filename)}">${label}</option>`;
                            }).join('')}
                        </select>
                        <button type="button" class="btn btn-sm btn-outline-warning flex-shrink-0"
                                data-restore-file="${escapeHtmlAttr(f.name)}"
                                data-restore-select="${selectId}">
                            <i class="bi bi-arrow-counterclockwise me-1"></i>Restore selected
                        </button>
                        <span class="text-muted small flex-shrink-0">${backups.length} snapshot${backups.length === 1 ? '' : 's'}</span>
                    </div>
                `
                : '<div class="small text-muted mt-2">No backups yet — they appear after the next save.</div>';

            return `
                <div class="border-bottom py-2">
                    <div>
                        <code>${escapeHtmlText(f.name)}</code>${headerBadge}
                        <div class="small text-muted">Live saved: ${escapeHtmlText(liveAge)}</div>
                    </div>
                    ${selectHtml}
                </div>
            `;
        }).join('');
        panel.innerHTML = blocks;
        panel.querySelectorAll('[data-restore-file]').forEach((btn) => {
            btn.addEventListener('click', async (ev) => {
                const target = ev.currentTarget;
                const file = target.dataset.restoreFile;
                const select = document.getElementById(target.dataset.restoreSelect);
                const backup = select ? select.value : '';
                if (!file || !backup) return;
                if (!confirm(`Restore ${file} from ${backup}? The current contents will be saved as a fresh backup first, so you can undo this restore.`)) return;
                target.disabled = true;
                const origHtml = target.innerHTML;
                target.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Restoring…';
                try {
                    await apiPost('/api/settings/backups/restore', { file, backup });
                    showToast('Backup restored', `${file} restored from ${backup}. Reload the page for caches to pick it up.`, 'success');
                    refreshBackupsPanel();
                } catch (e) {
                    showToast('Restore failed', (e && e.message) || 'Unknown error', 'danger');
                    target.disabled = false;
                    target.innerHTML = origHtml;
                }
            });
        });
    } catch (e) {
        panel.innerHTML = `<div class="text-warning small"><i class="bi bi-exclamation-triangle me-1"></i>Could not list backups: ${escapeHtmlText((e && e.message) || 'unknown error')}</div>`;
    }
}

// D17 — load + save retention controls. Hooks into the global Settings
// save flow (the SaveSettings button in the settings sidebar already
// POSTs every form input that has a known field name, so just keep the
// inputs in sync with /api/settings on load).
async function _initBackupRetentionControls() {
    const keepInput = document.getElementById('configBackupKeep');
    const ageInput = document.getElementById('configBackupMaxAgeDays');
    if (!keepInput || !ageInput) return;
    try {
        const data = await apiGet('/api/settings');
        const keep = parseInt(data.config_backup_keep, 10);
        const age = parseInt(data.config_backup_max_age_days, 10);
        keepInput.value = Number.isFinite(keep) ? keep : 10;
        ageInput.value = Number.isFinite(age) ? age : 0;
    } catch (e) {
        keepInput.value = 10;
        ageInput.value = 0;
    }
    const persist = async () => {
        const keep = Math.min(100, Math.max(1, parseInt(keepInput.value, 10) || 10));
        const age = Math.min(365, Math.max(0, parseInt(ageInput.value, 10) || 0));
        keepInput.value = keep;
        ageInput.value = age;
        try {
            await apiPost('/api/settings', {
                config_backup_keep: keep,
                config_backup_max_age_days: age,
            });
            showToast('Backups', 'Retention updated', 'success');
        } catch (e) {
            showToast('Save failed', (e && e.message) || 'Unknown error', 'danger');
        }
    };
    keepInput.addEventListener('change', persist);
    ageInput.addEventListener('change', persist);
}

document.addEventListener('DOMContentLoaded', () => {
    if (document.getElementById('backupRestorePanel')) {
        refreshBackupsPanel();
        const btn = document.getElementById('refreshBackupsBtn');
        if (btn) btn.addEventListener('click', refreshBackupsPanel);
        _initBackupRetentionControls();
    }
    // Centralised Bootstrap tooltip init: every page that lands on the
    // base template gets `[data-bs-toggle="tooltip"]` initialised once
    // on load, so individual pages no longer need their own init blocks.
    // For elements added dynamically after page load, call
    // window._initBootstrapTooltips(scope) — see below.
    _initBootstrapTooltips(document);
});

/**
 * Initialise Bootstrap tooltips on every `[data-bs-toggle="tooltip"]`
 * element under ``scope`` (defaults to the whole document). Safe to
 * call multiple times — Bootstrap's `Tooltip.getInstance(el)` short-
 * circuits if a tooltip already exists for the element.
 *
 * Pages with dynamic content (modals, library refreshes, etc.) should
 * call this after the new DOM lands so the tooltips render.
 */
function _initBootstrapTooltips(scope) {
    const root = scope || document;
    if (typeof bootstrap === 'undefined' || !bootstrap.Tooltip) return;
    root.querySelectorAll('[data-bs-toggle="tooltip"]').forEach((el) => {
        if (!bootstrap.Tooltip.getInstance(el)) {
            new bootstrap.Tooltip(el);
        }
    });
}
window._initBootstrapTooltips = _initBootstrapTooltips;
