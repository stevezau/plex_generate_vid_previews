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
let cachedWorkerConfigCounts = null;
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
    // Request notification permission
    requestNotificationPermission();

    // Set up auto-refresh
    // System status includes cached GPU detection — poll less frequently
    setInterval(refreshStatus, 120000);
    setInterval(loadJobStats, 10000);
    setInterval(loadJobs, 5000);  // Poll jobs every 5 seconds
    setInterval(loadWorkerStatuses, 1000);  // Poll workers every second
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

    socket.on('job_started', function(job) {
        console.log('Job started:', job);
        loadJobs();
        loadJobStats();
        updateCurrentJob(job);
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
        clearCurrentJob();
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
        clearCurrentJob();
        showToast('Job Failed', `Job ${job.id.substring(0, 8)} failed: ${job.error}`, 'danger');
        showNotification('Job Failed', job.error || 'Unknown error', 'error');
    });

    socket.on('job_cancelled', function(job) {
        console.log('Job cancelled:', job);
        loadJobs();
        loadJobStats();
        loadWorkerStatuses();
        clearCurrentJob();
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

        // Update the header GPU badges
        const gpuStatus = document.getElementById('gpuStatus');
        if (gpuStatus) {
            if (status.gpus && status.gpus.length > 0) {
                gpuStatus.innerHTML = status.gpus.map(g =>
                    `<span class="badge bg-success me-1">${escapeHtml(g.name)}</span>`
                ).join('');
            } else {
                gpuStatus.innerHTML = '<span class="text-muted">No GPU detected - using CPU</span>';
            }
        }
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
    } catch (e) {
        console.warn('Failed to load worker config:', e);
    }
}

function normalizeWorkerConfigCounts(config) {
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
        // Use bare fetch (not apiGet) to avoid 401 redirect side-effects.
        const resp = await fetch('/api/system/config');
        if (resp.ok) {
            const config = await resp.json();
            cachedWorkerConfigCounts = normalizeWorkerConfigCounts(config);
            return cachedWorkerConfigCounts;
        }
    } catch (e) {
        console.warn('Failed to cache worker config counts:', e);
    }

    return cachedWorkerConfigCounts;
}

