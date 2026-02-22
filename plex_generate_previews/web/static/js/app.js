/**
 * Plex Preview Generator - Dashboard JavaScript
 */

// Global state
let socket = null;
let libraries = [];
let jobs = [];
let schedules = [];
let _lastNotifiedJobId = null;


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

    const textarea = document.createElement('textarea');
    textarea.value = stringValue;
    textarea.setAttribute('readonly', '');
    textarea.style.position = 'fixed';
    textarea.style.left = '-9999px';
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();

    let copied = false;
    try {
        copied = document.execCommand('copy');
    } catch (error) {
        copied = false;
    } finally {
        document.body.removeChild(textarea);
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
    // Connect to SocketIO
    connectSocket();

    // Load initial data
    refreshStatus();
    loadLibraries();
    loadJobs();
    loadSchedules();
    loadJobStats();
    loadWorkerStatuses();
    // Request notification permission
    requestNotificationPermission();

    // Set up auto-refresh
    // System status includes cached GPU detection — poll less frequently
    setInterval(refreshStatus, 120000);
    setInterval(loadJobStats, 10000);
    setInterval(loadJobs, 5000);  // Poll jobs every 5 seconds
    setInterval(loadWorkerStatuses, 5000);  // Poll workers every 5 seconds
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

    socket.on('job_completed', function(job) {
        console.log('Job completed:', job);
        loadJobs();
        loadJobStats();
        loadWorkerStatuses();
        clearCurrentJob();
        showToast('Job Completed', `Job ${job.id.substring(0, 8)} completed successfully`, 'success');
        showNotification('Job Completed', `Processing finished for ${job.library_name || 'All Libraries'}`, 'success');
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
    // Use bare fetch (not apiGet) to avoid 401-redirect side-effects
    // that could navigate the page away and abort other in-flight requests.
    try {
        const gpuEl = document.getElementById('gpuWorkers');
        const cpuEl = document.getElementById('cpuWorkers');
        if (gpuEl || cpuEl) {
            const resp = await fetch('/api/system/config');
            if (resp.ok) {
                const config = await resp.json();
                if (gpuEl) gpuEl.textContent = config.gpu_threads || 0;
                if (cpuEl) cpuEl.textContent = config.cpu_threads || 1;
            }
        }
    } catch (e) {
        console.warn('Failed to load worker config:', e);
    }
}

async function loadLibraries() {
    try {
        const data = await apiGet('/api/libraries');
        libraries = data.libraries || [];
        updateLibraryList();
        updateLibrarySelects();
    } catch (error) {
        console.error('Failed to load libraries:', error);
        document.getElementById('libraryList').innerHTML =
            '<div class="text-danger small">Failed to load libraries</div>';
    }
}

async function loadJobs() {
    try {
        const data = await apiGet('/api/jobs');
        jobs = data.jobs || [];
        updateJobQueue();

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
                        showNotification('Job Completed', `Job ${currentJobId.substring(0, 8)} finished successfully`, 'success');
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
            let countText = `${escapeHtml(lib.count)} items`;

            html += `
                <div class="library-item">
                    <span class="library-name">
                        <i class="bi ${icon} me-2"></i>${escapeHtml(lib.name)}
                    </span>
                    <span class="library-count">${countText}</span>
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

function updateJobQueue() {
    const tbody = document.getElementById('jobQueue');

    if (jobs.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="6" class="text-center text-muted py-4">
                    No jobs in queue
                </td>
            </tr>
        `;
        return;
    }

    let html = '';

    // Sort jobs: running first, then pending, then by created_at desc
    const sortedJobs = [...jobs].sort((a, b) => {
        if (a.status === 'running') return -1;
        if (b.status === 'running') return 1;
        if (a.status === 'pending' && b.status !== 'pending') return -1;
        if (b.status === 'pending' && a.status !== 'pending') return 1;
        return new Date(b.created_at) - new Date(a.created_at);
    });

    for (const job of sortedJobs) {
        const statusBadge = getStatusBadge(job.status);
        const progress = job.progress.percent.toFixed(1);
        const created = formatDate(job.created_at);

        html += `
            <tr id="job-row-${escapeHtml(job.id)}">
                <td><code>${escapeHtml(job.id.substring(0, 8))}</code></td>
                <td>${escapeHtml(job.library_name) || 'All Libraries'}</td>
                <td>${statusBadge}</td>
                <td>
                    <div class="progress" style="height: 20px;">
                        <div class="progress-bar" role="progressbar"
                             style="width: ${progress}%">${progress}%</div>
                    </div>
                </td>
                <td>${created}</td>
                <td class="text-nowrap">
                    ${job.status === 'running' || job.status === 'pending' ?
                        `<button class="btn btn-sm btn-outline-danger" onclick="cancelJob('${escapeHtml(job.id)}')" title="Cancel">
                            <i class="bi bi-x"></i>
                        </button>` :
                        `<button class="btn btn-sm btn-outline-info me-1" onclick="showLogsModal('${escapeHtml(job.id)}')" title="View Logs">
                            <i class="bi bi-file-text"></i>
                        </button>
                        <button class="btn btn-sm btn-outline-secondary" onclick="deleteJob('${escapeHtml(job.id)}')" title="Delete">
                            <i class="bi bi-trash"></i>
                        </button>`
                    }
                </td>
            </tr>
        `;
    }

    tbody.innerHTML = html;
}

function updateCurrentJob(job) {
    const card = document.getElementById('currentJobCard');
    const statusBadge = document.getElementById('currentJobStatus');

    statusBadge.textContent = 'Running';
    statusBadge.className = 'badge bg-primary pulse';

    const progress = job.progress.percent.toFixed(1);

    card.innerHTML = `
        <div class="mb-3">
            <strong>Job ID:</strong> <code>${escapeHtml(job.id)}</code>
            <br>
            <strong>Library:</strong> ${escapeHtml(job.library_name) || 'All Libraries'}
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
            <span class="ms-3" id="currentJobEta">ETA: ${escapeHtml(job.progress.eta) || 'Calculating...'}</span>
        </div>
    `;
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

    // Update ETA
    const etaEl = document.getElementById('currentJobEta');
    if (etaEl) {
        etaEl.textContent = `ETA: ${progress.eta || 'Calculating...'}`;
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
}

// Worker Status Functions
let currentJobId = null;
let logsRefreshInterval = null;

function updateWorkerStatuses(workers) {
    const container = document.getElementById('workerStatusContainer');

    if (!workers || workers.length === 0) {
        container.innerHTML = `
            <div class="text-muted text-center py-3">
                <span>No active workers</span>
            </div>
        `;
        return;
    }

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
}

async function loadWorkerStatuses() {
    try {
        const data = await apiGet('/api/jobs/workers');
        updateWorkerStatuses(data.workers || []);
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
                updateWorkerStatuses([]);
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
    const modal = new bootstrap.Modal(document.getElementById('logsModal'));
    modal.show();

    refreshLogs();

    // Auto-refresh only while the job is still running
    if (logsRefreshInterval) clearInterval(logsRefreshInterval);
    const job = jobs.find(j => j.id === targetId);
    if (job && job.status === 'running') {
        logsRefreshInterval = setInterval(refreshLogs, 5000);
    }

    // Stop auto-refresh when modal is closed
    document.getElementById('logsModal').addEventListener('hidden.bs.modal', function() {
        if (logsRefreshInterval) {
            clearInterval(logsRefreshInterval);
            logsRefreshInterval = null;
        }
        _logsModalJobId = null;
    }, { once: true });
}

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
        const autoScroll = document.getElementById('logsAutoScroll').checked;

        if (data.logs && data.logs.length > 0) {
            _rawLogs = data.logs;
            logsContent.innerHTML = data.logs.map(colorizeLogLine).join('\n');
            filterLogs();
        } else {
            _rawLogs = [];
            logsContent.innerHTML = '<span class="text-muted">No logs available yet...</span>';
        }

        if (autoScroll) {
            logsContent.scrollTop = logsContent.scrollHeight;
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

function copyLogs() {
    const text = _rawLogs.join('\n');
    copyToClipboard(text, 'Logs copied to clipboard', 'Failed to copy logs');
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

    const icon = type === 'success' ? '✅' : type === 'error' ? '❌' : 'ℹ️';

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
function getStatusBadge(status) {
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
