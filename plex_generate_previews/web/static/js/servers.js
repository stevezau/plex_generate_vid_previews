// Servers page — fetches /api/servers and drives the Add Server wizard.
//
// Stays vanilla JS / Bootstrap 5; no framework so the page mounts the same
// way as the rest of the app. CSRF token comes from the <meta> tag the base
// template renders.

(function () {
    'use strict';

    const $ = (sel, el) => (el || document).querySelector(sel);
    const $$ = (sel, el) => Array.from((el || document).querySelectorAll(sel));

    function csrfToken() {
        const meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.getAttribute('content') : '';
    }

    async function api(method, url, body) {
        const opts = {
            method,
            headers: { 'X-CSRFToken': csrfToken() },
        };
        if (body !== undefined) {
            opts.headers['Content-Type'] = 'application/json';
            opts.body = JSON.stringify(body);
        }
        const r = await fetch(url, opts);
        let data = null;
        try { data = await r.json(); } catch (_) { /* non-JSON */ }
        return { ok: r.ok, status: r.status, data };
    }

    // ---------- list rendering -------------------------------------------------
    async function loadServers() {
        const list = $('#serverList');
        list.innerHTML = '<div class="col-12 text-center text-muted py-3"><div class="spinner-border" role="status"></div></div>';
        const r = await api('GET', '/api/servers');
        if (!r.ok) {
            list.innerHTML = `<div class="col-12"><div class="alert alert-danger">Failed to load servers (HTTP ${r.status}).</div></div>`;
            return;
        }
        const servers = (r.data && r.data.servers) || [];
        if (servers.length === 0) {
            list.innerHTML = `<div class="col-12"><div class="alert alert-secondary">No media servers configured yet. Click <strong>Add Server</strong> to start.</div></div>`;
            return;
        }
        list.innerHTML = servers.map(serverCard).join('');
        $$('.delete-server-btn').forEach((btn) => {
            btn.addEventListener('click', async (ev) => {
                const id = ev.currentTarget.dataset.id;
                const name = ev.currentTarget.dataset.name;
                if (!confirm(`Delete media server "${name}"? This does not remove any files on disk.`)) return;
                const r = await api('DELETE', `/api/servers/${encodeURIComponent(id)}`);
                if (r.ok) loadServers();
                else alert(`Failed to delete: ${(r.data && r.data.error) || r.status}`);
            });
        });
        $$('.refresh-libraries-btn').forEach((btn) => {
            btn.addEventListener('click', async (ev) => {
                const id = ev.currentTarget.dataset.id;
                ev.currentTarget.disabled = true;
                ev.currentTarget.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Refreshing…';
                const r = await api('POST', `/api/servers/${encodeURIComponent(id)}/refresh-libraries`);
                if (r.ok) loadServers();
                else {
                    alert(`Failed to refresh: ${(r.data && r.data.error) || r.status}`);
                    ev.currentTarget.disabled = false;
                    ev.currentTarget.innerHTML = '<i class="bi bi-arrow-clockwise me-1"></i>Refresh libraries';
                }
            });
        });
    }

    function serverCard(server) {
        const typeBadgeColor = { plex: 'warning', emby: 'success', jellyfin: 'info' }[server.type] || 'secondary';
        const enabledBadge = server.enabled
            ? '<span class="badge bg-success">enabled</span>'
            : '<span class="badge bg-secondary">disabled</span>';
        const libCount = (server.libraries || []).length;
        const enabledLibs = (server.libraries || []).filter((l) => l.enabled).length;
        return `
            <div class="col-md-6 col-lg-4">
                <div class="card h-100">
                    <div class="card-body">
                        <div class="d-flex justify-content-between align-items-start mb-2">
                            <h5 class="card-title mb-0">${escapeHtml(server.name)}</h5>
                            <span class="badge bg-${typeBadgeColor}">${escapeHtml(server.type)}</span>
                        </div>
                        <div class="text-muted small mb-2">${escapeHtml(server.url)}</div>
                        <div class="mb-2">${enabledBadge}</div>
                        <div class="text-muted small">
                            Libraries: <strong>${enabledLibs}</strong> enabled / ${libCount} total
                        </div>
                    </div>
                    <div class="card-footer bg-transparent d-flex justify-content-between">
                        <button class="btn btn-sm btn-outline-secondary refresh-libraries-btn"
                                data-id="${escapeHtml(server.id)}">
                            <i class="bi bi-arrow-clockwise me-1"></i>Refresh libraries
                        </button>
                        <button class="btn btn-sm btn-outline-danger delete-server-btn"
                                data-id="${escapeHtml(server.id)}"
                                data-name="${escapeHtml(server.name)}">
                            <i class="bi bi-trash"></i>
                        </button>
                    </div>
                </div>
            </div>
        `;
    }

    function escapeHtml(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    // ---------- Add Server wizard ---------------------------------------------
    const wizard = {
        type: null,
        url: '',
        name: '',
        authMethod: null,
        accessToken: null,
        userId: null,
        apiKey: null,
        plexToken: null,
        plexConfigFolder: null,
        quickConnectSecret: null,
        quickConnectPoll: null,
    };

    function showStep(stepId) {
        $$('.server-step').forEach((s) => s.classList.add('d-none'));
        $('#' + stepId).classList.remove('d-none');
    }

    function resetWizard() {
        Object.keys(wizard).forEach((k) => { wizard[k] = null; });
        wizard.url = '';
        wizard.name = '';
        showStep('step-type');
        $('#serverModalTitle').textContent = 'Add Server';
        $('#serverUrl').value = '';
        $('#serverName').value = '';
        $('#authUsername').value = '';
        $('#authPassword').value = '';
        $('#authApiKey').value = '';
        $('#plexToken').value = '';
        $('#plexConfigFolder').value = '';
        $('#quickConnectCode').classList.add('d-none');
        $('#quickConnectCode').textContent = '';
        if (wizard.quickConnectPoll) {
            clearInterval(wizard.quickConnectPoll);
            wizard.quickConnectPoll = null;
        }
    }

    document.addEventListener('DOMContentLoaded', () => {
        loadServers();

        // Webhook URL display.
        const u = new URL('/api/webhooks/incoming', window.location.origin);
        $('#webhookUrl').value = u.toString();
        $('#copyWebhookUrl').addEventListener('click', () => {
            navigator.clipboard.writeText(u.toString());
            const orig = $('#copyWebhookUrl').innerHTML;
            $('#copyWebhookUrl').innerHTML = '<i class="bi bi-check2"></i> Copied';
            setTimeout(() => { $('#copyWebhookUrl').innerHTML = orig; }, 1500);
        });

        // Wizard wiring.
        document.getElementById('addServerModal').addEventListener('show.bs.modal', resetWizard);
        document.getElementById('addServerModal').addEventListener('hidden.bs.modal', resetWizard);

        $$('.server-type-btn').forEach((btn) => {
            btn.addEventListener('click', () => {
                wizard.type = btn.dataset.type;
                $('#step-connect-vendor').textContent = wizard.type[0].toUpperCase() + wizard.type.slice(1);
                showStep('step-connect');
                configureAuthForType(wizard.type);
            });
        });

        $('#step-connect-back').addEventListener('click', () => showStep('step-type'));

        $$('input[name="authMethod"]').forEach((radio) => {
            radio.addEventListener('change', () => {
                wizard.authMethod = radio.value;
                renderAuthFields();
            });
        });

        $('#step-connect-test').addEventListener('click', testConnection);
        $('#step-result-back').addEventListener('click', () => showStep('step-connect'));
        $('#step-result-save').addEventListener('click', saveServer);
        $('#quickConnectStart').addEventListener('click', startQuickConnect);
        $('#plexOAuthStart').addEventListener('click', startPlexOAuth);
    });

    // ---------- Plex OAuth + auto-discovery ------------------------------------
    async function startPlexOAuth() {
        const btn = $('#plexOAuthStart');
        btn.disabled = true;
        const origLabel = btn.innerHTML;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Waiting for plex.tv…';

        const auth = new PlexAuth({
            onSuccess: async (token) => {
                btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Discovering servers…';
                wizard.plexToken = token;
                const r = await fetch('/api/plex/servers', {
                    headers: {
                        'X-Plex-Token': token,
                        'X-CSRFToken': csrfToken(),
                    },
                });
                let data = null;
                try { data = await r.json(); } catch (_) { /* */ }
                if (!r.ok || !data || !data.servers) {
                    alert('Could not list Plex servers from plex.tv');
                    btn.disabled = false;
                    btn.innerHTML = origLabel;
                    return;
                }
                renderPlexDiscovered(data.servers);
                btn.disabled = false;
                btn.innerHTML = origLabel;
            },
            onError: (err) => {
                console.error('Plex OAuth failed', err);
                alert('Plex OAuth failed: ' + (err.message || err));
                btn.disabled = false;
                btn.innerHTML = origLabel;
            },
            onCancel: () => {
                btn.disabled = false;
                btn.innerHTML = origLabel;
            },
        });

        try {
            const pin = await auth.requestPin();
            auth.openAuthWindow(pin.auth_url);
            await auth.pollForToken(pin.id);
        } catch (err) {
            console.error('Plex OAuth flow error', err);
            alert('Plex OAuth flow error: ' + (err.message || err));
            btn.disabled = false;
            btn.innerHTML = origLabel;
        }
    }

    function renderPlexDiscovered(servers) {
        const list = $('#plexDiscoveredServers');
        if (servers.length === 0) {
            list.innerHTML = '<div class="text-muted small">No Plex servers found on your account.</div>';
            $('#plexDiscoveredList').classList.remove('d-none');
            return;
        }
        list.innerHTML = servers.map((s) => {
            const ownedBadge = s.owned ? '<span class="badge bg-success">owned</span>' : '<span class="badge bg-secondary">shared</span>';
            const localBadge = s.local ? '<span class="badge bg-info">local</span>' : '';
            return `
                <button type="button" class="list-group-item list-group-item-action plex-server-pick"
                        data-uri="${escapeHtml(s.uri || '')}"
                        data-name="${escapeHtml(s.name || '')}">
                    <div class="d-flex justify-content-between align-items-start">
                        <div>
                            <strong>${escapeHtml(s.name || 'Unnamed Plex')}</strong><br>
                            <small class="text-muted">${escapeHtml(s.uri || s.host || '')}</small>
                        </div>
                        <div>${ownedBadge} ${localBadge}</div>
                    </div>
                </button>
            `;
        }).join('');
        $('#plexDiscoveredList').classList.remove('d-none');

        $$('.plex-server-pick').forEach((el) => {
            el.addEventListener('click', () => {
                const uri = el.dataset.uri;
                const name = el.dataset.name;
                $('#serverUrl').value = uri;
                if (!$('#serverName').value) $('#serverName').value = name;
                // Visually mark the picked server.
                $$('.plex-server-pick').forEach((x) => x.classList.remove('active'));
                el.classList.add('active');
            });
        });
    }

    function configureAuthForType(type) {
        const methodSection = $('#auth-method-section');
        if (type === 'plex') {
            methodSection.classList.add('d-none');
            wizard.authMethod = 'token';
            $('#auth-fields-token-plex').classList.remove('d-none');
            $('#auth-fields-password').classList.add('d-none');
            $('#auth-fields-api-key').classList.add('d-none');
            $('#auth-fields-quick-connect').classList.add('d-none');
        } else {
            methodSection.classList.remove('d-none');
            // Pick a sensible default per vendor.
            const defaultMethod = type === 'jellyfin' ? 'quick_connect' : 'password';
            wizard.authMethod = defaultMethod;
            $$('input[name="authMethod"]').forEach((r) => {
                r.checked = r.value === defaultMethod;
            });
            // Quick Connect only makes sense on Jellyfin.
            $('#auth-quick').parentElement.classList.toggle('d-none', type !== 'jellyfin');
            $$('label[for=auth-quick]').forEach((l) => l.classList.toggle('d-none', type !== 'jellyfin'));
            renderAuthFields();
        }
    }

    function renderAuthFields() {
        $('#auth-fields-password').classList.toggle('d-none', wizard.authMethod !== 'password');
        $('#auth-fields-api-key').classList.toggle('d-none', wizard.authMethod !== 'api_key');
        $('#auth-fields-quick-connect').classList.toggle('d-none', wizard.authMethod !== 'quick_connect');
        $('#auth-fields-token-plex').classList.toggle('d-none', wizard.authMethod !== 'token');
    }

    async function startQuickConnect() {
        const url = $('#serverUrl').value.trim();
        if (!url) { alert('Enter the Jellyfin URL first.'); return; }
        const r = await api('POST', '/api/servers/auth/jellyfin/quick-connect/initiate', { url });
        if (!r.ok || !r.data || !r.data.ok) {
            $('#quickConnectCode').classList.remove('d-none');
            $('#quickConnectCode').className = 'alert alert-danger';
            $('#quickConnectCode').textContent = (r.data && r.data.message) || 'Quick Connect failed';
            return;
        }
        wizard.quickConnectSecret = r.data.secret;
        $('#quickConnectCode').classList.remove('d-none');
        $('#quickConnectCode').className = 'alert alert-info';
        $('#quickConnectCode').innerHTML =
            `Open Jellyfin → your profile → Quick Connect, then enter <strong class="fs-3">${escapeHtml(r.data.code)}</strong>. Waiting for approval…`;

        // Poll every 2 seconds.
        if (wizard.quickConnectPoll) clearInterval(wizard.quickConnectPoll);
        wizard.quickConnectPoll = setInterval(async () => {
            const p = await api('POST', '/api/servers/auth/jellyfin/quick-connect/poll',
                { url, secret: wizard.quickConnectSecret });
            if (p.ok && p.data && p.data.authenticated) {
                clearInterval(wizard.quickConnectPoll);
                wizard.quickConnectPoll = null;
                const e = await api('POST', '/api/servers/auth/jellyfin/quick-connect/exchange',
                    { url, secret: wizard.quickConnectSecret });
                if (e.ok && e.data && e.data.ok) {
                    wizard.accessToken = e.data.access_token;
                    wizard.userId = e.data.user_id;
                    $('#quickConnectCode').className = 'alert alert-success';
                    $('#quickConnectCode').innerHTML = `<i class="bi bi-check2-circle me-1"></i>Approved as ${escapeHtml(e.data.server_name || 'Jellyfin user')}.`;
                } else {
                    $('#quickConnectCode').className = 'alert alert-danger';
                    $('#quickConnectCode').textContent = (e.data && e.data.message) || 'Token exchange failed';
                }
            }
        }, 2000);
    }

    async function testConnection() {
        wizard.url = $('#serverUrl').value.trim();
        wizard.name = $('#serverName').value.trim();
        if (!wizard.url || !wizard.name) { alert('Enter a URL and a display name.'); return; }

        // Build auth based on method.
        const auth = await buildAuth();
        if (!auth) return;  // helper already alerted

        const payload = {
            type: wizard.type,
            name: wizard.name,
            url: wizard.url,
            auth,
        };
        if (wizard.type === 'plex') {
            payload.output = {
                adapter: 'plex_bundle',
                plex_config_folder: $('#plexConfigFolder').value.trim(),
                frame_interval: 10,
            };
        }

        const r = await api('POST', '/api/servers/test-connection', payload);
        const result = $('#connectResult');
        if (r.ok && r.data && r.data.ok) {
            result.className = 'alert alert-success';
            result.innerHTML = `<i class="bi bi-check2-circle me-1"></i>Connected to <strong>${escapeHtml(r.data.server_name || wizard.name)}</strong>${r.data.version ? ' (v' + escapeHtml(r.data.version) + ')' : ''}.`;
        } else {
            result.className = 'alert alert-warning';
            result.innerHTML = `<i class="bi bi-exclamation-triangle me-1"></i>${escapeHtml((r.data && r.data.message) || 'Connection failed')}`;
        }
        wizard._lastTestPayload = payload;
        showStep('step-result');
    }

    async function buildAuth() {
        if (wizard.type === 'plex') {
            // Prefer the OAuth-derived token when present; fall back to manual.
            const tok = wizard.plexToken || $('#plexToken').value.trim();
            if (!tok) { alert('Plex token required (sign in with Plex or paste a token).'); return null; }
            return { method: 'token', token: tok };
        }
        if (wizard.authMethod === 'api_key') {
            const k = $('#authApiKey').value.trim();
            if (!k) { alert('API key required.'); return null; }
            return { method: 'api_key', api_key: k };
        }
        if (wizard.authMethod === 'password') {
            const u = $('#authUsername').value.trim();
            const p = $('#authPassword').value;
            if (!u) { alert('Username required.'); return null; }
            const endpoint = wizard.type === 'jellyfin'
                ? '/api/servers/auth/jellyfin/password'
                : '/api/servers/auth/emby/password';
            const r = await api('POST', endpoint, { url: wizard.url, username: u, password: p });
            if (!r.ok || !r.data || !r.data.ok) {
                alert((r.data && r.data.message) || 'Authentication failed');
                return null;
            }
            return {
                method: 'password',
                access_token: r.data.access_token,
                user_id: r.data.user_id,
            };
        }
        if (wizard.authMethod === 'quick_connect') {
            if (!wizard.accessToken) {
                alert('Complete Quick Connect first.');
                return null;
            }
            return {
                method: 'quick_connect',
                access_token: wizard.accessToken,
                user_id: wizard.userId,
            };
        }
        return null;
    }

    async function saveServer() {
        const payload = wizard._lastTestPayload;
        if (!payload) { alert('No test payload — go back and run a connection test first.'); return; }
        const r = await api('POST', '/api/servers', payload);
        if (r.ok) {
            const modal = bootstrap.Modal.getInstance(document.getElementById('addServerModal'));
            modal.hide();
            loadServers();
        } else {
            alert(`Failed to save: ${(r.data && r.data.error) || r.status}`);
        }
    }
})();
