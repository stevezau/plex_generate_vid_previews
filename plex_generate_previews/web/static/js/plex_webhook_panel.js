// Plex-only automation surfaces (Plex Direct webhook + Recently Added Scanner)
// for the Servers > Edit Plex modal. Functions are id-based and null-safe so
// the same code can run on both /servers (modal-driven) and /automation (legacy
// shim during migration). DOM wiring runs once at DOMContentLoaded; loaders are
// also re-invoked from servers.js when the Edit modal opens for a Plex server.
//
// Phase I5: every call now scopes to the currently-edited Plex server via
// `_pwpServerId`. servers.js sets it on modal open; without it the endpoints
// fall back to the first configured Plex server (handles setup wizard).

let recentlyAddedScanners = [];
let _pwpServerId = null;

function setPlexWebhookPanelServerId(serverId) {
    _pwpServerId = serverId || null;
}

function _withServerId(url) {
    if (!_pwpServerId) return url;
    const sep = url.includes('?') ? '&' : '?';
    return `${url}${sep}server_id=${encodeURIComponent(_pwpServerId)}`;
}

function _serverIdBody(extra) {
    const body = Object.assign({}, extra || {});
    if (_pwpServerId) body.server_id = _pwpServerId;
    return body;
}

function _formatScannerInterval(mins) {
    if (!mins) return '';
    mins = parseInt(mins, 10);
    if (mins < 60) return 'every ' + mins + ' min';
    if (mins % 60 === 0) {
        const h = mins / 60;
        return 'every ' + h + ' hour' + (h === 1 ? '' : 's');
    }
    return 'every ' + mins + ' min';
}

function _formatScannerLookback(hours) {
    const h = parseFloat(hours);
    if (isNaN(h) || h <= 0) return '';
    if (h < 1) return Math.round(h * 60) + ' min lookback';
    if (h < 24) return h + 'h lookback';
    return Math.round(h / 24) + 'd lookback';
}

async function loadPlexWebhookStatus() {
    const badge = document.getElementById('plexWebhookStatusBadge');
    if (!badge) return;
    try {
        const data = await apiGet(_withServerId('/api/settings/plex_webhook/status'));
        applyPlexWebhookStatus(data);
    } catch (e) {
        console.error('Failed to load Plex webhook status:', e);
        badge.className = 'badge badge-status bg-secondary';
        badge.textContent = 'Unknown';
    }
}

function applyPlexWebhookStatus(data) {
    const input = document.getElementById('plexWebhookPublicUrl');
    if (input && !input.value) input.value = data.public_url || data.default_url || '';

    const badge = document.getElementById('plexWebhookStatusBadge');
    const registerBtn = document.getElementById('plexWebhookRegisterBtn');
    const unregisterBtn = document.getElementById('plexWebhookUnregisterBtn');
    const noPassWarn = document.getElementById('plexWebhookNoPlexPass');
    const errBox = document.getElementById('plexWebhookError');
    const warnBox = document.getElementById('plexWebhookWarning');

    if (!badge || !registerBtn || !unregisterBtn) return;

    if (noPassWarn) noPassWarn.classList.add('d-none');
    if (errBox) {
        errBox.classList.add('d-none');
        errBox.textContent = '';
    }
    if (warnBox) {
        warnBox.classList.add('d-none');
        warnBox.textContent = '';
        if (data.warning) {
            warnBox.innerHTML = '<i class="bi bi-exclamation-triangle me-1"></i>' + data.warning;
            warnBox.classList.remove('d-none');
        }
    }

    if (data.has_plex_pass === false) {
        badge.className = 'badge badge-status bg-warning text-dark';
        badge.textContent = 'No Plex Pass';
        if (noPassWarn) noPassWarn.classList.remove('d-none');
        registerBtn.disabled = true;
        unregisterBtn.classList.add('d-none');
        return;
    }

    registerBtn.disabled = false;

    if (data.error && data.error_reason !== 'plex_pass_required') {
        badge.className = 'badge badge-status bg-warning text-dark';
        badge.textContent = 'Error';
        if (errBox) {
            errBox.textContent = data.error;
            errBox.classList.remove('d-none');
        }
    }

    const regSpan = registerBtn.querySelector('span');
    if (data.registered_in_plex) {
        badge.className = 'badge badge-status bg-success';
        badge.textContent = 'Registered';
        if (regSpan) regSpan.textContent = 'Re-register with Plex';
        unregisterBtn.classList.remove('d-none');
    } else {
        if (!data.error) {
            badge.className = 'badge badge-status bg-secondary';
            badge.textContent = 'Not registered';
        }
        if (regSpan) regSpan.textContent = 'Register with Plex';
        unregisterBtn.classList.add('d-none');
    }
}

