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

    // ---------- inline form-validation helpers --------------------------------
    // Centralised so popups never appear for form errors. Marking a field
    // invalid relies on Bootstrap's `.is-invalid` + adjacent
    // `.invalid-feedback` div pattern. Helpers also self-clear on input.
    function markFieldInvalid(input, msg) {
        if (!input) return;
        input.classList.add('is-invalid');
        // Override the static feedback message if one was provided.
        const fb = input.parentElement && input.parentElement.querySelector('.invalid-feedback');
        if (fb && msg) fb.textContent = msg;
        // Re-clear once the user edits the field, so the red doesn't stick.
        if (!input.dataset.invalidWired) {
            input.addEventListener('input', () => input.classList.remove('is-invalid'), { once: true });
            input.dataset.invalidWired = '1';
        }
        input.focus();
    }

    function clearFieldErrors(rootSelector) {
        $$(`${rootSelector || '#step-connect'} .is-invalid`).forEach(el => el.classList.remove('is-invalid'));
    }

    function showFormError(msg, region) {
        const el = $(region || '#connectFormError');
        if (!el) return;
        el.textContent = msg;
        el.classList.remove('d-none');
    }

    function clearFormError(region) {
        const el = $(region || '#connectFormError');
        if (!el) return;
        el.classList.add('d-none');
        el.textContent = '';
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
                else showToast('Delete failed', `${(r.data && r.data.error) || r.status}`, 'danger');
            });
        });
        $$('.edit-server-btn').forEach((btn) => {
            btn.addEventListener('click', async (ev) => {
                const id = ev.currentTarget.dataset.id;
                openEditModal(id);
            });
        });
        $$('.refresh-libraries-btn').forEach((btn) => {
            btn.addEventListener('click', async (ev) => {
                // Capture the button before await — `ev.currentTarget` is
                // nulled once the event handler returns, which happens at
                // the first await suspension.
                const target = ev.currentTarget;
                const id = target.dataset.id;
                target.disabled = true;
                target.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Refreshing…';
                const r = await api('POST', `/api/servers/${encodeURIComponent(id)}/refresh-libraries`);
                if (r.ok) loadServers();
                else {
                    showToast('Refresh failed', `${(r.data && r.data.error) || r.status}`, 'danger');
                    target.disabled = false;
                    target.innerHTML = '<i class="bi bi-arrow-clockwise me-1"></i>Refresh libraries';
                }
            });
        });
        $$('.fix-trickplay-btn').forEach((btn) => {
            // Calls /api/servers/<id>/jellyfin/fix-trickplay which flips
            // EnableTrickplayImageExtraction on every library so Jellyfin
            // actually serves the trickplay sidecars we publish. Idempotent
            // — safe to click twice.
            btn.addEventListener('click', async (ev) => {
                const target = ev.currentTarget;
                const id = target.dataset.id;
                const original = target.innerHTML;
                target.disabled = true;
                target.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Fixing…';
                const r = await api('POST', `/api/servers/${encodeURIComponent(id)}/jellyfin/fix-trickplay`);
                if (r.ok && r.data && r.data.ok) {
                    target.innerHTML = '<i class="bi bi-check2 me-1"></i>Fixed';
                    // Re-probe so the button hides automatically once
                    // every library reports trickplay enabled — without
                    // this, the button reverted to "Fix trickplay" on
                    // the next render even though the fix had taken.
                    setTimeout(() => probeJellyfinTrickplay(id, target), 1500);
                } else {
                    const msg = (r.data && (r.data.error || JSON.stringify(r.data.results))) || r.status;
                    showToast('Trickplay fix failed', String(msg), 'danger');
                    target.innerHTML = original;
                    target.disabled = false;
                }
            });
        });
        // Per-card trickplay probe: hide the Fix trickplay button when
        // every library already has extraction enabled, otherwise reveal
        // it. Runs after the list has rendered so the button stays
        // d-none by default.
        $$('.fix-trickplay-btn').forEach((btn) => {
            probeJellyfinTrickplay(btn.dataset.id, btn);
        });
    }

    async function probeJellyfinTrickplay(serverId, btn) {
        if (!btn) return;
        const r = await api('GET', `/api/servers/${encodeURIComponent(serverId)}/jellyfin/trickplay-status`);
        const libs = (r.ok && r.data && Array.isArray(r.data.libraries)) ? r.data.libraries : [];
        const needsFix = libs.some(l => !l.extraction_enabled);
        if (needsFix) {
            btn.classList.remove('d-none');
            btn.disabled = false;
            btn.innerHTML = '<i class="bi bi-magic me-1"></i>Fix trickplay';
        } else {
            btn.classList.add('d-none');
        }
    }

    function serverCard(server) {
        const typeBadgeColor = { plex: 'warning', emby: 'success', jellyfin: 'info' }[server.type] || 'secondary';
        const enabledBadge = server.enabled
            ? '<span class="badge bg-success">enabled</span>'
            : '<span class="badge bg-secondary">disabled</span>';
        const libCount = (server.libraries || []).length;
        const enabledLibs = (server.libraries || []).filter((l) => l.enabled).length;
        // Vendor SVG logo (24px) prepended to the server name. Falls back
        // to nothing when type is unknown — the type-coloured badge on the
        // right keeps the vendor signal.
        const vendorLogo = ['plex', 'emby', 'jellyfin'].includes((server.type || '').toLowerCase())
            ? `<img src="/static/images/vendors/${escapeHtml(server.type.toLowerCase())}.svg" alt="${escapeHtml(server.type)}" width="24" height="24" style="margin-right: 8px; vertical-align: -5px;">`
            : '';
        return `
            <div class="col-md-6 col-lg-4">
                <div class="card h-100">
                    <div class="card-body">
                        <div class="d-flex justify-content-between align-items-start mb-2">
                            <h5 class="card-title mb-0">${vendorLogo}${escapeHtml(server.name)}</h5>
                            <span class="badge bg-${typeBadgeColor}">${escapeHtml(server.type)}</span>
                        </div>
                        <div class="text-muted small mb-2">${escapeHtml(server.url)}</div>
                        <div class="mb-2">${enabledBadge}</div>
                        <div class="text-muted small">
                            Libraries: <strong>${enabledLibs}</strong> enabled / ${libCount} total
                        </div>
                    </div>
                    <div class="card-footer bg-transparent d-flex flex-wrap gap-1 justify-content-between">
                        <div class="d-flex flex-wrap gap-1">
                            <button class="btn btn-sm btn-outline-primary edit-server-btn"
                                    data-id="${escapeHtml(server.id)}">
                                <i class="bi bi-pencil me-1"></i>Edit
                            </button>
                            <button class="btn btn-sm btn-outline-secondary refresh-libraries-btn"
                                    data-id="${escapeHtml(server.id)}">
                                <i class="bi bi-arrow-clockwise me-1"></i>Refresh libraries
                            </button>
                            ${server.type === 'jellyfin' ? `
                            <button class="btn btn-sm btn-outline-warning fix-trickplay-btn d-none"
                                    data-id="${escapeHtml(server.id)}"
                                    title="Enable trickplay extraction so Jellyfin actually serves the preview thumbnails we publish">
                                <i class="bi bi-magic me-1"></i>Fix trickplay
                            </button>
                            ` : ''}
                        </div>
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
        // step-type only exists when the modal is on the page (i.e. /servers).
        // The setup wizard inlines just the connection form and supplies its
        // own vendor picker, so guard the call here.
        if (document.getElementById('step-type')) {
            showStep('step-type');
        }
        const titleEl = document.getElementById('serverModalTitle');
        if (titleEl) titleEl.textContent = 'Add Server';
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

    // Switch the connection form into "connect to <vendor>" mode and reveal
    // step-connect. Used by both the modal's vendor buttons (/servers) and
    // the setup wizard's vendor picker (/setup, via window.MPGShared.pickVendor).
    function pickVendorAndAdvance(vendor) {
        wizard.type = vendor;
        const vendorLabel = document.getElementById('step-connect-vendor');
        if (vendorLabel) {
            vendorLabel.textContent = vendor[0].toUpperCase() + vendor.slice(1);
        }
        showStep('step-connect');
        configureAuthForType(vendor);
    }

    document.addEventListener('DOMContentLoaded', () => {
        // The Add Server modal is a shared partial included from both
        // /servers and /setup. The /servers-only setup (server list,
        // webhook URL, edit modal) only runs when those elements exist
        // — on /setup we just need the modal wiring below.
        const isServersPage = !!document.getElementById('serverList');

        if (isServersPage) {
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
        }

        // Modal-only wiring. /setup inlines the connection form (no modal),
        // so guard these so a missing #addServerModal doesn't throw and
        // break the connection-form button listeners further down.
        const modalEl = document.getElementById('addServerModal');
        if (modalEl) {
            modalEl.addEventListener('show.bs.modal', resetWizard);
            modalEl.addEventListener('hidden.bs.modal', resetWizard);
        }

        $$('.server-type-btn').forEach((btn) => {
            btn.addEventListener('click', () => pickVendorAndAdvance(btn.dataset.type));
        });

        // Legacy /servers?add=<vendor> entry point — pre-opens the modal at
        // the connection step. Setup wizard no longer uses this path (it
        // calls window.MPGShared.pickVendor directly via the inline panel),
        // but kept for any deep links / bookmarks that survived the refactor.
        const _addParam = new URLSearchParams(window.location.search).get('add');
        if (_addParam && ['plex', 'emby', 'jellyfin'].includes(_addParam) && modalEl) {
            const _modal = bootstrap.Modal.getOrCreateInstance(modalEl);
            _modal.show();
            setTimeout(() => {
                document.querySelector('.server-type-btn[data-type="' + _addParam + '"]')?.click();
            }, 50);
            window.history.replaceState({}, '', window.location.pathname);
        }

        // step-connect-back returns to step-type — that only exists in the
        // modal. /setup overrides this in its own DOMContentLoaded handler
        // to return to the wizard's vendor picker instead.
        $('#step-connect-back').addEventListener('click', () => {
            if (document.getElementById('step-type')) showStep('step-type');
        });

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
        const addSelected = $('#plexAddSelected');
        if (addSelected) addSelected.addEventListener('click', addSelectedPlexServers);
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
                    showFormError('Could not list Plex servers from plex.tv. Try the manual token option below.');
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
                showFormError('Plex OAuth failed: ' + (err.message || err));
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
            // pollForToken resolves with the actual auth_token (not just a
            // boolean) so we can hand it to the wizard's per-server entry
            // builder. The onSuccess callback wired into the constructor
            // above expects the token as its parameter, so invoke it
            // explicitly — pollForToken intentionally does NOT call
            // onSuccess itself (PlexAuth.login() is the wrapper that does;
            // we don't use login() here because we want the in-flight
            // button-state changes around the discovery fetch).
            const token = await auth.pollForToken(pin.id);
            if (token) {
                await auth.onSuccess(token);
            }
        } catch (err) {
            console.error('Plex OAuth flow error', err);
            showFormError('Plex OAuth flow error: ' + (err.message || err));
            btn.disabled = false;
            btn.innerHTML = origLabel;
        }
    }

    // Stash the discovered set so the batch-add path can look up
    // each server's machine_id + uri without re-fetching.
    let plexDiscoveredCache = [];

    function renderPlexDiscovered(servers) {
        const list = $('#plexDiscoveredServers');
        plexDiscoveredCache = servers || [];
        if (servers.length === 0) {
            list.innerHTML = '<div class="text-muted small">No Plex servers found on your account.</div>';
            $('#plexDiscoveredList').classList.remove('d-none');
            return;
        }
        list.innerHTML = servers.map((s, idx) => {
            const ownedBadge = s.owned ? '<span class="badge bg-success">owned</span>' : '<span class="badge bg-secondary">shared</span>';
            const localBadge = s.local ? '<span class="badge bg-info ms-1">local</span>' : '';
            const sslBadge = s.ssl ? '<span class="badge bg-secondary ms-1">https</span>' : '';
            return `
                <label class="list-group-item d-flex align-items-start gap-2">
                    <input type="checkbox" class="form-check-input mt-1 plex-server-pick"
                           data-idx="${idx}"
                           data-uri="${escapeHtml(s.uri || '')}"
                           data-name="${escapeHtml(s.name || '')}"
                           data-machine-id="${escapeHtml(s.machine_id || '')}">
                    <div class="flex-grow-1">
                        <strong>${escapeHtml(s.name || 'Unnamed Plex')}</strong>
                        ${ownedBadge}${localBadge}${sslBadge}
                        <br>
                        <small class="text-muted">${escapeHtml(s.uri || s.host || '')}</small>
                    </div>
                </label>
            `;
        }).join('');
        $('#plexDiscoveredList').classList.remove('d-none');

        $$('.plex-server-pick').forEach((el) => {
            el.addEventListener('change', () => {
                const checked = $$('.plex-server-pick:checked');
                const count = checked.length;
                $('#plexSelectedCount').textContent = String(count);
                $('#plexAddSelected').classList.toggle('d-none', count < 1);

                // Single-pick convenience: when exactly one is ticked,
                // populate the wizard fields so the user can hit "Test
                // connection" and customise. Multi-pick clears them
                // (the batch path doesn't need them).
                if (count === 1) {
                    const one = checked[0];
                    $('#serverUrl').value = one.dataset.uri;
                    if (!$('#serverName').value) $('#serverName').value = one.dataset.name;
                } else {
                    $('#serverUrl').value = '';
                }
            });
        });
    }

    /**
     * Batch-add every ticked Plex server in one go. Each becomes its
     * own ``media_servers`` entry with the same Plex token + the
     * machine_id pulled from /api/v2/resources as ``server_identity``.
     * Avoids the connection-test step (already trusted: user just
     * proved control of the plex.tv account).
     */
    async function addSelectedPlexServers() {
        const checked = Array.from($$('.plex-server-pick:checked'));
        if (checked.length === 0) return;
        const plexConfigFolder = $('#plexConfigFolder').value.trim() || '/config/plex';
        const btn = $('#plexAddSelected');
        const orig = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Adding…';

        const results = [];
        for (const el of checked) {
            const idx = parseInt(el.dataset.idx, 10);
            const server = plexDiscoveredCache[idx] || {};
            const payload = {
                type: 'plex',
                name: server.name || el.dataset.name || 'Plex',
                enabled: true,
                url: server.uri || el.dataset.uri,
                auth: { method: 'token', token: wizard.plexToken },
                server_identity: server.machine_id || el.dataset.machineId || null,
                libraries: [],
                path_mappings: [],
                output: {
                    adapter: 'plex_bundle',
                    plex_config_folder: plexConfigFolder,
                    frame_interval: 10,
                },
            };
            const r = await api('POST', '/api/servers', payload);
            results.push({ name: payload.name, ok: r.ok, message: r.data && r.data.error });
        }

        btn.disabled = false;
        btn.innerHTML = orig;

        const failed = results.filter((r) => !r.ok);
        if (failed.length === 0) {
            // All saved — close modal + reload list (and notify any
            // listening page so the setup wizard can advance).
            const modalEl = document.getElementById('addServerModal');
            if (modalEl && window.bootstrap) {
                const inst = window.bootstrap.Modal.getInstance(modalEl);
                if (inst) inst.hide();
            }
            document.dispatchEvent(new CustomEvent('mediaServerAdded', {
                detail: { count: results.length, type: 'plex' },
            }));
            if (typeof loadServers === 'function' && document.getElementById('serverList')) {
                loadServers();
            }
        } else {
            showToast(
                `Saved ${results.length - failed.length}/${results.length}`,
                failed.map((f) => `${f.name}: ${f.message || 'unknown error'}`).join('; '),
                'warning',
            );
            if (typeof loadServers === 'function' && document.getElementById('serverList')) {
                loadServers();
            }
        }
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
        if (!url) { markFieldInvalid($('#serverUrl'), 'Enter the Jellyfin URL first.'); return; }
        const r = await api('POST', '/api/servers/auth/jellyfin/quick-connect/initiate', { url });
        if (!r.ok || !r.data || !r.data.ok) {
            $('#quickConnectCode').classList.remove('d-none');
            $('#quickConnectCode').className = 'alert alert-danger';
            $('#quickConnectCode').textContent = (r.data && r.data.message) || 'Quick Connect failed';
            return;
        }
        wizard.quickConnectSecret = r.data.secret;
        // D22 — auto-open the Jellyfin Quick Connect entry page in a
        // new tab so the user doesn't have to navigate manually. Best-
        // effort: popup blockers may refuse, in which case the inline
        // instruction below still tells them where to go. Strip any
        // trailing slash on the base URL so we don't end up with
        // /web//#/quickconnect.
        const baseUrl = url.replace(/\/+$/, '');
        const qcUrl = baseUrl + '/web/#/quickconnect';
        try { window.open(qcUrl, '_blank', 'noopener,noreferrer'); } catch (_) { /* blocked */ }
        $('#quickConnectCode').classList.remove('d-none');
        $('#quickConnectCode').className = 'alert alert-info';
        $('#quickConnectCode').innerHTML =
            `Opened <a href="${escapeHtml(qcUrl)}" target="_blank" rel="noopener" class="alert-link">Jellyfin Quick Connect</a> in a new tab — log in if needed,
             then paste this code: <strong class="fs-3">${escapeHtml(r.data.code)}</strong>.
             Waiting for approval…
             <div class="small text-muted mt-2">
               <i class="bi bi-info-circle me-1"></i>After you log in: Jellyfin needs <em>Trickplay image extraction</em> enabled per library —
               the Servers page has a one-click <strong>Fix trickplay</strong> button on each Jellyfin server card.
             </div>`;

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
        clearFieldErrors('#step-connect');
        clearFormError();
        wizard.url = $('#serverUrl').value.trim();
        wizard.name = $('#serverName').value.trim();
        let firstBad = null;
        if (!wizard.url) { markFieldInvalid($('#serverUrl')); firstBad = $('#serverUrl'); }
        if (!wizard.name) {
            markFieldInvalid($('#serverName'));
            if (!firstBad) firstBad = $('#serverName');
        }
        if (firstBad) { firstBad.focus(); return; }

        // Build auth based on method.
        const auth = await buildAuth();
        if (!auth) return;  // helper already surfaced an inline error

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

            // Surface server-side warnings (e.g. Jellyfin trickplay
            // extraction disabled). The "Fix it for me" button posts
            // to the per-vendor remediation endpoint and re-tests.
            const warnings = Array.isArray(r.data.warnings) ? r.data.warnings : [];
            if (warnings.length > 0) {
                const warnDiv = document.createElement('div');
                warnDiv.className = 'alert alert-warning mt-2';
                warnings.forEach(w => {
                    const wrap = document.createElement('div');
                    wrap.innerHTML = `<i class="bi bi-exclamation-triangle me-1"></i>${escapeHtml(w.message || 'Setup warning')}`;
                    if (w.code === 'jellyfin_trickplay_disabled') {
                        const libs = Array.isArray(w.libraries) ? w.libraries : [];
                        if (libs.length > 0) {
                            const libNames = libs.map(l => escapeHtml(l.name || l.id)).join(', ');
                            wrap.innerHTML += `<div class="small text-muted mt-1">Affected libraries: ${libNames}</div>`;
                        }
                        const btn = document.createElement('button');
                        btn.type = 'button';
                        btn.className = 'btn btn-sm btn-warning mt-2';
                        btn.innerHTML = '<i class="bi bi-magic me-1"></i>Fix it for me';
                        btn.dataset.libraryIds = libs.map(l => l.id).join(',');
                        btn.addEventListener('click', async () => {
                            btn.disabled = true;
                            btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Fixing…';
                            // The wizard's server hasn't been saved yet, so
                            // we don't have a stored ``server_id``. Skip the
                            // pre-save fix here and tell the user it'll be
                            // applied after they click Save.
                            wrap.innerHTML = '<i class="bi bi-info-circle me-1"></i>The trickplay flag will be enabled when you save this server.';
                            wizard._pendingTrickplayFix = libs.map(l => l.id);
                        });
                        wrap.appendChild(btn);
                    }
                    warnDiv.appendChild(wrap);
                });
                result.appendChild(warnDiv);
            }
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
            if (!tok) {
                markFieldInvalid($('#plexToken'), 'Sign in with Plex or paste a token.');
                return null;
            }
            return { method: 'token', token: tok };
        }
        if (wizard.authMethod === 'api_key') {
            const k = $('#authApiKey').value.trim();
            if (!k) { markFieldInvalid($('#authApiKey')); return null; }
            return { method: 'api_key', api_key: k };
        }
        if (wizard.authMethod === 'password') {
            const u = $('#authUsername').value.trim();
            const p = $('#authPassword').value;
            if (!u) { markFieldInvalid($('#authUsername')); return null; }
            const endpoint = wizard.type === 'jellyfin'
                ? '/api/servers/auth/jellyfin/password'
                : '/api/servers/auth/emby/password';
            const r = await api('POST', endpoint, { url: wizard.url, username: u, password: p });
            if (!r.ok || !r.data || !r.data.ok) {
                // Common cause: wrong URL (or URL unreachable from this
                // container — e.g. user typed `http://localhost:8096` but
                // Jellyfin is on a docker bridge). The backend message is
                // usually specific enough; pass it through verbatim.
                const msg = (r.data && r.data.message)
                    || `Authentication failed (HTTP ${r.status}). Check the username, password, and that the URL is reachable from this container.`;
                showFormError(msg);
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
                showFormError('Complete Quick Connect first — open Jellyfin and approve the code shown above.');
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
        if (!payload) {
            showToast('Run the connection test first', 'Use the Test connection button before saving.', 'warning');
            return;
        }
        const r = await api('POST', '/api/servers', payload);
        if (r.ok) {
            // Modal only exists on /servers; /setup inlines the form.
            const modalEl = document.getElementById('addServerModal');
            if (modalEl) {
                const modal = bootstrap.Modal.getInstance(modalEl);
                if (modal) modal.hide();
            }
            // Notify any listening page (the setup wizard subscribes to this
            // so it can advance from step 1 → GPU/security after an
            // Emby/Jellyfin add). Always fires; /servers ignores it.
            document.dispatchEvent(new CustomEvent('mediaServerAdded', {
                detail: { server: r.data, type: payload.type },
            }));
            if (typeof loadServers === 'function' && document.getElementById('serverList')) {
                loadServers();
            }
        } else {
            const msg = (r.data && r.data.error) || `HTTP ${r.status}`;
            showFormError(`Failed to save server: ${msg}`);
        }
    }

    // ---------- Edit Server modal --------------------------------------------
    // Opens a separate modal pre-populated from GET /api/servers/<id> and
    // submits via PUT /api/servers/<id>. Path mappings + exclude paths get
    // an "Apply to all servers" button that PUTs the same list to every
    // other configured server (one click instead of N).

    let _editState = null;  // { server, allServers }

    async function openEditModal(serverId) {
        // Fetch the target server + the full server list (needed for the
        // "Apply to all" buttons so we know who to copy to).
        const [singleR, listR] = await Promise.all([
            api('GET', `/api/servers/${encodeURIComponent(serverId)}`),
            api('GET', '/api/servers'),
        ]);
        if (!singleR.ok || !singleR.data) {
            showToast('Failed to load server', `HTTP ${singleR.status}`, 'danger');
            return;
        }
        const server = singleR.data;
        const allServers = (listR.ok && listR.data && listR.data.servers) || [];
        _editState = { server, allServers };

        $('#editServerName').textContent = `${server.name || ''} (${server.type || ''})`;
        $('#editServerId').value = server.id || '';
        $('#editServerType').value = server.type || '';
        $('#editServerDisplayName').value = server.name || '';
        $('#editServerUrl').value = server.url || '';
        $('#editServerVerifySsl').checked = server.verify_ssl !== false;
        $('#editServerEnabled').checked = server.enabled !== false;
        $('#editServerToken').value = '';

        // Plex-only: show the config folder field + wire its inline validator,
        // and reveal the "Webhook & Scanner" tab (Phase H4).
        const isPlex = (server.type || '').toLowerCase() === 'plex';
        $('#editPlexConfigGroup').classList.toggle('d-none', !isPlex);
        const automationTabLi = document.getElementById('editTabAutomationLi');
        if (automationTabLi) automationTabLi.classList.toggle('d-none', !isPlex);
        // Always force the General tab active on open. Without this, opening a
        // Plex server, clicking "Webhook & Scanner", closing, then opening a
        // non-Plex server leaves the now-hidden Plex pane visible because
        // Bootstrap doesn't auto-reset on modal hide. (Fix-2 from H code review.)
        try {
            document.querySelectorAll('#editServerModal .nav-link').forEach((el) => el.classList.remove('active'));
            document.querySelectorAll('#editServerModal .tab-pane').forEach((el) => el.classList.remove('show', 'active'));
            const generalTab = document.querySelector('#editServerModal [data-bs-target="#edit-tab-general"]');
            const generalPane = document.getElementById('edit-tab-general');
            if (generalTab) generalTab.classList.add('active');
            if (generalPane) generalPane.classList.add('show', 'active');
        } catch (_e) {
            // Best-effort — Bootstrap not available shouldn't break Edit.
        }
        if (isPlex) {
            const out = server.output || {};
            const cfgInput = $('#editPlexConfigFolder');
            cfgInput.value = out.plex_config_folder || '';
            cfgInput.classList.remove('is-valid', 'is-invalid');
            // Bind once — _editPlexConfigBound is set after the first wire-up.
            if (!cfgInput.dataset.validatorBound) {
                cfgInput.addEventListener('input', _debouncedValidatePath(cfgInput));
                cfgInput.dataset.validatorBound = '1';
            }
            if (cfgInput.value) _validateLocalPathInput(cfgInput);

            // Webhook & Scanner panel: scope all panel calls to this Plex
            // server (Phase I5), then load status + scanner list so the user
            // sees current state without needing to open the tab first.
            try {
                if (typeof setPlexWebhookPanelServerId === 'function') setPlexWebhookPanelServerId(server.id);
                if (typeof _wirePlexWebhookPanel === 'function') _wirePlexWebhookPanel();
                if (typeof loadPlexWebhookStatus === 'function') loadPlexWebhookStatus();
                if (typeof loadRecentlyAddedScanners === 'function') loadRecentlyAddedScanners();
                const caption = document.getElementById('editPlexWebhookServerCaption');
                if (caption) caption.innerHTML = `This webhook will be registered with <strong>${escapeHtml(server.name || 'this Plex server')}</strong> using its own Plex token.`;
            } catch (_e) { }
        }

        renderEditLibraries(server.libraries || []);
        renderEditPathMappings(server.path_mappings || []);
        renderEditExcludePaths(server.exclude_paths || []);
        $('#editServerResult').className = 'd-none';
        $('#editServerResult').innerHTML = '';

        const modalEl = document.getElementById('editServerModal');
        const modal = window.bootstrap.Modal.getOrCreateInstance(modalEl);
        modal.show();
    }

    function renderEditLibraries(libraries) {
        const list = $('#editLibraryList');
        if (!libraries.length) {
            list.innerHTML = '<div class="text-muted small">No cached libraries — click "Refresh libraries" on the server card to fetch them from the server.</div>';
            return;
        }
        list.innerHTML = libraries.map((lib, idx) => `
            <label class="list-group-item d-flex align-items-center gap-2">
                <input type="checkbox" class="form-check-input edit-lib-toggle"
                       data-idx="${idx}"
                       data-id="${escapeHtml(lib.id || '')}"
                       data-name="${escapeHtml(lib.name || lib.id || '')}"
                       ${lib.enabled ? 'checked' : ''}>
                <span>${escapeHtml(lib.name || lib.id || 'unnamed')}</span>
                <span class="badge bg-secondary ms-auto">${escapeHtml(lib.kind || 'unknown')}</span>
            </label>
        `).join('');
    }

    function renderEditPathMappings(mappings) {
        const tbody = $('#editPathMappingsTable tbody');
        tbody.innerHTML = '';
        mappings.forEach((row) => addPathMappingRow(row));
        if (!mappings.length) addPathMappingRow();
    }

    function addPathMappingRow(row) {
        row = row || {};
        const tbody = $('#editPathMappingsTable tbody');
        const tr = document.createElement('tr');
        const remoteVal = row.plex_prefix || row.remote_prefix || '';
        const localVal = row.local_prefix || '';
        const webhookAliases = Array.isArray(row.webhook_prefixes)
            ? row.webhook_prefixes.join('; ')
            : (row.webhook_prefixes || '');
        tr.innerHTML = `
            <td><input type="text" class="form-control form-control-sm pm-remote" value="${escapeHtml(remoteVal)}" placeholder="/data_16tb/movies"></td>
            <td>
                <div class="input-group input-group-sm">
                    <input type="text" class="form-control form-control-sm pm-local" value="${escapeHtml(localVal)}" placeholder="/mnt/plex/movies">
                    <button type="button" class="btn btn-outline-secondary pm-browse" title="Browse folders">
                        <i class="bi bi-folder2-open"></i>
                    </button>
                    <div class="invalid-feedback small"></div>
                    <div class="valid-feedback small">Path exists</div>
                </div>
            </td>
            <td><input type="text" class="form-control form-control-sm pm-webhook" value="${escapeHtml(webhookAliases)}" placeholder="/data" title="Optional. Webhook source prefix that resolves to this disk. Add another row for additional sources."></td>
            <td><button type="button" class="btn btn-sm btn-outline-danger pm-remove"><i class="bi bi-x-lg"></i></button></td>
        `;
        tr.querySelector('.pm-remove').addEventListener('click', () => tr.remove());
        const localInput = tr.querySelector('.pm-local');
        localInput.addEventListener('input', _debouncedValidatePath(localInput));
        if (localVal) _validateLocalPathInput(localInput);
        tr.querySelector('.pm-browse').addEventListener('click', () => {
            const start = (localInput.value || '').trim() || '/';
            window.openFolderPicker(start, (picked) => {
                localInput.value = picked;
                _validateLocalPathInput(localInput);
            });
        });
        tbody.appendChild(tr);
    }

    // Debounced inline validation of local-path inputs (path mappings + Plex
    // config folder). Mirrors the Setup Wizard's UX so users get red-border
    // feedback the moment they type a path that doesn't exist.
    const _validateTimers = new WeakMap();
    function _debouncedValidatePath(input) {
        return function () {
            clearTimeout(_validateTimers.get(input));
            _validateTimers.set(input, setTimeout(() => _validateLocalPathInput(input), 400));
        };
    }

    async function _validateLocalPathInput(input) {
        const path = (input.value || '').trim();
        const feedback = input.parentElement.querySelector('.invalid-feedback');
        const success = input.parentElement.querySelector('.valid-feedback');
        if (!path) {
            input.classList.remove('is-invalid', 'is-valid');
            return;
        }
        // The Plex config folder field gets the deeper structural check so
        // the success message can confidently say "this is a real Plex config
        // folder". Other path-mapping inputs only need existence + readable.
        // Three IDs cover the same widget on different surfaces:
        //   * editPlexConfigFolder       — /servers Edit Server modal
        //   * plexConfigFolder           — _server_connection_form partial
        //   * wizardPlexConfigFolder     — /setup wizard step 3 (renamed to
        //                                   avoid colliding with the partial's
        //                                   hidden input on the same page)
        const useStructuralCheck =
            input.id === 'editPlexConfigFolder' ||
            input.id === 'plexConfigFolder' ||
            input.id === 'wizardPlexConfigFolder';
        const endpoint = useStructuralCheck
            ? '/api/settings/validate-plex-config-folder'
            : '/api/settings/validate-local-path';
        try {
            const resp = await fetch(endpoint, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': typeof getCsrfToken === 'function' ? getCsrfToken() : '',
                },
                body: JSON.stringify({ path }),
            });
            const data = await resp.json();
            if (input.value.trim() !== path) return;
            if (data.error) {
                input.classList.remove('is-valid');
                input.classList.add('is-invalid');
                if (feedback) feedback.textContent = data.error;
            } else if (!data.exists) {
                input.classList.remove('is-valid');
                input.classList.add('is-invalid');
                if (feedback) feedback.textContent = 'Directory not found on this container';
            } else {
                input.classList.remove('is-invalid');
                input.classList.add('is-valid');
                if (success && useStructuralCheck && data.detail) {
                    success.textContent = `Looks like a ${data.detail}`;
                } else if (success && useStructuralCheck) {
                    success.textContent = 'Valid Plex config folder';
                } else if (success) {
                    success.textContent = 'Path exists';
                }
            }
        } catch {
            input.classList.remove('is-valid', 'is-invalid');
        }
    }

    function readPathMappingsFromForm() {
        return $$('#editPathMappingsTable tbody tr').map((tr) => {
            const remote = tr.querySelector('.pm-remote').value.trim();
            const local = tr.querySelector('.pm-local').value.trim();
            const webhookRaw = (tr.querySelector('.pm-webhook')?.value || '').trim();
            const webhook_prefixes = webhookRaw
                ? webhookRaw.split(/[;,]/).map((s) => s.trim()).filter(Boolean)
                : [];
            if (!remote && !local && !webhook_prefixes.length) return null;
            return { plex_prefix: remote, local_prefix: local, webhook_prefixes };
        }).filter(Boolean);
    }

    function renderEditExcludePaths(rules) {
        const tbody = $('#editExcludePathsTable tbody');
        tbody.innerHTML = '';
        rules.forEach((row) => addExcludePathRow(row));
        if (!rules.length) addExcludePathRow();
    }

    function addExcludePathRow(row) {
        row = row || {};
        const tbody = $('#editExcludePathsTable tbody');
        const tr = document.createElement('tr');
        const value = row.value || '';
        const type = row.type || 'path';
        tr.innerHTML = `
            <td><input type="text" class="form-control form-control-sm ep-value" value="${escapeHtml(value)}" placeholder="/data/Trailers/"></td>
            <td>
                <select class="form-select form-select-sm ep-type">
                    <option value="path" ${type === 'path' ? 'selected' : ''}>path (prefix)</option>
                    <option value="regex" ${type === 'regex' ? 'selected' : ''}>regex</option>
                </select>
            </td>
            <td><button type="button" class="btn btn-sm btn-outline-danger ep-remove"><i class="bi bi-x-lg"></i></button></td>
        `;
        tr.querySelector('.ep-remove').addEventListener('click', () => tr.remove());
        tbody.appendChild(tr);
    }

    function readExcludePathsFromForm() {
        return $$('#editExcludePathsTable tbody tr').map((tr) => {
            const value = tr.querySelector('.ep-value').value.trim();
            const type = tr.querySelector('.ep-type').value;
            if (!value) return null;
            return { value, type };
        }).filter(Boolean);
    }

    function readEnabledLibraryIds() {
        return $$('.edit-lib-toggle').map((el) => ({
            id: el.dataset.id,
            name: el.dataset.name,
            enabled: el.checked,
        }));
    }

    async function saveEditedServer() {
        if (!_editState) return;
        const { server } = _editState;
        const result = $('#editServerResult');
        const saveBtn = $('#editServerSave');
        saveBtn.disabled = true;
        const orig = saveBtn.innerHTML;
        saveBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Saving…';

        // Build PUT payload — only include fields the user actually changed
        // shape-wise (match server_config_to_dict's expected keys).
        const payload = {
            name: $('#editServerDisplayName').value.trim() || server.name,
            url: $('#editServerUrl').value.trim(),
            verify_ssl: $('#editServerVerifySsl').checked,
            enabled: $('#editServerEnabled').checked,
            path_mappings: readPathMappingsFromForm(),
            exclude_paths: readExcludePathsFromForm(),
        };

        // Token: only send when the user typed something. Empty preserves
        // the existing one (matches the api_servers.py PUT redaction rules).
        const newToken = $('#editServerToken').value.trim();
        if (newToken) {
            payload.auth = { ...(server.auth || {}) };
            const method = (server.auth && server.auth.method) || 'token';
            if (method === 'api_key') payload.auth.api_key = newToken;
            else payload.auth.token = newToken;
            payload.auth.method = method;
        }

        // Per-library enabled toggles (preserve other library fields).
        // D23 — build the payload from the DOM toggle inputs as the
        // source of truth (they're what the user actually clicked),
        // then merge any non-toggle fields (paths, kind, etc.) from
        // the cached server.libraries by id. The previous design
        // built ONLY from cached server.libraries, which silently
        // wrote libraries=[] when the user clicked Refresh-libraries
        // mid-modal: the DOM had the freshly-fetched checkboxes but
        // the cache still held the empty list captured at modal open.
        const toggles = readEnabledLibraryIds();
        const cachedById = new Map((server.libraries || []).map((lib) => [String(lib.id), lib]));
        payload.libraries = toggles.map((t) => {
            const cached = cachedById.get(String(t.id)) || {};
            return {
                ...cached,
                id: t.id,
                name: t.name || cached.name || t.id,
                enabled: !!t.enabled,
            };
        });

        // Plex config folder lives under output.
        if ((server.type || '').toLowerCase() === 'plex') {
            payload.output = {
                ...(server.output || {}),
                adapter: 'plex_bundle',
                plex_config_folder: $('#editPlexConfigFolder').value.trim(),
            };
        }

        const r = await api('PUT', `/api/servers/${encodeURIComponent(server.id)}`, payload);
        saveBtn.disabled = false;
        saveBtn.innerHTML = orig;
        if (r.ok) {
            const modalEl = document.getElementById('editServerModal');
            const inst = window.bootstrap.Modal.getInstance(modalEl);
            if (inst) inst.hide();
            loadServers();
        } else {
            result.className = 'alert alert-danger mt-2';
            result.textContent = (r.data && r.data.error) || `Save failed (HTTP ${r.status})`;
        }
    }

    /**
     * Copy the modal's current path_mappings / exclude_paths into every
     * OTHER configured server via PUT. Lets users with shared mounts /
     * shared exclusion rules avoid typing the same list N times.
     */
    async function applyListToAllServers(field, valueProducer, btn) {
        if (!_editState) return;
        const { server, allServers } = _editState;
        const others = allServers.filter((s) => s.id !== server.id);
        if (!others.length) {
            showToast('Nothing to copy', 'No other servers are configured.', 'info');
            return;
        }
        const value = valueProducer();
        const okList = [];
        const failedList = [];
        const orig = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Applying…';
        for (const other of others) {
            const r = await api('PUT', `/api/servers/${encodeURIComponent(other.id)}`, { [field]: value });
            if (r.ok) okList.push(other.name || other.id);
            else failedList.push(`${other.name || other.id}: ${(r.data && r.data.error) || r.status}`);
        }
        btn.disabled = false;
        btn.innerHTML = orig;
        const result = $('#editServerResult');
        if (failedList.length === 0) {
            result.className = 'alert alert-success mt-2';
            result.innerHTML = `<i class="bi bi-check2-circle me-1"></i>Copied ${field} to ${okList.length} other server${okList.length === 1 ? '' : 's'}.`;
        } else {
            result.className = 'alert alert-warning mt-2';
            result.innerHTML = `<i class="bi bi-exclamation-triangle me-1"></i>Copied to ${okList.length}/${others.length} server${others.length === 1 ? '' : 's'}; failures:<br>${failedList.map(escapeHtml).join('<br>')}`;
        }
    }

    async function refreshLibrariesFromModal(btn) {
        const id = ($('#editServerId').value || '').trim();
        if (!id) return;
        const original = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Refreshing…';
        try {
            const r = await api('POST', `/api/servers/${encodeURIComponent(id)}/refresh-libraries`);
            if (!r.ok) {
                showToast('Refresh failed', `${(r.data && r.data.error) || r.status}`, 'danger');
                return;
            }
            // Re-fetch the server payload so the Libraries tab repaints with
            // fresh names without forcing the user to close and re-open.
            // /api/servers/<id> returns the server dict directly (matches
            // openEditModal's `const server = singleR.data;` earlier in this
            // file) — not wrapped under `.server`.
            const fresh = await api('GET', `/api/servers/${encodeURIComponent(id)}`);
            if (fresh.ok && fresh.data) {
                renderEditLibraries(fresh.data.libraries || []);
                // D23 — sync the cached server payload so saveEditedServer
                // sees the freshly-fetched libraries, not the stale [] it
                // captured at modal open. Without this, ticking checkboxes
                // and clicking Save would write libraries=[] to the
                // server (because (server.libraries || []).map(...) is []).
                if (_editState && _editState.server) {
                    _editState.server.libraries = fresh.data.libraries || [];
                }
            }
            // Also refresh the cards on the page (counts changed).
            loadServers();
        } finally {
            btn.disabled = false;
            btn.innerHTML = original;
        }
    }

    document.addEventListener('DOMContentLoaded', () => {
        // The Edit Server modal lives only on /servers. servers.js is also
        // loaded on /setup (for MPGShared exports the wizard depends on),
        // so bail when the modal-specific elements aren't on this page —
        // otherwise the very first .addEventListener throws TypeError on
        // null and halts every JS handler that runs after this script,
        // including the wizard's vendor-button click bindings.
        const editServerSaveBtn = document.getElementById('editServerSave');
        if (!editServerSaveBtn) return;
        $('#editAddPathMapping').addEventListener('click', () => addPathMappingRow());
        $('#editAddExcludePath').addEventListener('click', () => addExcludePathRow());
        editServerSaveBtn.addEventListener('click', saveEditedServer);
        $('#editApplyPathMappingsAll').addEventListener('click', (ev) =>
            applyListToAllServers('path_mappings', readPathMappingsFromForm, ev.currentTarget)
        );
        $('#editApplyExcludePathsAll').addEventListener('click', (ev) =>
            applyListToAllServers('exclude_paths', readExcludePathsFromForm, ev.currentTarget)
        );
        const refreshBtn = document.getElementById('editRefreshLibrariesBtn');
        if (refreshBtn) refreshBtn.addEventListener('click', (ev) => refreshLibrariesFromModal(ev.currentTarget));

        const plexBrowseBtn = document.getElementById('editPlexConfigBrowseBtn');
        if (plexBrowseBtn) {
            plexBrowseBtn.addEventListener('click', () => {
                const cfgInput = document.getElementById('editPlexConfigFolder');
                const start = ((cfgInput && cfgInput.value) || '').trim() || '/';
                window.openFolderPicker(start, (picked) => {
                    cfgInput.value = picked;
                    _validateLocalPathInput(cfgInput);
                });
            });
        }
    });

    // Public surface for /setup wizard (and any other page) that needs the
    // same path-mapping row + path-validation behaviour without duplicating
    // the IIFE-private helpers.
    window.MPGShared = window.MPGShared || {};
    window.MPGShared.validateLocalPathInput = _validateLocalPathInput;
    window.MPGShared.debouncedValidatePath = _debouncedValidatePath;
    window.MPGShared.addPathMappingRow = addPathMappingRow;
    // Used by the /setup wizard's vendor picker to enter the inlined
    // connection form at "step-connect" without going through #step-type
    // (which only exists in the modal).
    window.MPGShared.pickVendor = pickVendorAndAdvance;
    window.MPGShared.resetServerWizard = resetWizard;
})();