async function loadLibraries() {
    try {
        const data = await apiGet('/api/libraries');
        libraries = data.libraries || [];
        updateLibraryList();
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

        // Update current job if there's a running one
        const runningJob = jobs.find(j => j.status === 'running');
        if (runningJob) {
            currentJobId = runningJob.id;
            _lastNotifiedJobId = null;
            updateCurrentJob(runningJob);
            document.getElementById('viewLogsBtn').style.display = 'inline-block';
        } else {
            // Check if a job just completed (notify only once)
            if (currentJobId && currentJobId !== _lastNotifiedJobId) {
                const completedJob = jobs.find(j => j.id === currentJobId);
                if (completedJob && completedJob.status !== 'running') {
                    _lastNotifiedJobId = currentJobId;
                    if (completedJob.status === 'completed') {
                        if (completedJob.error) {
                            showNotification('Job completed with warnings', completedJob.error, 'warning');
                        } else {
                            showNotification('Job Completed', `Job ${currentJobId.substring(0, 8)} finished successfully`, 'success');
                        }
                    } else if (completedJob.status === 'failed') {
                        showNotification('Job Failed', `Job ${currentJobId.substring(0, 8)} failed: ${completedJob.error || 'Unknown error'}`, 'error');
                    }
                }
            }
            clearCurrentJob();
            currentJobId = null;
        }
    } catch (error) {
        console.error('Failed to load jobs:', error);
        // Show empty state instead of error - jobs list may just be unavailable temporarily
        const tbody = document.getElementById('jobQueue');
        if (tbody && !error.message.includes('Authentication')) {
            // Show a less alarming message
            tbody.innerHTML = `
                <tr>
                    <td colspan="6" class="text-center text-muted py-4">
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

    // GPU Info
    if (status.gpus && status.gpus.length > 0) {
        html += '<div class="mb-3">';
        html += '<h6><i class="bi bi-gpu-card me-2"></i>GPUs</h6>';
        for (const gpu of status.gpus) {
            html += `<div class="d-flex align-items-center mb-1">`;
            html += `<span class="badge bg-primary me-2">${escapeHtml(gpu.type).toUpperCase()}</span>`;
            html += `<span>${escapeHtml(gpu.name)}</span>`;
            html += `</div>`;
        }
        html += '</div>';
    } else {
        html += '<div class="mb-3">';
        html += '<h6><i class="bi bi-cpu me-2"></i>Processing</h6>';
        html += '<span class="text-muted">CPU only (no GPU detected)</span>';
        html += '</div>';
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
}

function updateLibraryList() {
    let html = '';

    if (libraries.length === 0) {
        html = '<div class="text-muted small">No libraries found</div>';
    } else {
        for (const lib of libraries) {
            const icon = lib.type === 'movie' ? 'bi-film' : 'bi-tv';
            const typeLabel = lib.type === 'movie' ? 'Movies' : 'TV Shows';

            html += `
                <div class="library-item">
                    <span class="library-name">
                        <i class="bi ${icon} me-2"></i>${escapeHtml(lib.name)}
                    </span>
                    <span class="library-count">${typeLabel}</span>
                </div>
            `;
        }
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

function toggleCurrentJobFiles() {
    const listEl = document.getElementById('currentJobFilesList');
    const iconEl = document.getElementById('currentJobFilesIcon');
    const btn = document.getElementById('currentJobFilesToggle');
    if (!listEl || !iconEl || !btn) return;
    const isExpanded = !listEl.classList.contains('d-none');
    listEl.classList.toggle('d-none');
    iconEl.classList.toggle('bi-chevron-down', isExpanded);
    iconEl.classList.toggle('bi-chevron-up', !isExpanded);
    btn.setAttribute('aria-expanded', isExpanded ? 'false' : 'true');
}

function updateJobQueue() {
    const tbody = document.getElementById('jobQueue');

    if (jobs.length === 0) {
        const msg = jobTotal === 0 ? 'No jobs in queue' : 'No jobs on this page';
        tbody.innerHTML = `
            <tr>
                <td colspan="6" class="text-center text-muted py-4">
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
        const statusBadge = getStatusBadge(job.status, job.paused, job.error);
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

        const webhookBasenames = job.config && Array.isArray(job.config.webhook_basenames) && job.config.webhook_basenames.length > 0
            ? job.config.webhook_basenames
            : [];
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
        html += `
            <tr id="job-row-${escapeHtml(job.id)}">
                <td><code>${escapeHtml(job.id.substring(0, 8))}</code></td>
                <td${libraryTitle}>${escapeHtml(job.library_name) || 'All Libraries'}${retryLabel}${filesToggleBtn}</td>
                <td>${statusBadge}</td>
                <td>
                    <div class="progress" style="height: 20px;">
                        <div class="progress-bar" role="progressbar"
                             style="width: ${progress}%">${progress}%</div>
                    </div>
                </td>
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
                <td colspan="6" class="bg-dark bg-opacity-10 small py-2 ps-4">
                    <strong>Files:</strong>
                    <div class="mt-1">${filesList}${overflow}</div>
                </td>
            </tr>
            `;
        }
    }

    tbody.innerHTML = html;
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

function updateCurrentJob(job) {
    const card = document.getElementById('currentJobCard');
    const statusBadge = document.getElementById('currentJobStatus');

    const isPaused = !!job.paused;
    statusBadge.textContent = isPaused ? 'Paused' : 'Running';
    statusBadge.className = `badge ${isPaused ? 'bg-warning text-dark' : 'bg-primary pulse'}`;

    const progress = job.progress.percent.toFixed(1);

    const webhookFiles = job.config && Array.isArray(job.config.webhook_basenames) && job.config.webhook_basenames.length > 0
        ? job.config.webhook_basenames
        : null;
    const fileCount = webhookFiles ? webhookFiles.length : 0;
    const pathCount = (job.config && typeof job.config.path_count === 'number') ? job.config.path_count : fileCount;
    const showExpandableFiles = fileCount > 8;
    const previewCount = 8;
    let webhookFilesLine = '';
    if (webhookFiles && webhookFiles.length > 0) {
        if (showExpandableFiles) {
            const preview = webhookFiles.slice(0, previewCount).map(function (b) { return escapeHtml(b); }).join(', ');
            const overflowCount = pathCount > previewCount ? pathCount - previewCount : fileCount - previewCount;
            webhookFilesLine = `<br><strong>Files:</strong> <span class="text-muted small">${preview} (+${overflowCount} more)</span>
                <button type="button" class="btn btn-sm btn-link p-0 ms-1 align-baseline" onclick="toggleCurrentJobFiles()" id="currentJobFilesToggle" title="Show all files">
                    <i class="bi bi-chevron-down" id="currentJobFilesIcon"></i>
                </button>
                <div id="currentJobFilesList" class="d-none small text-muted mt-1 ms-3" style="max-height: 12rem; overflow-y: auto;">${webhookFiles.map(function (b) { return escapeHtml(b); }).join('<br>')}</div>`;
        } else {
            webhookFilesLine = `<br><strong>Files:</strong> <span class="text-muted small">${webhookFiles.map(function (b) { return escapeHtml(b); }).join(', ')}</span>`;
        }
    }
    card.innerHTML = `
        <div class="mb-3">
            <strong>Job ID:</strong> <code>${escapeHtml(job.id)}</code>
            <br>
            <strong>Library:</strong> ${escapeHtml(job.library_name) || 'All Libraries'}${webhookFilesLine}
        </div>
        <div class="progress" style="height: 30px;">
            <div class="progress-bar progress-bar-striped progress-bar-animated"
                 role="progressbar" style="width: ${progress}%" id="currentJobProgress">
                ${progress}%
            </div>
        </div>
        <div class="progress-info mt-2">
            <span id="currentJobItem">${escapeHtml(job.progress.current_item) || 'Starting...'}</span>
            <span id="currentJobSpeed">${escapeHtml(job.progress.speed) || ''}</span>
        </div>
        <div class="mt-2 text-muted small">
            <span id="currentJobItems">Items: ${escapeHtml(job.progress.processed_items) || 0} / ${escapeHtml(job.progress.total_items) || '?'}</span>
        </div>
        <div class="mt-3">
            <button class="btn btn-sm btn-outline-danger" onclick="cancelJob('${escapeHtml(job.id)}')">
                <i class="bi bi-x me-1"></i>Cancel
            </button>
        </div>
    `;
    refreshWorkerScaleButtons();
}

function updateJobProgress(jobId, progress) {
    // Update current job card if this is the running job
    const progressBar = document.getElementById('currentJobProgress');
    if (progressBar) {
        const percent = progress.percent.toFixed(1);
        progressBar.style.width = `${percent}%`;
        progressBar.textContent = `${percent}%`;
    }

    const itemEl = document.getElementById('currentJobItem');
    if (itemEl && progress.current_item) {
        itemEl.textContent = progress.current_item;
    }

    const speedEl = document.getElementById('currentJobSpeed');
    if (speedEl && progress.speed) {
        speedEl.textContent = progress.speed;
    }

    // Update items count
    const itemsEl = document.getElementById('currentJobItems');
    if (itemsEl) {
        itemsEl.textContent = `Items: ${progress.processed_items || 0} / ${progress.total_items || '?'}`;
    }

    // Update job queue row
    const row = document.getElementById(`job-row-${jobId}`);
    if (row) {
        const progressBar = row.querySelector('.progress-bar');
        if (progressBar) {
            const percent = progress.percent.toFixed(1);
            progressBar.style.width = `${percent}%`;
            progressBar.textContent = `${percent}%`;
        }
    }
}

function clearCurrentJob() {
    const card = document.getElementById('currentJobCard');
    const statusBadge = document.getElementById('currentJobStatus');

    statusBadge.textContent = 'No active job';
    statusBadge.className = 'badge bg-secondary';

    card.innerHTML = `
        <div class="text-muted text-center py-4">
            <i class="bi bi-inbox fs-1 d-block mb-2"></i>
            No job is currently running
        </div>
    `;
    // Hide logs button when no job
    document.getElementById('viewLogsBtn').style.display = 'none';
    refreshWorkerScaleButtons();
}

// Worker Status Functions
let currentJobId = null;
let logsRefreshInterval = null;

function updateWorkerStatuses(workers, options = {}) {
    const {
        fallbackCounts = null,
        keepBadgeCounts = false
    } = options;
    const container = document.getElementById('workerStatusContainer');
    const gpuWorkersEl = document.getElementById('gpuWorkers');
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
            if (gpuWorkersEl) gpuWorkersEl.textContent = String(counts.gpu_threads);
            if (cpuWorkersEl) cpuWorkersEl.textContent = String(counts.cpu_threads);
            if (cpuFallbackWorkersEl) cpuFallbackWorkersEl.textContent = String(counts.cpu_fallback_threads);
        }
        refreshWorkerScaleButtons();
        return;
    }

    const gpuCount = workers.filter(w => w.worker_type === 'GPU').length;
    const cpuCount = workers.filter(w => w.worker_type === 'CPU').length;
    const cpuFallbackCount = workers.filter(w => w.worker_type === 'CPU_FALLBACK').length;
    if (gpuWorkersEl) gpuWorkersEl.textContent = String(gpuCount);
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
                                ${escapeHtml(worker.current_title) || 'Processing...'}
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
        const hasRunningJob = jobs.some(j => j.status === 'running');

        if (workers.length === 0) {
            if (hasRunningJob || !jobsLoadedOnce) {
                // During startup/polls a running job can briefly report no
                // worker telemetry; avoid replacing badges with config defaults.
                // Also skip fallback on first load when we haven't fetched jobs
                // yet — prevents a brief flash of config defaults (e.g. "1").
                updateWorkerStatuses([], { keepBadgeCounts: true });
                return;
            }
            const fallbackCounts = await loadWorkerConfigCounts(false);
            updateWorkerStatuses([], { fallbackCounts });
            return;
        }

        updateWorkerStatuses(workers);
    } catch (error) {
        console.error('Failed to load worker statuses:', error);
        // If not an auth error, show empty state (no workers) rather than error
        const container = document.getElementById('workerStatusContainer');
        if (container && !error.message.includes('Authentication')) {
            // Check if there's a running job - if so, show error; if not, show empty state
            const runningJob = jobs.find(j => j.status === 'running');
            if (runningJob) {
                container.innerHTML = `
                    <div class="text-warning text-center py-3">
                        <i class="bi bi-exclamation-triangle me-2"></i>Worker status temporarily unavailable
                    </div>
                `;
            } else {
                // No running job, so no workers expected - show idle state
                const fallbackCounts = await loadWorkerConfigCounts(false);
                updateWorkerStatuses([], { fallbackCounts });
            }
        }
    }
}