function resetPlexWebhookUrl() {
    const origin = window.location.origin;
    const input = document.getElementById('plexWebhookPublicUrl');
    if (!input) return;
    input.value = origin + '/api/webhooks/plex';
    if (typeof showToast === 'function') {
        showToast('Reset', 'URL reset to ' + origin + '/api/webhooks/plex', 'info');
    }
}

async function registerPlexWebhook() {
    const btn = document.getElementById('plexWebhookRegisterBtn');
    const input = document.getElementById('plexWebhookPublicUrl');
    if (!btn || !input) return;
    const url = input.value.trim();
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Registering…';
    try {
        await apiPost('/api/settings/plex_webhook/register', _serverIdBody({ public_url: url }));
        showToast('Success', 'Plex webhook registered.', 'success');
    } catch (e) {
        const msg = (e && e.message) ? e.message : 'Failed to register Plex webhook';
        showToast('Error', msg, 'danger');
        const errBox = document.getElementById('plexWebhookError');
        if (errBox) {
            errBox.textContent = msg;
            errBox.classList.remove('d-none');
        }
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-plus-circle me-1"></i><span>Register with Plex</span>';
        loadPlexWebhookStatus();
    }
}

async function unregisterPlexWebhook() {
    const btn = document.getElementById('plexWebhookUnregisterBtn');
    if (!btn) return;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Removing…';
    try {
        await apiPost('/api/settings/plex_webhook/unregister', _serverIdBody({}));
        showToast('Removed', 'Plex webhook removed from your account.', 'success');
    } catch (e) {
        showToast('Error', (e && e.message) || 'Failed to remove webhook', 'danger');
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-x-circle me-1"></i>Remove from Plex';
        loadPlexWebhookStatus();
    }
}

async function testPlexWebhookReachability() {
    const btn = document.getElementById('plexWebhookTestBtn');
    const result = document.getElementById('plexWebhookTestResult');
    const input = document.getElementById('plexWebhookPublicUrl');
    if (!btn || !input || !result) return;
    const url = input.value.trim();
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Testing…';
    result.textContent = '';
    result.className = 'small mt-2';
    try {
        const data = await apiPost('/api/settings/plex_webhook/test', _serverIdBody({ public_url: url }));
        if (data.success) {
            result.className = 'small mt-2 text-success';
            result.innerHTML = '<i class="bi bi-check-circle me-1"></i>Reachable (HTTP ' + data.status_code + '). Plex should be able to deliver events here.';
        } else {
            result.className = 'small mt-2 text-danger';
            result.innerHTML = '<i class="bi bi-exclamation-triangle me-1"></i>' + (data.error || ('Unexpected status ' + data.status_code));
        }
    } catch (e) {
        result.className = 'small mt-2 text-danger';
        result.innerHTML = '<i class="bi bi-exclamation-triangle me-1"></i>' + ((e && e.message) || 'Test failed');
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-broadcast-pin me-1"></i>Test reachability';
    }
}

async function loadRecentlyAddedScanners() {
    const list = document.getElementById('recentlyAddedScannerList');
    const empty = document.getElementById('recentlyAddedNoScanners');
    const createBtn = document.getElementById('recentlyAddedCreateDefaultBtn');
    const scanBtn = document.getElementById('recentlyAddedScanNowBtn');
    const badge = document.getElementById('recentlyAddedStatusBadge');
    if (!list || !badge) return;
    try {
        const data = await apiGet('/api/schedules');
        const all = (data && data.schedules) || [];
        recentlyAddedScanners = all.filter(function(s) {
            return s.config && s.config.job_type === 'recently_added';
        });

        if (recentlyAddedScanners.length === 0) {
            list.innerHTML = '';
            if (empty) empty.classList.remove('d-none');
            if (createBtn) createBtn.classList.remove('d-none');
            if (scanBtn) scanBtn.classList.add('d-none');
            badge.className = 'badge badge-status bg-secondary';
            badge.textContent = 'No scanners';
        } else {
            if (empty) empty.classList.add('d-none');
            if (createBtn) createBtn.classList.add('d-none');
            if (scanBtn) scanBtn.classList.remove('d-none');

            const anyEnabled = recentlyAddedScanners.some(function(s) { return s.enabled !== false; });
            if (anyEnabled) {
                badge.className = 'badge badge-status bg-success';
                badge.textContent = recentlyAddedScanners.length === 1
                    ? '1 scanner active'
                    : recentlyAddedScanners.length + ' scanners active';
            } else {
                badge.className = 'badge badge-status bg-secondary';
                badge.textContent = 'All disabled';
            }

            let html = '<div class="list-group list-group-flush border rounded">';
            recentlyAddedScanners.forEach(function(s) {
                const interval = s.trigger_type === 'interval'
                    ? _formatScannerInterval(s.trigger_value)
                    : (s.trigger_value ? 'cron: ' + s.trigger_value : '');
                const lookback = _formatScannerLookback((s.config || {}).lookback_hours);
                const library = s.library_name || 'All Libraries';
                const enabledBadge = s.enabled !== false
                    ? '<span class="badge bg-success">Enabled</span>'
                    : '<span class="badge bg-secondary">Disabled</span>';
                html += '<div class="list-group-item bg-transparent d-flex justify-content-between align-items-center flex-wrap gap-2">' +
                    '<div>' +
                    '<div class="fw-semibold">' + escapeHtml(s.name) + ' ' + enabledBadge + '</div>' +
                    '<div class="small text-muted">' +
                    escapeHtml(library) + ' · ' + escapeHtml(interval) +
                    (lookback ? ' · ' + escapeHtml(lookback) : '') +
                    '</div>' +
                    '</div>' +
                    '<a class="btn btn-sm btn-outline-secondary" href="/automation#schedules" ' +
                    'title="Edit on the Schedules page">' +
                    '<i class="bi bi-sliders me-1"></i>Edit' +
                    '</a>' +
                    '</div>';
            });
            html += '</div>';
            list.innerHTML = html;
        }
    } catch (e) {
        console.error('Failed to load scanner schedules:', e);
        list.innerHTML = '<div class="text-danger small"><i class="bi bi-exclamation-triangle me-1"></i>Failed to load scanners: ' + escapeHtml(e.message || 'unknown error') + '</div>';
        badge.className = 'badge badge-status bg-warning text-dark';
        badge.textContent = 'Error';
    }
}

