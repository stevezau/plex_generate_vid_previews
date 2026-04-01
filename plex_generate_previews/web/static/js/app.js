/**
 * Plex Preview Generator - Dashboard JavaScript
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
    loadSchedules();
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

    // Flush deferred job-queue updates when a priority dropdown closes
    document.addEventListener('hidden.bs.dropdown', function () {
        if (_jobQueueUpdatePending) {
            updateJobQueue();
        }
    });
}

// SocketIO Connection
function connectSocket() {
    socket = io('/jobs', {
        transports: ['websocket', 'polling'],
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

async function apiGet(url) {
    const response = await fetch(url);
    if (!response.ok) {
        // Handle authentication errors
        if (response.status === 401) {
            console.error('Authentication failed, redirecting to login');
            window.location.href = '/login';
            throw new Error('Authentication required');
        }
        const text = await response.text();
        try {
            const json = JSON.parse(text);
            throw new Error(json.error || `HTTP ${response.status}`);
        } catch (e) {
            if (e.message.includes('HTTP') || e.message.includes('Authentication')) throw e;
            throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        }
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
        const text = await response.text();
        try {
            const json = JSON.parse(text);
            throw new Error(json.error || `HTTP ${response.status}`);
        } catch (e) {
            if (e.message.includes('HTTP')) throw e;
            throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        }
    }
    return response.json();
}

async function apiDelete(url) {
    const response = await fetch(url, {
        method: 'DELETE',
        headers: { 'X-CSRFToken': getCsrfToken() }
    });
    if (!response.ok) {
        const text = await response.text();
        try {
            const json = JSON.parse(text);
            throw new Error(json.error || `HTTP ${response.status}`);
        } catch (e) {
            if (e.message.includes('HTTP')) throw e;
            throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        }
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
        const text = await response.text();
        try {
            const json = JSON.parse(text);
            throw new Error(json.error || `HTTP ${response.status}`);
        } catch (e) {
            if (e.message.includes('HTTP')) throw e;
            throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        }
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

    // --- Plex server status ---
    try {
        const plexStatus = document.getElementById('plexStatus');
        const plexInfo = document.getElementById('plexServerInfo');
        if (plexStatus && plexInfo && typeof SettingsManager !== 'undefined') {
            const settings = await new SettingsManager().get();
            if (settings.plex_name) {
                plexInfo.textContent = settings.plex_name;
                plexStatus.textContent = 'Connected';
                plexStatus.className = 'badge bg-success';
            } else if (settings.plex_url) {
                plexInfo.textContent = settings.plex_url;
                plexStatus.textContent = 'Configured';
                plexStatus.className = 'badge bg-info';
            } else {
                plexStatus.textContent = 'Not configured';
                plexStatus.className = 'badge bg-warning';
            }
        }
    } catch (e) {
        console.warn('Failed to refresh Plex status:', e);
        const plexStatus = document.getElementById('plexStatus');
        if (plexStatus) {
            plexStatus.textContent = 'Unknown';
            plexStatus.className = 'badge bg-secondary';
        }
    }

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
        cpu_threads: Number(config?.cpu_threads ?? 1),
        cpu_fallback_threads: Number(config?.cpu_fallback_threads ?? 0)
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

async function loadLibraries() {
    try {
        const data = await apiGet('/api/libraries');
        libraries = data.libraries || [];
        await updateLibraryList();
        updateLibrarySelects();
    } catch (error) {
        console.error('Failed to load libraries:', error);
        const detail = error.message || 'Unknown error';
        document.getElementById('libraryList').innerHTML =
            `<div class="text-danger small">Failed to load libraries: ${detail}</div>`;
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

async function loadJobStats() {
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
        const parts = pending.map(function (p) {
            const remaining = Math.max(0, Math.ceil(p.remaining_seconds));
            const source = escapeHtml(p.source || 'webhook');
            const label = p.file_count === 1 && p.first_title
                ? escapeHtml(p.first_title)
                : `${p.file_count} file(s)`;
            return `<strong>${source}</strong>: ${label} — starting in <strong>${remaining}s</strong>`;
        });
        content.innerHTML = parts.join(' &middot; ');
    } catch (error) {
        row.classList.add('d-none');
    }
}

function renderGlobalPauseResume() {
    const pauseTitle = 'Pause all processing. No new jobs will start; active job will stop dispatching new tasks after current ones finish.';
    const resumeTitle = 'Resume processing. New jobs can start and dispatch will continue.';
    const pauseBtn = `<button class="btn btn-sm btn-outline-warning" onclick="pauseProcessing()" title="${escapeHtml(pauseTitle)}">
        <i class="bi bi-pause-fill me-1"></i>Pause Processing
    </button>`;
    const resumeBtn = `<button class="btn btn-sm btn-outline-success" onclick="resumeProcessing()" title="${escapeHtml(resumeTitle)}">
        <i class="bi bi-play-fill me-1"></i>Resume Processing
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

    // Running job
    html += '<div>';
    html += '<h6><i class="bi bi-activity me-2"></i>Status</h6>';
    if (status.running_job) {
        html += `<span class="badge bg-primary">Processing</span>`;
    } else if (status.pending_jobs > 0) {
        html += `<span class="badge bg-secondary">${status.pending_jobs} job(s) pending</span>`;
    } else {
        html += `<span class="badge bg-success">Idle</span>`;
    }
    html += '</div>';

    document.getElementById('systemStatus').innerHTML = html;

    renderDashboardGpuConfig();
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

        if (isFailed) {
            const errorTitle = escapeHtml(gpu.error || 'GPU unusable');
            const errorDetail = escapeHtml(gpu.error_detail || '');
            html += `<div class="d-flex justify-content-between align-items-center mb-2">`;
            html += `<span class="text-truncate me-2" style="max-width: 70%;" title="${safeDevice}">`;
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
        html += `<span class="text-truncate me-2" style="max-width: 55%;" title="${safeDevice}">`;
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
    html += `<div class="mt-1"><a href="/settings" class="small text-decoration-none"><i class="bi bi-gear me-1"></i>Configure GPUs in Settings</a></div>`;
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
    let html = '';

    if (libraries.length === 0) {
        html = '<div class="text-muted small">No libraries found</div>';
        document.getElementById('libraryList').innerHTML = html;
        return;
    }

    // Determine which libraries are selected in settings
    let selectedNames = [];
    try {
        if (typeof SettingsManager !== 'undefined') {
            const settings = await new SettingsManager().get();
            selectedNames = (settings.selected_libraries || []).map(function (s) {
                return String(s).trim().toLowerCase();
            }).filter(function (s) { return s.length > 0; });
        }
    } catch (e) {
        console.warn('Could not load selected_libraries for filter:', e);
    }

    const allSelected = selectedNames.length === 0;

    if (allSelected) {
        html += '<div class="text-muted small mb-2">All libraries selected</div>';
    } else {
        html += `<div class="text-muted small mb-2">${selectedNames.length} of ${libraries.length} selected &middot; <a href="/settings" class="text-decoration-none">Manage</a></div>`;
    }

    for (const lib of libraries) {
        const icon = lib.type === 'movie' ? 'bi-film' : 'bi-tv';
        const typeLabel = lib.type === 'movie' ? 'Movies' : 'TV Shows';
        const isSelected = allSelected || selectedNames.includes(lib.name.toLowerCase());
        const dimClass = isSelected ? '' : ' opacity-50';

        html += `
            <div class="library-item${dimClass}">
                <span class="library-name">
                    <i class="bi ${icon} me-2"></i>${escapeHtml(lib.name)}
                </span>
                <span class="library-count">${typeLabel}</span>
            </div>
        `;
    }

    document.getElementById('libraryList').innerHTML = html;
}

function updateLibrarySelects() {
    const selects = ['jobLibrary', 'scheduleLibrary'];

    for (const selectId of selects) {
        const select = document.getElementById(selectId);
        if (!select) continue;

        // Keep first option (All Libraries)
        select.innerHTML = '<option value="">All Libraries</option>';

        for (const lib of libraries) {
            const option = document.createElement('option');
            option.value = lib.id;
            option.textContent = `${lib.name} (${lib.type})`; // textContent auto-escapes
            select.appendChild(option);
        }
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

let _jobQueueUpdatePending = false;

function updateJobQueue() {
    const tbody = document.getElementById('jobQueue');

    // Defer rebuild while a priority dropdown is open to avoid destroying it
    if (tbody && tbody.querySelector('.dropdown-menu.show')) {
        _jobQueueUpdatePending = true;
        return;
    }
    _jobQueueUpdatePending = false;

    if (jobs.length === 0) {
        const msg = jobTotal === 0 ? 'No jobs in queue' : 'No jobs on this page';
        tbody.innerHTML = `
            <tr>
                <td colspan="7" class="text-center text-muted py-4">
                    ${msg}
                </td>
            </tr>
        `;
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
        const created = formatDate(job.created_at);
        let actionButtons = '';

        if (job.status === 'running' || job.status === 'pending') {
            actionButtons += `<button class="btn btn-sm btn-outline-danger" onclick="cancelJob('${escapeHtml(job.id)}')" title="Cancel">
                                <i class="bi bi-x"></i>
                              </button>`;
        } else {
            actionButtons = `<button class="btn btn-sm btn-outline-info me-1" onclick="showLogsModal('${escapeHtml(job.id)}')" title="View Logs">
                                <i class="bi bi-file-text"></i>
                             </button>
                             <button class="btn btn-sm btn-outline-primary me-1" onclick="reprocessJob('${escapeHtml(job.id)}')" title="Reprocess">
                                <i class="bi bi-arrow-repeat"></i>
                             </button>
                             <button class="btn btn-sm btn-outline-secondary" onclick="deleteJob('${escapeHtml(job.id)}')" title="Delete">
                                <i class="bi bi-trash"></i>
                             </button>`;
        }

        let webhookBasenames = job.config && Array.isArray(job.config.webhook_basenames) && job.config.webhook_basenames.length > 0
            ? job.config.webhook_basenames
            : [];
        if (webhookBasenames.length === 0 && job.config && Array.isArray(job.config.webhook_paths) && job.config.webhook_paths.length > 0) {
            webhookBasenames = job.config.webhook_paths.map(function (p) { return p.split('/').pop() || p; });
        }
        const hasMultiFile = webhookBasenames.length > 1;
        const isFilesExpanded = expandedJobFileRows.has(String(job.id));
        const libraryTitle = webhookBasenames.length > 0
            ? ` title="${escapeHtml(webhookBasenames.join(', '))}"`
            : '';
        const filesToggleBtn = hasMultiFile
            ? ` <button type="button" class="btn btn-sm btn-link p-0 ms-1 align-baseline" id="job-files-toggle-${escapeHtml(job.id)}"
                        onclick="toggleJobFiles('${escapeHtml(job.id)}')" aria-expanded="${isFilesExpanded ? 'true' : 'false'}" aria-controls="job-detail-${escapeHtml(job.id)}" title="Show files">
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
            progressCell = `<div class="progress" style="height: 20px;">
                        <div class="progress-bar" role="progressbar"
                             style="width: ${progress}%">${progress}%</div>
                    </div>`;
        }
        html += `
            <tr id="job-row-${escapeHtml(job.id)}">
                <td><code>${escapeHtml(job.id.substring(0, 8))}</code></td>
                <td${libraryTitle}>${escapeHtml(job.library_name) || 'All Libraries'}${retryLabel}${filesToggleBtn}</td>
                <td>${statusBadge}</td>
                <td>${priorityCell}</td>
                <td>${progressCell}</td>
                <td>${created}</td>
                <td class="text-nowrap">
                    ${actionButtons}
                </td>
            </tr>
        `;
        if (hasMultiFile) {
            const filesList = webhookBasenames
                .map(function (b) { return `<div class="text-muted">${escapeHtml(b)}</div>`; })
                .join('');
            const overflow = job.config.path_count > webhookBasenames.length
                ? `<div class="text-muted mt-1">(+${job.config.path_count - webhookBasenames.length} more)</div>`
                : '';
            html += `
            <tr id="job-detail-${escapeHtml(job.id)}" class="${isFilesExpanded ? '' : 'd-none'} job-files-detail" aria-hidden="${isFilesExpanded ? 'false' : 'true'}">
                <td colspan="7" class="bg-dark bg-opacity-10 small py-2 ps-4">
                    <strong>Files:</strong>
                    <div class="mt-1">${filesList}${overflow}</div>
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

    if (!runningJobs || runningJobs.length === 0) {
        countBadge.textContent = 'No active jobs';
        countBadge.className = 'badge bg-secondary';
        container.innerHTML = `
            <div class="text-muted text-center py-4">
                <i class="bi bi-inbox fs-1 d-block mb-2"></i>
                No jobs are currently running
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
        } else if (webhookFiles && webhookFiles.length === 1) {
            webhookFilesHtml = `<br><strong>File:</strong> <span class="text-muted small">${escapeHtml(webhookFiles[0])}</span>`;
        }

        // Start time and elapsed
        const startedLine = job.started_at
            ? `<span class="text-muted small"><i class="bi bi-clock me-1"></i>Started ${formatDate(job.started_at)} (<span data-elapsed-since="${escapeHtml(job.started_at)}">${formatElapsed(job.started_at)}</span>)</span>`
            : '';

        const activePri = job.priority || 2;
        const activePriBadge = `<span class="badge ${PRIORITY_BADGE_CLASS[activePri] || 'bg-primary'} priority-badge ms-1">${PRIORITY_LABELS[activePri] || 'Normal'}</span>`;

        html += `
        <div class="active-job-card mb-3 p-3 border rounded" id="active-job-${jid}">
            <div class="d-flex justify-content-between align-items-center mb-2">
                <div>
                    <strong>Job:</strong> <code>${jid.substring(0, 8)}</code>
                    <span class="ms-2">${statusBadge}</span>${activePriBadge}
                    <button class="btn btn-sm btn-outline-info ms-2" onclick="showLogsModal('${jid}')" title="View Logs">
                        <i class="bi bi-file-text"></i>
                    </button>
                </div>
                <button class="btn btn-sm btn-outline-danger" onclick="cancelJob('${jid}')">
                    <i class="bi bi-x me-1"></i>Cancel
                </button>
            </div>
            <div class="mb-2 small">
                <strong>Library:</strong> ${escapeHtml(job.library_name) || 'All Libraries'}${webhookFilesHtml}
            </div>
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
let logsRefreshInterval = null;

function updateWorkerStatuses(workers, options = {}) {
    const {
        fallbackCounts = null,
        keepBadgeCounts = false
    } = options;
    const container = document.getElementById('workerStatusContainer');
    const cpuWorkersEl = document.getElementById('cpuWorkers');
    const cpuFallbackWorkersEl = document.getElementById('cpuFallbackWorkers');

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
            if (cpuFallbackWorkersEl) cpuFallbackWorkersEl.textContent = String(counts.cpu_fallback_threads);
        }
        refreshWorkerScaleButtons();
        return;
    }

    const cpuCount = workers.filter(w => w.worker_type === 'CPU').length;
    const cpuFallbackCount = workers.filter(w => w.worker_type === 'CPU_FALLBACK').length;
    if (cpuWorkersEl) cpuWorkersEl.textContent = String(cpuCount);
    if (cpuFallbackWorkersEl) cpuFallbackWorkersEl.textContent = String(cpuFallbackCount);

    let html = '<div class="row g-3">';

    for (const worker of workers) {
        const icon = worker.worker_type === 'GPU' ? 'bi-gpu-card' : 'bi-cpu';
        const statusColor = worker.status === 'processing' ? 'primary' : 'secondary';
        const progressPercent = (worker.progress_percent || 0).toFixed(1);

        html += `
            <div class="col-md-6">
                <div class="card bg-dark">
                    <div class="card-body py-2">
                        <div class="d-flex justify-content-between align-items-center mb-2">
                            <span><i class="bi ${icon} me-2"></i>${escapeHtml(worker.worker_name)}</span>
                            <span class="badge bg-${statusColor}">${escapeHtml(worker.status)}</span>
                        </div>
                        ${worker.status === 'processing' ? `
                            <div class="small text-truncate mb-1" title="${escapeHtml(worker.current_title)}">
                                ${worker.library_name ? `<span class="text-muted">${escapeHtml(worker.library_name)}</span> <i class="bi bi-chevron-right small text-muted"></i> ` : ''}${escapeHtml(worker.current_title) || 'Processing...'}
                            </div>
                            <div class="progress" style="height: 6px;">
                                <div class="progress-bar" style="width: ${progressPercent}%"></div>
                            </div>
                            <div class="d-flex justify-content-between small text-muted mt-1">
                                <span>${progressPercent}%</span>
                                <span>${escapeHtml(worker.speed)}</span>
                                <span>ETA: ${escapeHtml(worker.eta) || '-'}</span>
                            </div>
                        ` : `
                            <div class="small text-muted">Idle - waiting for task</div>
                        `}
                    </div>
                </div>
            </div>
        `;
    }

    html += '</div>';
    container.innerHTML = html;
    refreshWorkerScaleButtons();
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

// Logs Functions
let _rawLogs = [];
let _logsModalJobId = null;
let _logsTotalLines = 0;
let _logsLoadedOffset = 0;
let _logsKnownCount = 0;
const _LOGS_CHUNK_SIZE = 500;

function showLogsModal(jobId) {
    const targetId = jobId || _lastNotifiedJobId;
    if (!targetId) return;
    _logsModalJobId = targetId;

    document.getElementById('logsJobId').textContent = `Job ID: ${targetId}`;
    document.getElementById('logsSearchInput').value = '';

    _rawLogs = [];
    _logsTotalLines = 0;
    _logsLoadedOffset = 0;
    _logsKnownCount = 0;
    _updateEarlierLogsButton();

    // Reset Files tab state
    _fileResultsActiveFilter = '';
    _fileResultsLoaded = false;
    _filePage = 1;
    _fileSummary = {};
    document.getElementById('fileResultsSummary').innerHTML = '';
    document.getElementById('fileResultsBody').innerHTML =
        '<tr><td colspan="4" class="text-muted text-center">Click to load file results</td></tr>';
    document.getElementById('fileResultsCount').textContent = '';
    document.getElementById('fileResultsSearch').value = '';
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
            var filesPane = document.getElementById('filesTabPane');
            if (filesPane && filesPane.classList.contains('active')) {
                refreshFileResults();
            }
        }, 5000);
    }

    document.getElementById('logsModal').addEventListener('hidden.bs.modal', function() {
        if (logsRefreshInterval) {
            clearInterval(logsRefreshInterval);
            logsRefreshInterval = null;
        }
        _logsModalJobId = null;
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
    const targetId = _logsModalJobId;
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
    const targetId = _logsModalJobId;
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
    const targetId = _logsModalJobId;
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
    const targetId = _logsModalJobId;
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
    const targetId = _logsModalJobId;
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
var _fileSummary = {};
var _fileSearchDebounce = null;

var FILE_OUTCOME_META = {
    'generated':              { label: 'Generated',     badge: 'bg-success' },
    'skipped_bif_exists':     { label: 'Already Existed', badge: 'bg-info text-dark' },
    'failed':                 { label: 'Failed',        badge: 'bg-danger' },
    'skipped_file_not_found': { label: 'Not Found',     badge: 'bg-warning text-dark' },
    'skipped_excluded':       { label: 'Excluded',      badge: 'bg-secondary' },
    'skipped_invalid_hash':   { label: 'Invalid Hash',  badge: 'bg-warning text-dark' },
    'no_media_parts':         { label: 'No Media Parts', badge: 'bg-light text-dark' },
    'unresolved_plex':        { label: 'Not In Plex',    badge: 'bg-danger' },
};

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
        _fileSummary = data.summary || {};

        renderFileResultsSummary(_fileSummary, _fileTotal);
        renderFileResultsTable(data.files || []);
        renderFilePagination();
    } catch (e) {
        document.getElementById('fileResultsBody').innerHTML =
            '<tr><td colspan="4" class="text-muted text-center">Could not load file results</td></tr>';
    }
}

function renderFileResultsSummary(summary, total) {
    var container = document.getElementById('fileResultsSummary');
    if (!summary || Object.keys(summary).length === 0) {
        container.innerHTML = '<span class="text-muted">No file results recorded for this job</span>';
        return;
    }
    var html = '';

    var ordered = [
        'generated', 'skipped_bif_exists', 'failed',
        'skipped_file_not_found', 'skipped_excluded',
        'skipped_invalid_hash', 'no_media_parts',
        'unresolved_plex'
    ];
    for (var i = 0; i < ordered.length; i++) {
        var key = ordered[i];
        var count = summary[key];
        if (!count) continue;
        var meta = FILE_OUTCOME_META[key] || { label: key, badge: 'bg-secondary' };
        var btnClass = (_fileResultsActiveFilter === key)
            ? meta.badge.replace('bg-', 'btn-outline-').replace(' text-dark', '')
            : meta.badge.replace('bg-', 'btn-').replace(' text-dark', '');
        html += '<button class="btn btn-sm ' + btnClass + '" onclick="toggleFileOutcomeFilter(\'' + key + '\')">'
            + meta.label + ': ' + count + '</button>';
    }
    container.innerHTML = html;
}

function renderFileResultsTable(files) {
    var tbody = document.getElementById('fileResultsBody');
    var countEl = document.getElementById('fileResultsCount');

    if (!files || files.length === 0) {
        tbody.innerHTML = '<tr><td colspan="4" class="text-muted text-center">No matching files</td></tr>';
        countEl.textContent = _fileFilteredCount === 0 && _fileTotal > 0
            ? '0 of ' + _fileTotal + ' files match'
            : '';
        return;
    }

    var start = (_filePage - 1) * _filePerPage + 1;
    var end = start + files.length - 1;
    var label = 'Showing ' + start + '\u2013' + end + ' of ' + _fileFilteredCount.toLocaleString();
    if (_fileFilteredCount !== _fileTotal) label += ' (' + _fileTotal.toLocaleString() + ' total)';
    countEl.textContent = label;

    var html = '';
    for (var i = 0; i < files.length; i++) {
        var f = files[i];
        var meta = FILE_OUTCOME_META[f.outcome] || { label: f.outcome, badge: 'bg-secondary' };
        var fileName = f.file || '';
        var shortName = fileName.split('/').pop() || fileName;
        var reason = escapeHtml(f.reason || '');
        var worker = escapeHtml(f.worker || '');

        html += '<tr>'
            + '<td class="text-truncate" style="max-width: 400px;" title="' + escapeHtml(fileName) + '">'
            + '<small>' + escapeHtml(shortName) + '</small></td>'
            + '<td><span class="badge ' + meta.badge + '">' + meta.label + '</span></td>'
            + '<td><small class="text-muted" title="' + reason + '">' + reason + '</small></td>'
            + '<td><small class="text-muted">' + worker + '</small></td>'
            + '</tr>';
    }
    tbody.innerHTML = html;
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
    info.textContent = 'Showing ' + start + '\u2013' + end + ' of ' + _fileFilteredCount;

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

function toggleFileOutcomeFilter(outcome) {
    _fileResultsActiveFilter = (_fileResultsActiveFilter === outcome) ? '' : outcome;
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
        const dayNums = dow.split(',').map(Number);
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

function updateScheduleList() {
    const tbody = document.getElementById('scheduleList');

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

        html += `
            <tr>
                <td>${escapeHtml(schedule.name)}</td>
                <td>${escapeHtml(schedule.library_name) || 'All Libraries'}</td>
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

// Action Functions
function showNewJobModal() {
    // Populate library checkboxes
    const libraryList = document.getElementById('jobLibraryList');
    if (libraries.length > 0) {
        libraryList.innerHTML = libraries.map(lib => `
            <div class="form-check">
                <input class="form-check-input job-library-checkbox" type="checkbox"
                       value="${lib.id}" id="jobLib_${lib.id}" disabled>
                <label class="form-check-label" for="jobLib_${lib.id}">
                    ${lib.name} <span class="text-muted small">(${lib.type})</span>
                </label>
            </div>
        `).join('');
    } else {
        libraryList.innerHTML = '<div class="text-muted small">No libraries found</div>';
    }

    // Reset "All Libraries" checkbox to checked
    document.getElementById('jobLibraryAll').checked = true;

    const modal = new bootstrap.Modal(document.getElementById('newJobModal'));
    modal.show();
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

async function startNewJob() {
    const allLibrariesCheckbox = document.getElementById('jobLibraryAll');
    const forceRegenerate = document.getElementById('jobRegenerateAll').checked;

    let selectedLibraryNames = [];
    let libraryName = 'All Libraries';

    if (!allLibrariesCheckbox.checked) {
        // Get selected library checkboxes
        const selectedCheckboxes = document.querySelectorAll('.job-library-checkbox:checked');
        const selectedIds = Array.from(selectedCheckboxes).map(cb => cb.value);

        if (selectedIds.length === 0) {
            showToast('Error', 'Please select at least one library', 'warning');
            return;
        }

        // Convert IDs to library names (lowercase for config matching)
        selectedLibraryNames = selectedIds.map(id => {
            const lib = libraries.find(l => l.id === id);
            return lib ? lib.name.toLowerCase() : null;
        }).filter(n => n !== null);

        if (selectedIds.length === 1) {
            const lib = libraries.find(l => l.id === selectedIds[0]);
            libraryName = lib ? lib.name : 'Selected Library';
        } else {
            libraryName = `${selectedIds.length} Libraries`;
        }
    }

    const priority = parseInt(document.getElementById('jobPriority').value, 10) || 2;

    const jobPayload = {
        library_names: selectedLibraryNames.length > 0 ? selectedLibraryNames : null,
        library_name: libraryName,
        priority: priority,
        config: {
            force_generate: forceRegenerate
        }
    };

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

function showManualTriggerModal() {
    document.getElementById('manualFilePaths').value = '';
    document.getElementById('manualForceRegenerate').checked = false;
    document.getElementById('manualPriority').value = '2';
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

    try {
        await apiPost('/api/jobs/manual', {
            file_paths: paths,
            force_regenerate: forceRegenerate,
            priority: manualPriority
        });
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
    const id = workerType === 'CPU' ? 'cpuWorkers' : 'cpuFallbackWorkers';
    const el = document.getElementById(id);
    if (!el) return 0;
    const n = parseInt(el.textContent, 10);
    return Number.isNaN(n) ? 0 : n;
}

function settingsKeyForWorkerType(workerType) {
    if (workerType === 'CPU') return 'cpu_threads';
    return 'cpu_fallback_threads';
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

        const badgeEl = document.getElementById(
            workerType === 'CPU' ? 'cpuWorkers' : 'cpuFallbackWorkers'
        );
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
        const badgeEl = document.getElementById(
            workerType === 'CPU' ? 'cpuWorkers' : 'cpuFallbackWorkers'
        );
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

function onScheduleTypeChange() {
    const selected = document.querySelector('input[name="scheduleType"]:checked').value;
    document.getElementById('scheduleFieldsTime').classList.toggle('d-none', selected !== 'specific-time');
    document.getElementById('scheduleFieldsInterval').classList.toggle('d-none', selected !== 'interval');
    document.getElementById('scheduleFieldsCron').classList.toggle('d-none', selected !== 'cron');
}

function _getSelectedScheduleType() {
    return document.querySelector('input[name="scheduleType"]:checked').value;
}

function _resetScheduleForm() {
    document.getElementById('scheduleName').value = '';
    document.getElementById('scheduleLibrary').value = '';
    document.getElementById('scheduleCron').value = '';
    document.getElementById('scheduleEditId').value = '';
    document.getElementById('scheduleEnabled').checked = true;
    document.getElementById('schedulePriority').value = '2';

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
    document.getElementById('scheduleLibrary').value = schedule.library_id || '';
    document.getElementById('scheduleEnabled').checked = schedule.enabled !== false;
    document.getElementById('schedulePriority').value = String(schedule.priority || 2);

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
            const cronDays = parts[4].split(',').map(d => d.trim());
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
    const libraryId = document.getElementById('scheduleLibrary').value;
    const library = libraries.find(l => l.id === libraryId);

    const payload = {
        name: name,
        library_id: libraryId || null,
        library_name: library ? library.name : 'All Libraries',
        enabled: document.getElementById('scheduleEnabled').checked,
        priority: parseInt(document.getElementById('schedulePriority').value, 10) || 2
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
        payload.cron_expression = `${parseInt(minutes)} ${parseInt(hours)} * * ${selectedDays.join(',')}`;
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

// Helper Functions
function _buildOutcomeTooltip(outcome) {
    if (!outcome || typeof outcome !== 'object') return '';
    var labels = {
        'generated': 'Generated',
        'skipped_bif_exists': 'Already existed',
        'skipped_file_not_found': 'File not found',
        'skipped_excluded': 'Excluded',
        'skipped_invalid_hash': 'Invalid hash',
        'failed': 'Failed',
        'no_media_parts': 'No media parts'
    };
    var lines = [];
    var keys = ['generated', 'skipped_bif_exists', 'skipped_file_not_found',
                'skipped_excluded', 'skipped_invalid_hash', 'failed', 'no_media_parts'];
    for (var i = 0; i < keys.length; i++) {
        var count = outcome[keys[i]];
        if (count && count > 0) {
            lines.push(labels[keys[i]] + ': ' + count.toLocaleString());
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

    if (status === 'running' && paused) {
        return '<span class="badge bg-warning text-dark"' + tooltipAttrs + '>Paused</span>';
    }
    if (status === 'completed' && error) {
        return '<span class="badge bg-warning text-dark"' + tooltipAttrs + '>Completed with warnings</span>';
    }
    var badgeMap = {
        'pending': 'bg-secondary',
        'running': 'bg-primary pulse',
        'completed': 'bg-success',
        'failed': 'bg-danger',
        'cancelled': 'bg-warning text-dark'
    };
    var labelMap = {
        'pending': 'Pending',
        'running': 'Running',
        'completed': 'Completed',
        'failed': 'Failed',
        'cancelled': 'Cancelled'
    };
    var cls = badgeMap[status] || 'bg-secondary';
    var label = labelMap[status] || status;
    return '<span class="badge ' + cls + '"' + tooltipAttrs + '>' + label + '</span>';
}

function formatDate(dateStr) {
    if (!dateStr) return '-';
    const date = new Date(dateStr);
    return date.toLocaleString();
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
    let html = escapeHtml(md);
    html = html.replace(/^### (.+)$/gm, '<h6 class="mt-3 mb-1">$1</h6>');
    html = html.replace(/^## (.+)$/gm, '<h5 class="mt-3 mb-1">$1</h5>');
    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    html = html.replace(/^\* (.+)$/gm, '<li>$1</li>');
    html = html.replace(/^- (.+)$/gm, '<li>$1</li>');
    html = html.replace(/(<li>.*<\/li>\n?)+/g, function (m) { return '<ul class="mb-2">' + m + '</ul>'; });
    html = html.replace(/\n{2,}/g, '<br><br>');
    html = html.replace(/\n/g, '<br>');
    return html;
}

// checkWhatsNew() is called from the dashboard page (index.html) only,
// to avoid hitting the GitHub API on every page navigation.