// Logs Functions
let _rawLogs = [];
let _logsModalJobId = null;

function showLogsModal(jobId) {
    const targetId = jobId || currentJobId || _lastNotifiedJobId;
    if (!targetId) return;
    _logsModalJobId = targetId;

    document.getElementById('logsJobId').textContent = `Job ID: ${targetId}`;
    document.getElementById('logsSearchInput').value = '';

    const job = jobs.find(j => j.id === targetId);
    const isRunning = job && job.status === 'running';
    const autoScrollEl = document.getElementById('logsAutoScroll');
    autoScrollEl.checked = isRunning;

    const modal = new bootstrap.Modal(document.getElementById('logsModal'));
    modal.show();

    refreshLogs();

    if (logsRefreshInterval) clearInterval(logsRefreshInterval);
    if (isRunning) {
        logsRefreshInterval = setInterval(refreshLogs, 5000);
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
    const targetId = _logsModalJobId || currentJobId;
    if (!targetId) return;

    try {
        const data = await apiGet(`/api/jobs/${targetId}/logs`);
        const logsContent = document.getElementById('logsContent');
        const lineCountEl = document.getElementById('logsLineCount');
        const autoScroll = document.getElementById('logsAutoScroll').checked;

        if (data.log_cleared_by_retention) {
            _rawLogs = [];
            logsContent.innerHTML = [
                '<div class="alert alert-info mb-0" role="alert">',
                '<i class="bi bi-info-circle me-2"></i>',
                'Log file was cleared due to log retention policy.',
                '</div>'
            ].join('');
            if (lineCountEl) lineCountEl.textContent = '';
        } else if (data.logs && data.logs.length > 0) {
            _rawLogs = data.logs;
            logsContent.innerHTML = data.logs.map(colorizeLogLine).join('\n');
            filterLogs();
            if (lineCountEl) {
                lineCountEl.textContent = `${data.logs.length.toLocaleString()} log lines`;
            }
        } else {
            _rawLogs = [];
            logsContent.innerHTML = '<span class="text-muted">No logs available yet...</span>';
            if (lineCountEl) lineCountEl.textContent = '';
        }

        if (autoScroll) {
            _suppressScrollDetect = Date.now() + 600;
            logsContent.scrollTo({ top: logsContent.scrollHeight, behavior: 'smooth' });
        }
    } catch (error) {
        console.error('Failed to load logs:', error);
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
    const renderedLogs = Array.from(document.querySelectorAll('#logsContent .log-line'))
        .filter((line) => !line.classList.contains('log-line-hidden'))
        .map((line) => (line.textContent || '').trim())
        .filter((line) => line.length > 0);

    const text = _rawLogs.length > 0 ? _rawLogs.join('\n') : renderedLogs.join('\n');
    if (!text.trim()) {
        showToast('Warning', 'No logs to copy', 'warning');
        return;
    }

    await copyToClipboard(text, 'Logs copied to clipboard', 'Failed to copy logs');
}

function downloadLogs() {
    if (_rawLogs.length === 0) {
        showToast('Warning', 'No logs to download', 'warning');
        return;
    }
    const text = _rawLogs.join('\n');
    const blob = new Blob([text], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `job-${currentJobId || 'unknown'}-logs.txt`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
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

function updateScheduleList() {
    const tbody = document.getElementById('scheduleList');

    if (schedules.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="6" class="text-center text-muted py-4">
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

        const cronDisplay = schedule.trigger_type === 'cron' ?
            schedule.trigger_value :
            `Every ${schedule.trigger_value} minutes`;

        const nextRun = schedule.next_run ? formatDate(schedule.next_run) : '-';

        html += `
            <tr>
                <td>${escapeHtml(schedule.name)}</td>
                <td>${escapeHtml(schedule.library_name) || 'All Libraries'}</td>
                <td><code>${escapeHtml(cronDisplay)}</code></td>
                <td>${nextRun}</td>
                <td>${statusBadge}</td>
                <td>
                    <button class="btn btn-sm btn-outline-primary me-1" onclick="runScheduleNow('${escapeHtml(schedule.id)}')" title="Run Now">
                        <i class="bi bi-play-fill"></i>
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

    const jobPayload = {
        library_names: selectedLibraryNames.length > 0 ? selectedLibraryNames : null,
        library_name: libraryName,
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
    const id = workerType === 'GPU' ? 'gpuWorkers' : workerType === 'CPU' ? 'cpuWorkers' : 'cpuFallbackWorkers';
    const el = document.getElementById(id);
    if (!el) return 0;
    const n = parseInt(el.textContent, 10);
    return Number.isNaN(n) ? 0 : n;
}

function settingsKeyForWorkerType(workerType) {
    if (workerType === 'GPU') return 'gpu_threads';
    if (workerType === 'CPU') return 'cpu_threads';
    return 'cpu_fallback_threads';
}

async function scaleWorkersGlobal(workerType, direction) {
    const currentCount = getWorkerCountForType(workerType);
    const newCount = Math.max(0, currentCount + direction);
    if (newCount === currentCount) return;

    const settingsKey = settingsKeyForWorkerType(workerType);

    try {
        await apiPost('/api/settings', { [settingsKey]: newCount });
        cachedWorkerConfigCounts = null;
        await loadWorkerConfigCounts(true);

        const badgeEl = document.getElementById(
            workerType === 'GPU' ? 'gpuWorkers' : workerType === 'CPU' ? 'cpuWorkers' : 'cpuFallbackWorkers'
        );
        if (badgeEl) badgeEl.textContent = String(newCount);
        refreshWorkerScaleButtons();

        const hasRunningJob = jobs.some(j => j.status === 'running');
        if (hasRunningJob) {
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
                console.warn('Live worker scaling failed (setting saved):', scaleErr);
            }
        } else {
            showToast('Setting Saved', `${workerType} workers set to ${newCount}`, 'success');
        }
    } catch (error) {
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

function showNewScheduleModal() {
    document.getElementById('scheduleName').value = '';
    document.getElementById('scheduleLibrary').value = '';
    document.getElementById('scheduleCron').value = '';
    document.getElementById('scheduleEnabled').checked = true;

    const modal = new bootstrap.Modal(document.getElementById('newScheduleModal'));
    modal.show();
}

async function createSchedule() {
    const name = document.getElementById('scheduleName').value;
    const libraryId = document.getElementById('scheduleLibrary').value;
    const enabled = document.getElementById('scheduleEnabled').checked;

    // Get time
    const timeValue = document.getElementById('scheduleTime').value;
    if (!timeValue) {
        showToast('Error', 'Time is required', 'danger');
        return;
    }
    const [hours, minutes] = timeValue.split(':');

    // Get selected days
    const dayCheckboxes = document.querySelectorAll('.schedule-day:checked');
    const selectedDays = Array.from(dayCheckboxes).map(cb => cb.value);

    if (selectedDays.length === 0) {
        showToast('Error', 'Select at least one day', 'danger');
        return;
    }

    // Build cron expression: minute hour * * day_of_week
    const cronExpr = `${parseInt(minutes)} ${parseInt(hours)} * * ${selectedDays.join(',')}`;

    if (!name) {
        showToast('Error', 'Name is required', 'danger');
        return;
    }

    const library = libraries.find(l => l.id === libraryId);

    try {
        await apiPost('/api/schedules', {
            name: name,
            cron_expression: cronExpr,
            library_id: libraryId || null,
            library_name: library ? library.name : 'All Libraries',
            enabled: enabled
        });

        bootstrap.Modal.getInstance(document.getElementById('newScheduleModal')).hide();
        loadSchedules();
        showToast('Schedule Created', `Schedule "${name}" created successfully`, 'success');
    } catch (error) {
        showToast('Error', 'Failed to create schedule: ' + error.message, 'danger');
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
function getStatusBadge(status, paused = false, error = null) {
    if (status === 'running' && paused) {
        return '<span class="badge bg-warning text-dark">Paused</span>';
    }
    if (status === 'completed' && error) {
        return '<span class="badge bg-warning text-dark">Completed with warnings</span>';
    }
    const badges = {
        'pending': '<span class="badge bg-secondary">Pending</span>',
        'running': '<span class="badge bg-primary pulse">Running</span>',
        'completed': '<span class="badge bg-success">Completed</span>',
        'failed': '<span class="badge bg-danger">Failed</span>',
        'cancelled': '<span class="badge bg-warning text-dark">Cancelled</span>'
    };
    return badges[status] || `<span class="badge bg-secondary">${status}</span>`;
}

function formatDate(dateStr) {
    if (!dateStr) return '-';
    const date = new Date(dateStr);
    return date.toLocaleString();
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