async function createDefaultRecentlyAddedScanner() {
    const btn = document.getElementById('recentlyAddedCreateDefaultBtn');
    const result = document.getElementById('recentlyAddedScanResult');
    if (!btn || !result) return;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Creating…';
    result.textContent = '';
    result.className = 'small mt-2';
    try {
        await apiPost('/api/schedules', {
            name: 'Recently Added Scanner',
            interval_minutes: 15,
            library_id: null,
            library_name: 'All Libraries',
            enabled: true,
            priority: 2,
            config: { job_type: 'recently_added', lookback_hours: 1 },
        });
        result.className = 'small mt-2 text-success';
        result.innerHTML = '<i class="bi bi-check-circle me-1"></i>Scanner created — runs every 15 minutes with a 1 hour lookback. Customize it on the Schedules page.';
        showToast('Scanner created', 'Recently Added scanner is now running every 15 minutes.', 'success');
        loadRecentlyAddedScanners();
    } catch (e) {
        result.className = 'small mt-2 text-danger';
        result.innerHTML = '<i class="bi bi-exclamation-triangle me-1"></i>' + escapeHtml((e && e.message) || 'Failed to create scanner');
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-plus-circle me-1"></i>Create default scanner';
    }
}

async function scanAllRecentlyAddedNow() {
    const btn = document.getElementById('recentlyAddedScanNowBtn');
    const result = document.getElementById('recentlyAddedScanResult');
    if (!btn || !result) return;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Scanning…';
    result.textContent = '';
    result.className = 'small mt-2';
    try {
        const runs = recentlyAddedScanners.map(function(s) {
            return apiPost('/api/schedules/' + encodeURIComponent(s.id) + '/run', {});
        });
        await Promise.all(runs);
        result.className = 'small mt-2 text-success';
        result.innerHTML = '<i class="bi bi-check-circle me-1"></i>Triggered ' + recentlyAddedScanners.length + ' scanner run(s). Watch the Activity Log on the Automation page.';
    } catch (e) {
        result.className = 'small mt-2 text-danger';
        result.innerHTML = '<i class="bi bi-exclamation-triangle me-1"></i>' + escapeHtml((e && e.message) || 'Scan failed');
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-play-circle me-1"></i>Scan all now';
    }
}

// Wire up the buttons whenever the panel mark-up is in the DOM. Safe to call
// multiple times — listeners are registered with a guard attribute.
function _wirePlexWebhookPanel() {
    const wire = (id, evt, fn) => {
        const el = document.getElementById(id);
        if (!el || el.dataset.pwpwired === '1') return;
        el.addEventListener(evt, fn);
        el.dataset.pwpwired = '1';
    };
    wire('plexWebhookRegisterBtn', 'click', registerPlexWebhook);
    wire('plexWebhookUnregisterBtn', 'click', unregisterPlexWebhook);
    wire('plexWebhookTestBtn', 'click', testPlexWebhookReachability);
    wire('plexWebhookResetUrlBtn', 'click', resetPlexWebhookUrl);
    wire('recentlyAddedCreateDefaultBtn', 'click', createDefaultRecentlyAddedScanner);
    wire('recentlyAddedScanNowBtn', 'click', scanAllRecentlyAddedNow);
}

document.addEventListener('DOMContentLoaded', _wirePlexWebhookPanel);
