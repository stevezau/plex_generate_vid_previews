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
    input.value = origin + '/api/webhooks/incoming';
    if (typeof showToast === 'function') {
        showToast('Reset', 'URL reset to ' + origin + '/api/webhooks/incoming', 'info');
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
        const body = _serverIdBody({ public_url: url });
        await apiPost('/api/settings/plex_webhook/register', body);
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
    // The server-edit modal now surfaces a summary line + a deep-link
    // to the Schedules page; the full per-scanner list/edit UI lives
    // on /automation. We render a one-line summary into
    // #recentlyAddedScannerSummary and keep the status badge accurate.
    const summary = document.getElementById('recentlyAddedScannerSummary');
    const scanBtn = document.getElementById('recentlyAddedScanNowBtn');
    const badge = document.getElementById('recentlyAddedStatusBadge');
    if (!summary || !badge) return;
    try {
        const data = await apiGet('/api/schedules');
        const all = (data && data.schedules) || [];
        recentlyAddedScanners = all.filter(function(s) {
            if (!s.config || s.config.job_type !== 'recently_added') return false;
            if (!_pwpServerId) return true;
            const sid = s.server_id || '';
            return sid === '' || sid === _pwpServerId;
        });

        if (recentlyAddedScanners.length === 0) {
            if (scanBtn) scanBtn.classList.add('d-none');
            badge.className = 'badge badge-status bg-secondary';
            badge.textContent = 'No scanners';
            summary.innerHTML =
                '<i class="bi bi-info-circle me-1"></i>' +
                'No Recently Added scanner configured for this server yet. ' +
                'Click <strong>Configure on Schedules page</strong> below to add one.';
        } else {
            const anyEnabled = recentlyAddedScanners.some(function(s) { return s.enabled !== false; });
            if (anyEnabled) {
                badge.className = 'badge badge-status bg-success';
                badge.textContent = recentlyAddedScanners.length === 1
                    ? '1 scanner active'
                    : recentlyAddedScanners.length + ' scanners active';
                if (scanBtn) scanBtn.classList.remove('d-none');
            } else {
                badge.className = 'badge badge-status bg-secondary';
                badge.textContent = 'All disabled';
                if (scanBtn) scanBtn.classList.add('d-none');
            }

            const items = recentlyAddedScanners.map(function(s) {
                const interval = s.trigger_type === 'interval'
                    ? _formatScannerInterval(s.trigger_value)
                    : (s.trigger_value ? 'cron: ' + s.trigger_value : '');
                const lookback = _formatScannerLookback((s.config || {}).lookback_hours);
                const library = s.library_name || 'All Libraries';
                const tail = (lookback ? ' · ' + lookback : '');
                return '<li>' + escapeHtml(s.name) +
                    ' <span class="text-muted">— ' +
                    escapeHtml(library) + ' · ' + escapeHtml(interval) + escapeHtml(tail) +
                    '</span></li>';
            }).join('');
            summary.innerHTML = '<ul class="list-unstyled mb-0 small">' + items + '</ul>';
        }
    } catch (e) {
        console.error('Failed to load scanner schedules:', e);
        summary.innerHTML = '<span class="text-danger"><i class="bi bi-exclamation-triangle me-1"></i>Failed to load scanners: ' + escapeHtml(e.message || 'unknown error') + '</span>';
        badge.className = 'badge badge-status bg-warning text-dark';
        badge.textContent = 'Error';
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
    wire('recentlyAddedScanNowBtn', 'click', scanAllRecentlyAddedNow);
}

document.addEventListener('DOMContentLoaded', _wirePlexWebhookPanel);
