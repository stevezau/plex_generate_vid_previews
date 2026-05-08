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
                if (!await appConfirm(`Delete media server "${name}"? Previews already published to this server stay on disk; this only removes the configuration entry.`, { title: 'Delete media server', confirmText: 'Delete' })) return;
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
        // Wire readiness glyph click + keyboard activation → open Edit
        // modal so users can jump straight from a warning glyph to the
        // fix-it UI without hunting for the Edit button. role="button"
        // sets the screen-reader expectation that Enter/Space work too.
        $$('.server-readiness-glyph').forEach((glyph) => {
            glyph.addEventListener('click', () => openEditModal(glyph.dataset.id));
            glyph.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    openEditModal(glyph.dataset.id);
                }
            });
        });
        // Quick enable/disable toggle on each card.
        $$('.server-enabled-toggle').forEach((cb) => {
            cb.addEventListener('change', async (ev) => {
                const target = ev.currentTarget;
                const id = target.dataset.id;
                const enabled = target.checked;
                target.disabled = true;
                const r = await api('PATCH', `/api/servers/${encodeURIComponent(id)}/enabled`, { enabled });
                target.disabled = false;
                if (r.ok) {
                    const label = target.parentElement.querySelector('label');
                    if (label) label.textContent = enabled ? 'Enabled' : 'Disabled';
                    showToast('Server updated', `${enabled ? 'Enabled' : 'Disabled'} successfully`, 'success');
                    // Re-probe connection status — disabled servers shouldn't
                    // probe (we'd hit a server the user just paused).
                    if (enabled) probeServerConnection(id);
                    else updateServerStatusPill(id, { ok: null, message: 'Disabled' });
                } else {
                    target.checked = !enabled;  // revert on error
                    showToast('Update failed', `${(r.data && r.data.error) || r.status}`, 'danger');
                }
            });
        });
        // Per-card connection + readiness probe — sequential per server
        // to avoid hammering 3+ servers in parallel from the same
        // browser tab. Each probe is ~200-1500ms. Connection runs
        // first and returns its status; when the server is unreachable
        // we SKIP the readiness probe (the endpoint would error out and
        // paint a misleading "unknown" glyph when the real problem is
        // the connection pill below).
        (async () => {
            for (const s of servers) {
                if (!s.enabled) {
                    updateServerStatusPill(s.id, { ok: null, message: 'Disabled' });
                    updateServerReadinessGlyph(s.id, null);
                    continue;
                }
                const connOk = await probeServerConnection(s.id);
                if (connOk) {
                    await probeServerReadiness(s.id);
                } else {
                    // Connection failed — hide the readiness glyph; the
                    // connection pill already signals the underlying
                    // problem, no need to double-warn.
                    updateServerReadinessGlyph(s.id, { unknown: true });
                }
            }
        })();
    }

    async function probeServerConnection(serverId) {
        try {
            const r = await api('POST', `/api/servers/${encodeURIComponent(serverId)}/test-connection`);
            const data = r.data || {};
            const ok = data.ok === true;
            updateServerStatusPill(serverId, {
                ok,
                message: data.message || (ok ? 'Connected' : 'Connection failed'),
            });
            return ok;
        } catch (e) {
            updateServerStatusPill(serverId, { ok: false, message: String(e) });
            return false;
        }
    }

    function updateServerStatusPill(serverId, { ok, message }) {
        const pill = document.getElementById(`server-status-${serverId}`);
        if (!pill) return;
        if (ok === null) {
            pill.className = 'badge bg-secondary';
            pill.innerHTML = '<i class="bi bi-pause-circle me-1"></i>Disabled';
            pill.title = message || 'Disabled';
            return;
        }
        if (ok) {
            pill.className = 'badge bg-success';
            pill.innerHTML = '<i class="bi bi-check-circle me-1"></i>Connected';
            pill.title = message || 'Connected';
        } else {
            pill.className = 'badge bg-warning text-dark';
            pill.innerHTML = '<i class="bi bi-exclamation-triangle me-1"></i>Auth failed';
            pill.title = message || 'Connection failed';
        }
    }

    async function probeServerReadiness(serverId) {
        // Unified Setup Health probe — drives the inline glyph next to
        // each server name on the card. Same endpoint the Edit modal's
        // Setup Health section consumes, so the glyph and modal agree
        // without a second probe. Single source of truth.
        try {
            const r = await api('GET', `/api/servers/${encodeURIComponent(serverId)}/previews-readiness`);
            if (!r.ok || !r.data) {
                updateServerReadinessGlyph(serverId, { unknown: true });
                return;
            }
            // Roll up sections[] to the worst severity present. Mirrors
            // the modal's _deriveBadgeState walker — BOTH section-level
            // (section.ok/severity) AND per-check (check.ok/severity)
            // are checked so the glyph and the modal badge can never
            // disagree. Per-check counts drive the tooltip copy; a
            // section-level-only failure still registers in anyCritical
            // / anyRecommended even when its checks[] is empty.
            const sections = r.data.sections || [];
            let anyCritical = false;
            let anyRecommended = false;
            let criticalCount = 0;
            let recommendedCount = 0;
            for (const section of sections) {
                if (section.ok === false) {
                    if (section.severity === 'critical') anyCritical = true;
                    else if (section.severity === 'recommended') anyRecommended = true;
                }
                for (const check of (section.checks || [])) {
                    if (check.ok === false && check.severity === 'critical') {
                        anyCritical = true;
                        criticalCount += 1;
                    } else if (check.ok === false && check.severity === 'recommended') {
                        anyRecommended = true;
                        recommendedCount += 1;
                    }
                }
            }
            if (anyCritical) {
                // criticalCount is the per-check count; a section-level
                // failure without a matching check row still trips
                // anyCritical but contributes 0 to the count — fall
                // back to generic copy in that case so "0 critical
                // setup issues" never shows up.
                const tooltip = criticalCount > 0
                    ? `${criticalCount} critical setup issue${criticalCount === 1 ? '' : 's'} — click to fix`
                    : 'Critical setup issue — click to fix';
                updateServerReadinessGlyph(serverId, { state: 'critical', tooltip });
            } else if (anyRecommended) {
                const tooltip = recommendedCount > 0
                    ? `${recommendedCount} recommended improvement${recommendedCount === 1 ? '' : 's'} — click to review`
                    : 'Recommended improvement available — click to review';
                updateServerReadinessGlyph(serverId, { state: 'recommended', tooltip });
            } else {
                updateServerReadinessGlyph(serverId, {
                    state: 'ok',
                    tooltip: 'Setup healthy — click for details',
                });
            }
        } catch (e) {
            updateServerReadinessGlyph(serverId, { unknown: true });
        }
    }

    function updateServerReadinessGlyph(serverId, info) {
        const glyph = document.getElementById(`server-readiness-${serverId}`);
        if (!glyph) return;
        // info === null → card is Disabled; hide the glyph entirely.
        // Preserve the marker class so any post-render traversal that
        // looks for .server-readiness-glyph still finds it (e.g. if a
        // future enable-toggle path re-runs wireup on an existing card).
        if (info === null) {
            glyph.className = 'server-readiness-glyph d-none';
            return;
        }
        if (info.unknown) {
            // Connection failed / probe errored — stay neutral rather
            // than flashing a red warning. The connection pill below
            // already shows the underlying problem.
            glyph.className = 'server-readiness-glyph d-none';
            return;
        }
        const base = 'server-readiness-glyph ms-2';
        if (info.state === 'critical') {
            glyph.className = `${base} text-danger`;
            glyph.innerHTML = '<i class="bi bi-exclamation-triangle-fill"></i>';
        } else if (info.state === 'recommended') {
            glyph.className = `${base} text-warning`;
            glyph.innerHTML = '<i class="bi bi-exclamation-circle-fill"></i>';
        } else {
            glyph.className = `${base} text-success`;
            glyph.innerHTML = '<i class="bi bi-check-circle-fill"></i>';
        }
        glyph.title = info.tooltip || '';
        glyph.setAttribute('aria-label', info.tooltip || '');
    }

    function serverCard(server) {
        const libCount = (server.libraries || []).length;
        const enabledLibs = (server.libraries || []).filter((l) => l.enabled).length;
        // Vendor SVG logo (24px) prepended to the server name — the logo IS
        // the vendor signal, no need for a redundant text badge alongside.
        const vendorLogo = ['plex', 'emby', 'jellyfin'].includes((server.type || '').toLowerCase())
            ? `<img src="/static/images/vendors/${escapeHtml(server.type.toLowerCase())}.svg" alt="${escapeHtml(server.type)}" width="24" height="24" style="margin-right: 8px; vertical-align: -5px;">`
            : '';
        // Connection status pill — populated lazily after card render via
        // _refreshServerCardStatus(). Starts as "Checking…" so users get
        // immediate feedback that the probe is running. Same colour map
        // as the System & Workers card for visual consistency.
        const statusPillId = `server-status-${escapeHtml(server.id)}`;
        const enabledToggleId = `server-enabled-${escapeHtml(server.id)}`;
        const readinessGlyphId = `server-readiness-${escapeHtml(server.id)}`;
        return `
            <div class="col-md-6 col-lg-4">
                <div class="card card-interactive h-100">
                    <div class="card-body">
                        <h5 class="card-title mb-2 d-flex align-items-center" style="min-width:0;">
                            <span style="white-space:nowrap;">${vendorLogo}</span>
                            <span class="text-truncate">${escapeHtml(server.name)}</span>
                            <!-- Inline Setup Health glyph next to the server name.
                                 Populated by probeServerReadiness(). Stays hidden
                                 (d-none) until the probe completes so it doesn't
                                 flash-of-wrong-state on slow networks. -->
                            <span class="server-readiness-glyph d-none"
                                  id="${readinessGlyphId}"
                                  data-id="${escapeHtml(server.id)}"
                                  role="button"
                                  tabindex="0"
                                  style="cursor:pointer;"></span>
                        </h5>
                        <div class="text-muted small mb-2 text-truncate" title="${escapeHtml(server.url)}">${escapeHtml(server.url)}</div>
                        <div class="d-flex align-items-center justify-content-between mb-2 gap-2 flex-wrap">
                            <span class="badge bg-secondary" id="${statusPillId}" title="Connection status">
                                <span class="spinner-border spinner-border-sm me-1" role="status" style="width:0.7em; height:0.7em;"></span>Checking&hellip;
                            </span>
                            <div class="form-check form-switch mb-0" title="Quick enable/disable — when off, this server is ignored by all jobs and webhooks">
                                <input class="form-check-input server-enabled-toggle" type="checkbox"
                                       id="${enabledToggleId}" data-id="${escapeHtml(server.id)}"
                                       ${server.enabled ? 'checked' : ''}>
                                <label class="form-check-label small" for="${enabledToggleId}">${server.enabled ? 'Enabled' : 'Disabled'}</label>
                            </div>
                        </div>
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
            // The wizard used to render an inline "Jellyfin trickplay
            // disabled" warning + "Fix it for me" button here. The
            // button's only side-effect was setting a `_pendingTrickplayFix`
            // attribute that nothing read, so the user got an empty
            // promise that the fix would land "after Save". The
            // unified Server health-check panel on the Edit-Server
            // modal (post-save) covers this case for every vendor and
            // every flag — so we drop the misleading wizard surfacing.
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
    // D24 — Quick Connect poll handle for the Edit-modal flow. Distinct
    // from the wizard's `wizard.quickConnectPoll` so opening Edit while
    // the Add wizard is mid-flight doesn't stomp the wizard's poll.
    let _editReauthQcPoll = null;
    let _editReauthQcSecret = null;

    function _resetEditReauthSection(serverType) {
        const t = String(serverType || '').toLowerCase();
        const plex = document.getElementById('editReauthPlex');
        const jf = document.getElementById('editReauthJellyfin');
        const emby = document.getElementById('editReauthEmby');
        if (!plex || !jf || !emby) return;
        plex.classList.toggle('d-none', t !== 'plex');
        jf.classList.toggle('d-none', t !== 'jellyfin');
        emby.classList.toggle('d-none', t !== 'emby');

        // Reset all inputs / pending state so a previous Edit's
        // values don't leak across.
        const resetIds = [
            'editReauthPlexToken', 'editReauthJfUsername', 'editReauthJfPassword',
            'editReauthJfApiKey', 'editReauthEmbyUsername', 'editReauthEmbyPassword',
            'editReauthEmbyApiKey', 'editReauthPending',
        ];
        resetIds.forEach((id) => {
            const el = document.getElementById(id);
            if (el) el.value = '';
        });
        ['editReauthJfQcStatus', 'editReauthJfPwStatus', 'editReauthEmbyPwStatus'].forEach((id) => {
            const el = document.getElementById(id);
            if (el) {
                el.classList.add('d-none');
                el.innerHTML = '';
                el.className = 'alert alert-info d-none mt-2';
            }
        });
        if (_editReauthQcPoll) {
            clearInterval(_editReauthQcPoll);
            _editReauthQcPoll = null;
        }
        _editReauthQcSecret = null;
        // Reset method radios to defaults.
        const jfDefault = document.getElementById('editReauthJfQc');
        if (jfDefault) jfDefault.checked = true;
        const embyDefault = document.getElementById('editReauthEmbyPw');
        if (embyDefault) embyDefault.checked = true;
        _onEditReauthMethodChange();
    }

    function _onEditReauthMethodChange() {
        const jfMethod = (document.querySelector('input[name="editReauthJfMethod"]:checked') || {}).value;
        const showJf = (m) => (jfMethod === m ? '' : 'd-none');
        const jfQc = document.getElementById('editReauthJfFieldsQc');
        const jfPw = document.getElementById('editReauthJfFieldsPw');
        const jfKey = document.getElementById('editReauthJfFieldsKey');
        if (jfQc) jfQc.className = showJf('quick_connect');
        if (jfPw) jfPw.className = showJf('password');
        if (jfKey) jfKey.className = showJf('api_key');

        const embyMethod = (document.querySelector('input[name="editReauthEmbyMethod"]:checked') || {}).value;
        const showEmby = (m) => (embyMethod === m ? '' : 'd-none');
        const embyPw = document.getElementById('editReauthEmbyFieldsPw');
        const embyKey = document.getElementById('editReauthEmbyFieldsKey');
        if (embyPw) embyPw.className = showEmby('password');
        if (embyKey) embyKey.className = showEmby('api_key');
    }

    function _readEditReauthPayload(server) {
        const t = String(server && server.type || '').toLowerCase();
        // Prefer the JS-stashed verified payload (for Quick Connect /
        // password flows that already round-tripped through the auth
        // endpoint). Falls through to direct field reads for token /
        // api_key paste paths that don't need server-side verification.
        const pendingRaw = (document.getElementById('editReauthPending') || {}).value || '';
        if (pendingRaw) {
            try { return JSON.parse(pendingRaw); } catch (_) { /* fall through */ }
        }
        if (t === 'plex') {
            const tok = (document.getElementById('editReauthPlexToken') || {}).value || '';
            if (!tok.trim()) return null;
            return { method: 'token', token: tok.trim() };
        }
        if (t === 'jellyfin' || t === 'emby') {
            const radioName = t === 'jellyfin' ? 'editReauthJfMethod' : 'editReauthEmbyMethod';
            const method = (document.querySelector('input[name="' + radioName + '"]:checked') || {}).value;
            if (method === 'api_key') {
                const idPrefix = t === 'jellyfin' ? 'editReauthJf' : 'editReauthEmby';
                const k = (document.getElementById(idPrefix + 'ApiKey') || {}).value || '';
                if (!k.trim()) return null;
                return { method: 'api_key', api_key: k.trim() };
            }
            // password and quick_connect flows write the validated
            // {method, access_token, user_id} payload into
            // #editReauthPending when their respective Verify / Approve
            // step succeeds. If pending is empty, nothing to send.
            return null;
        }
        return null;
    }

    async function _editReauthVerifyPassword(vendor) {
        const url = ($('#editServerUrl').value || '').trim();
        if (!url) { showToast('URL required', 'Enter the server URL first.', 'warning'); return; }
        const idPrefix = vendor === 'jellyfin' ? 'editReauthJf' : 'editReauthEmby';
        const u = (document.getElementById(idPrefix + 'Username') || {}).value.trim();
        const p = (document.getElementById(idPrefix + 'Password') || {}).value;
        const status = document.getElementById(idPrefix + 'PwStatus');
        if (!u) { showToast('Username required', '', 'warning'); return; }
        const endpoint = vendor === 'jellyfin'
            ? '/api/servers/auth/jellyfin/password'
            : '/api/servers/auth/emby/password';
        if (status) {
            status.classList.remove('d-none');
            status.className = 'alert alert-info mt-2';
            status.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Verifying…';
        }
        const r = await api('POST', endpoint, { url, username: u, password: p });
        if (!r.ok || !r.data || !r.data.ok) {
            if (status) {
                status.className = 'alert alert-danger mt-2';
                status.textContent = (r.data && r.data.message) || `Auth failed (HTTP ${r.status})`;
            }
            return;
        }
        const payload = {
            method: 'password',
            access_token: r.data.access_token,
            user_id: r.data.user_id,
        };
        document.getElementById('editReauthPending').value = JSON.stringify(payload);
        if (status) {
            status.className = 'alert alert-success mt-2';
            status.innerHTML = '<i class="bi bi-check2-circle me-1"></i>Verified — click <strong>Save changes</strong> to apply.';
        }
    }

    async function _editReauthStartQuickConnect() {
        const url = ($('#editServerUrl').value || '').trim();
        if (!url) { showToast('URL required', 'Enter the Jellyfin URL first.', 'warning'); return; }
        const status = document.getElementById('editReauthJfQcStatus');
        const r = await api('POST', '/api/servers/auth/jellyfin/quick-connect/initiate', { url });
        if (!r.ok || !r.data || !r.data.ok) {
            status.classList.remove('d-none');
            status.className = 'alert alert-danger';
            status.textContent = (r.data && r.data.message) || 'Quick Connect failed';
            return;
        }
        _editReauthQcSecret = r.data.secret;
        const baseUrl = url.replace(/\/+$/, '');
        const qcUrl = baseUrl + '/web/#/quickconnect';
        try { window.open(qcUrl, '_blank', 'noopener,noreferrer'); } catch (_) { /* blocked */ }
        status.classList.remove('d-none');
        status.className = 'alert alert-info';
        status.innerHTML =
            `Opened <a href="${escapeHtml(qcUrl)}" target="_blank" rel="noopener" class="alert-link">Jellyfin Quick Connect</a> in a new tab — paste this code: <strong class="fs-3">${escapeHtml(r.data.code)}</strong>. Waiting for approval…`;

        if (_editReauthQcPoll) clearInterval(_editReauthQcPoll);
        _editReauthQcPoll = setInterval(async () => {
            const p = await api('POST', '/api/servers/auth/jellyfin/quick-connect/poll',
                { url, secret: _editReauthQcSecret });
            if (p.ok && p.data && p.data.authenticated) {
                clearInterval(_editReauthQcPoll);
                _editReauthQcPoll = null;
                const e = await api('POST', '/api/servers/auth/jellyfin/quick-connect/exchange',
                    { url, secret: _editReauthQcSecret });
                if (e.ok && e.data && e.data.ok) {
                    document.getElementById('editReauthPending').value = JSON.stringify({
                        method: 'quick_connect',
                        access_token: e.data.access_token,
                        user_id: e.data.user_id,
                    });
                    status.className = 'alert alert-success';
                    status.innerHTML = `<i class="bi bi-check2-circle me-1"></i>Approved as ${escapeHtml(e.data.server_name || 'Jellyfin user')} — click <strong>Save changes</strong> to apply.`;
                } else {
                    status.className = 'alert alert-danger';
                    status.textContent = (e.data && e.data.message) || 'Token exchange failed';
                }
            }
        }, 2000);
    }

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

        $('#editServerName').textContent = server.name || '';
        // Show the vendor logo next to the title — replaces the old text
        // type-badge ("plex" / "emby" / "jellyfin") which was redundant
        // because the icon already conveys the vendor.
        const vendorLogo = $('#editServerVendorLogo');
        if (vendorLogo) {
            const t = (server.type || '').toLowerCase();
            if (['plex', 'emby', 'jellyfin'].includes(t)) {
                vendorLogo.src = `/static/images/vendors/${t}.svg`;
                vendorLogo.alt = t;
                vendorLogo.classList.remove('d-none');
            } else {
                vendorLogo.classList.add('d-none');
            }
        }
        $('#editServerId').value = server.id || '';
        $('#editServerType').value = server.type || '';
        $('#editServerDisplayName').value = server.name || '';
        $('#editServerUrl').value = server.url || '';
        $('#editServerVerifySsl').checked = server.verify_ssl !== false;
        $('#editServerEnabled').checked = server.enabled !== false;
        // Reset the test-connection result so a stale "Connected" from
        // the previous Edit doesn't carry over.
        const tcResult = document.getElementById('editTestConnectionResult');
        if (tcResult) {
            tcResult.className = 'small text-muted';
            tcResult.textContent = '';
        }
        // Unified "Previews readiness" card — one probe per modal open.
        // Fire-and-forget so the modal opens instantly; the card renders
        // itself when the probe returns.
        runReadinessProbe(server.id, server.type || '');

        // D24 — vendor-aware re-auth UI: show ONE block matching the
        // server's type, hide the others, and reset all input state so
        // an old value from a previous Edit doesn't leak across.
        _resetEditReauthSection(server.type || '');

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

        // D24 — vendor-aware re-auth: build payload.auth from whichever
        // section is visible AND has new content. Empty section means
        // "leave existing auth alone" (matches api_servers.py PUT
        // redaction rules — omitting auth preserves the on-disk value).
        const newAuth = _readEditReauthPayload(server);
        if (newAuth) {
            payload.auth = newAuth;
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

    async function setVendorExtraction(scanExtraction) {
        const id = ($('#editServerId').value || '').trim();
        if (!id) return;
        const result = document.getElementById('editVendorExtractionResult');
        const disableBtn = document.getElementById('editDisableVendorExtractionBtn');
        const enableBtn = document.getElementById('editEnableVendorExtractionBtn');
        const both = [disableBtn, enableBtn].filter(Boolean);
        const labels = both.map(b => b.innerHTML);
        both.forEach(b => { b.disabled = true; });
        const targetBtn = scanExtraction ? enableBtn : disableBtn;
        if (targetBtn) targetBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Working…';
        result.className = 'small text-muted';
        result.textContent = '';
        try {
            const r = await api('POST', `/api/servers/${encodeURIComponent(id)}/vendor-extraction`, { scan_extraction: scanExtraction });
            const data = r.data || {};
            const okCount = data.ok_count || 0;
            const skippedCount = data.skipped_count || 0;
            const errorCount = data.error_count || 0;
            const total = data.total || 0;
            const verb = scanExtraction ? 'Re-enabled' : 'Disabled';
            const parts = [`${okCount}/${total} libraries`];
            if (skippedCount > 0) parts.push(`${skippedCount} skipped (custom agent — toggle in Plex UI)`);
            if (errorCount > 0) parts.push(`${errorCount} failed`);
            if (errorCount === 0) {
                result.className = skippedCount > 0 ? 'small text-warning' : 'small text-success';
                result.innerHTML = `<i class="bi bi-${skippedCount > 0 ? 'info-circle' : 'check-circle'} me-1"></i>${verb}: ${parts.join(' · ')}`;
            } else {
                result.className = 'small text-danger';
                result.innerHTML = `<i class="bi bi-exclamation-triangle me-1"></i>${verb}: ${parts.join(' · ')} — see Logs page`;
            }
        } catch (e) {
            result.className = 'small text-danger';
            result.textContent = String(e);
        } finally {
            both.forEach((b, i) => { b.disabled = false; b.innerHTML = labels[i]; });
            // Re-probe so the panel snaps to the new state's CTA.
            renderVendorExtractionState(id);
        }
    }

    async function renderVendorExtractionState(serverId) {
        // Probe per-library state and pick the right CTA. Avoids
        // showing both Disable and Re-enable when one of them would
        // be a no-op. Critical → red, mixed → yellow,
        // already-recommended → success message + small Re-enable link.
        const disableBtn = document.getElementById('editDisableVendorExtractionBtn');
        const enableBtn = document.getElementById('editEnableVendorExtractionBtn');
        const stateMsg = document.getElementById('editVendorExtractionState');
        if (!disableBtn || !enableBtn) return;
        // Default to hiding both until the probe answers.
        disableBtn.classList.add('d-none');
        enableBtn.classList.add('d-none');
        if (stateMsg) { stateMsg.className = 'small text-muted'; stateMsg.textContent = 'Checking…'; }

        const r = await api('GET', `/api/servers/${encodeURIComponent(serverId)}/vendor-extraction/status`);
        if (!r.ok || !r.data) {
            // Probe failed — show both buttons so the user can still act manually.
            disableBtn.classList.remove('d-none');
            enableBtn.className = 'btn btn-sm btn-outline-secondary';
            if (stateMsg) { stateMsg.className = 'small text-warning'; stateMsg.textContent = 'Could not check current state — try Test Connection above.'; }
            return;
        }

        const { extracting_count = 0, stopped_count = 0, skipped_count = 0, total = 0 } = r.data;
        if (stateMsg) {
            const fragments = [];
            if (stopped_count > 0) fragments.push(`${stopped_count}/${total} disabled`);
            if (skipped_count > 0) fragments.push(`${skipped_count} skipped (custom agent — toggle in Plex UI)`);
            stateMsg.textContent = fragments.length > 0 ? fragments.join(' · ') : '';
            stateMsg.className = 'small text-muted';
        }

        if (extracting_count > 0) {
            // At least one library is still doing its own extraction —
            // primary action is "disable on this server" (idempotent for
            // libraries already disabled).
            disableBtn.classList.remove('d-none');
            disableBtn.disabled = false;
            disableBtn.innerHTML = '<i class="bi bi-stop-circle me-1"></i>Disable on this server';
        } else {
            // All libraries at recommended state — hide Disable, keep Re-enable available for revert.
            disableBtn.classList.add('d-none');
            enableBtn.className = 'btn btn-sm btn-outline-secondary';
            enableBtn.disabled = false;
            enableBtn.innerHTML = 'Re-enable';
            if (stateMsg) {
                stateMsg.className = 'small text-success';
                stateMsg.innerHTML = `<i class="bi bi-check-circle me-1"></i>Server isn't generating its own previews. ${skipped_count > 0 ? `(${skipped_count} library could not be checked — toggle in Plex UI.)` : ''}`;
            }
        }
    }

    async function testEditConnection() {
        const id = ($('#editServerId').value || '').trim();
        if (!id) return;
        const btn = document.getElementById('editTestConnectionBtn');
        const result = document.getElementById('editTestConnectionResult');
        const original = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Testing…';
        result.className = 'small text-muted';
        result.textContent = '';
        try {
            const r = await api('POST', `/api/servers/${encodeURIComponent(id)}/test-connection`);
            const data = r.data || {};
            if (data.ok) {
                result.className = 'small text-success';
                result.innerHTML = `<i class="bi bi-check-circle me-1"></i>Connected${data.version ? ` &mdash; ${escapeHtml(data.version)}` : ''}`;
            } else {
                result.className = 'small text-warning';
                result.innerHTML = `<i class="bi bi-exclamation-triangle me-1"></i>${escapeHtml(data.message || 'Connection failed')}`;
            }
            // Plugin badge — only present in the response for Jellyfin
            // servers that connected successfully. updateJellyfinPluginPanel
            // hides the panel for non-Jellyfin and missing-plugin cases.
            updateJellyfinPluginPanel(data.plugin);
        } catch (e) {
            result.className = 'small text-danger';
            result.textContent = String(e);
        } finally {
            btn.disabled = false;
            btn.innerHTML = original;
        }
    }

    // ─── Unified "Previews readiness" card ─────────────────────────────
    // Section-based renderer — replaces the flat row list with three
    // labelled sections and inline per-issue CTAs:
    //
    //   STATUS           ─ version + activation mode row + inline
    //                      [Install plugin] button when plugin absent.
    //   LIBRARY SETTINGS ─ per-issue [Fix this] buttons + a global
    //                      [Fix all library settings] shortcut.
    //   SERVER OPTIONS   ─ Jellyfin TrickplayOptions row + [Sync
    //                      options] button when geometry mismatches.
    //
    // The "Fix and enable everything" button at the bottom is still
    // there for users who want one-click triage.
    async function runReadinessProbe(serverId, serverType) {
        const group = document.getElementById('editReadinessGroup');
        const badge = document.getElementById('editReadinessBadge');
        const body = document.getElementById('editReadinessBody');
        const fixCtl = document.getElementById('editReadinessFixControls');
        const fixResult = document.getElementById('editReadinessFixResult');
        const pluginCtl = document.getElementById('editReadinessPluginControls');
        if (!group || !badge || !body || !fixCtl) return;

        // Reset state from any prior modal open.
        group.classList.remove('d-none');
        body.innerHTML = '';
        fixCtl.classList.add('d-none');
        if (pluginCtl) pluginCtl.classList.add('d-none');
        if (fixResult) { fixResult.className = 'small text-muted'; fixResult.textContent = ''; }
        badge.className = 'badge ms-1 bg-secondary';
        badge.textContent = 'checking…';

        const r = await api('GET', `/api/servers/${encodeURIComponent(serverId)}/previews-readiness`);
        if (!r.ok || !r.data) {
            badge.className = 'badge ms-1 bg-warning text-dark';
            badge.textContent = 'unavailable';
            body.innerHTML = '<div class="small text-muted">Could not reach the server. Check connection and try again.</div>';
            return;
        }

        renderReadiness(serverId, serverType, r.data);
    }

    // Badge derivation — 5 possible labels driven by the unified envelope:
    //   red   "action needed"     — any critical section failing
    //   amber "recommendations"   — non-critical issues only
    //   green "ready (instant)"   — Jellyfin + plugin installed
    //   green "ready (next scan)" — Jellyfin without plugin (Mode B is valid)
    //   green "ready"             — everything ok, no plugin concept (Plex/Emby)
    //
    // Walks sections[] and rolls up severity. Drives the sub-label
    // off the plugin section's current state — no vendor branching
    // needed in the call site.
    function _deriveBadgeState(data) {
        const sections = data.sections || [];
        let anyCritical = false;
        let anyRecommended = false;
        let pluginInstalled = null;
        for (const section of sections) {
            if (section.ok === false && section.severity === 'critical') {
                anyCritical = true;
            }
            if (section.ok === false && section.severity === 'recommended') {
                anyRecommended = true;
            }
            if (section.id === 'plugin' && section.checks && section.checks.length) {
                // Plugin section carries the installed bit in the first row's
                // ``current`` value ("installed"/"not installed"/version).
                pluginInstalled = section.checks[0].current !== 'not installed';
            }
            for (const check of section.checks || []) {
                if (check.ok === false && check.severity === 'critical') anyCritical = true;
                if (check.ok === false && check.severity === 'recommended') anyRecommended = true;
            }
        }
        if (anyCritical) return { cls: 'bg-danger', text: 'action needed' };
        if (anyRecommended) return { cls: 'bg-warning text-dark', text: 'recommendations' };
        if (pluginInstalled === true) return { cls: 'bg-success', text: 'ready (instant)' };
        if (pluginInstalled === false) return { cls: 'bg-success', text: 'ready (next scan)' };
        return { cls: 'bg-success', text: 'ready' };
    }

    // Renders the unified previews-readiness card. Walks data.sections[]
    // and for each check row emits: icon + label + ⓘ tooltip (with a
    // docs anchor link) + enable/disable toggles (data-driven from
    // check.actions). Toggles carrying a non-null confirm blob route
    // through the #readinessConfirmModal.
    //
    // No vendor branching in this function — everything is driven by
    // what the server emitted. Vendors control section set + copy.
    function renderReadiness(serverId, serverType, data) {
        const badge = document.getElementById('editReadinessBadge');
        const body = document.getElementById('editReadinessBody');
        const fixCtl = document.getElementById('editReadinessFixControls');
        const pluginCtl = document.getElementById('editReadinessPluginControls');
        if (!badge || !body || !fixCtl) return;

        const sections = data.sections || [];

        // Badge.
        const badgeState = _deriveBadgeState(data);
        badge.className = `badge ms-1 ${badgeState.cls}`;
        badge.textContent = badgeState.text;

        // Body.
        body.innerHTML = '';
        for (const section of sections) {
            const sec = _makeSection(section.title || section.id, section.docs_anchor);
            const checks = section.checks || [];
            if (checks.length === 0) {
                sec.appendChild(_makeRow({ ok: section.ok !== false, label: 'OK' }));
            } else {
                for (const check of checks) {
                    sec.appendChild(_renderCheckRow(serverId, serverType, check));
                }
            }
            body.appendChild(sec);
        }

        // "Fix and enable" one-click button — still useful for users
        // who want everything flipped at once. Show whenever ANY check
        // in any section is ok=false (i.e. something could be fixed).
        const anyFixable = sections.some((s) =>
            s.ok === false || (s.checks || []).some((c) => c.ok === false)
        );
        if (anyFixable) {
            fixCtl.classList.remove('d-none');
        } else {
            fixCtl.classList.add('d-none');
        }

        // Hide the legacy plugin opt-in checkbox — install happens
        // inline via the per-check toggle now.
        if (pluginCtl) pluginCtl.classList.add('d-none');

        // Re-init Bootstrap tooltips on any new ⓘ icons.
        if (typeof _initBootstrapTooltips === 'function') {
            _initBootstrapTooltips(body);
        } else if (window.bootstrap && window.bootstrap.Tooltip) {
            body.querySelectorAll('[data-bs-toggle="tooltip"]').forEach((el) => new window.bootstrap.Tooltip(el));
        }
    }

    // Render one check row. Emits: status icon + label + ⓘ tooltip
    // (anchored to docs page) + severity badge + inline [Enable] /
    // [Disable] toggles built from check.actions. Missing actions =
    // hide that toggle.
    function _renderCheckRow(serverId, serverType, check) {
        const row = document.createElement('div');
        row.className = 'd-flex align-items-start gap-2 mb-1 small';

        const ok = check.ok !== false;
        const sev = check.severity || 'info';
        let icon;
        if (ok) {
            icon = '<i class="bi bi-check-circle-fill text-success mt-1"></i>';
        } else if (sev === 'critical') {
            icon = '<i class="bi bi-x-circle-fill text-danger mt-1"></i>';
        } else {
            icon = '<i class="bi bi-exclamation-triangle-fill text-warning mt-1"></i>';
        }

        const anchor = check.docs_anchor
            ? `/docs/guides/previews-readiness.html#${encodeURIComponent(check.docs_anchor)}`
            : '';
        const tooltip = check.tooltip || '';
        const infoIcon = tooltip
            ? `<a href="${escapeAttr(anchor)}" target="_blank" rel="noopener" class="text-muted ms-1" `
                + `data-bs-toggle="tooltip" title="${escapeAttr(tooltip)}">`
                + `<i class="bi bi-info-circle"></i></a>`
            : '';

        const currentStr = check.current === null || check.current === undefined
            ? ''
            : `<div class="text-muted">Currently <code>${formatHealthValue(check.current)}</code></div>`;

        const reasonStr = check.reason
            ? `<div class="text-muted">${escapeHtml(check.reason)}</div>`
            : '';

        const labelHtml = escapeHtml(check.label || check.id || '');
        row.innerHTML = `${icon}<div class="flex-grow-1">${labelHtml}${infoIcon}${reasonStr}${currentStr}</div>`;

        // Per-check toggle buttons.
        const actions = check.actions || {};
        const btnWrap = document.createElement('div');
        btnWrap.className = 'd-flex gap-1 flex-wrap';
        if (actions.enable) {
            const btn = _makeActionButton('btn-outline-success', 'bi-toggle-on', 'Enable', check, 'enable');
            btn.addEventListener('click', () => _runCheckAction(serverId, serverType, check, 'enable', btn));
            btnWrap.appendChild(btn);
        }
        if (actions.disable) {
            const btn = _makeActionButton('btn-outline-danger', 'bi-toggle-off', 'Disable', check, 'disable');
            btn.addEventListener('click', () => _runCheckAction(serverId, serverType, check, 'disable', btn));
            btnWrap.appendChild(btn);
        }
        if (btnWrap.children.length > 0) {
            row.querySelector('.flex-grow-1').appendChild(btnWrap);
        }
        return row;
    }

    function _makeActionButton(colorCls, iconCls, text, check, direction) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = `btn btn-sm ${colorCls} mt-1`;
        btn.innerHTML = `<i class="bi ${iconCls} me-1"></i>${text}`;
        btn.title = `${text} ${check.label || check.id || ''}`;
        btn.dataset.direction = direction;
        return btn;
    }

    // Action dispatcher — driven by check.actions[direction].action.
    // Opens the confirm modal first if action.confirm is non-null;
    // otherwise fires the request immediately. Re-probes after every
    // action completes.
    async function _runCheckAction(serverId, serverType, check, direction, btn) {
        const action = (check.actions || {})[direction];
        if (!action) return;
        const confirm = action.confirm;

        const proceed = async () => {
            const original = btn.innerHTML;
            btn.disabled = true;
            btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Applying…';
            try {
                const response = await _dispatchCheckAction(serverId, action);
                if (!response.ok) {
                    showToast('Action failed', response.error || `HTTP ${response.status}`, 'danger');
                    btn.disabled = false;
                    btn.innerHTML = original;
                    return;
                }
                // Re-probe — use convergence polling if the action
                // triggered a Jellyfin restart (install/uninstall).
                if (action.action === 'install_plugin' || action.action === 'uninstall_plugin') {
                    const expected = action.action === 'install_plugin';
                    await reprobeUntilConverged(
                        serverId,
                        serverType,
                        (d) => _pluginInstalledFromEnvelope(d) === expected,
                        { deadlineMs: 90_000, intervalMs: 3_000 },
                    );
                } else {
                    await runReadinessProbe(serverId, serverType);
                }
                showToast('Applied', `${check.label || 'Setting'} updated.`, 'success');
            } catch (e) {
                showToast('Action error', String(e), 'danger');
                btn.disabled = false;
                btn.innerHTML = original;
            }
        };

        if (confirm) {
            _openConfirmModal(confirm, proceed);
        } else {
            proceed();
        }
    }

    // Map an action envelope to the endpoint it drives. Pure data →
    // URL translation; every check's behaviour is determined by what
    // the server emitted in check.actions.
    async function _dispatchCheckAction(serverId, action) {
        const encoded = encodeURIComponent(serverId);
        const args = action.args || {};
        switch (action.action) {
            case 'apply_flag': {
                // New schema: {"set": [{flag, value, library_ids}]}.
                const row = {
                    flag: args.flag,
                    value: args.value,
                    library_ids: args.library_ids || null,
                };
                // Server-side destructive guardrail: if this flip is
                // destructive (confirm.kind='type'), include the phrase
                // in the body so the route's authoriser passes. Without
                // this the route 400s with "requires typed confirmation".
                const body = { set: [row] };
                if (action.confirm && action.confirm.kind === 'type' && action.confirm.phrase) {
                    body.confirm = { [args.flag]: action.confirm.phrase };
                }
                const r = await api('POST', `/api/servers/${encoded}/health-check/apply`, body);
                return { ok: !!(r.data && r.data.ok !== false) && r.ok, error: r.data && r.data.error, status: r.status };
            }
            case 'install_plugin': {
                const r = await api('POST', `/api/servers/${encoded}/install-plugin`, {});
                return { ok: !!(r.data && r.data.ok) && r.ok, error: r.data && r.data.error, status: r.status };
            }
            case 'uninstall_plugin': {
                const r = await api('POST', `/api/servers/${encoded}/uninstall-plugin`, {});
                return { ok: !!(r.data && r.data.ok) && r.ok, error: r.data && r.data.error, status: r.status };
            }
            case 'sync_trickplay_options': {
                const r = await api('POST', `/api/servers/${encoded}/trickplay-fix-all`, { install_plugin: false });
                return { ok: !!(r.data && r.data.ok) && r.ok, error: r.data && r.data.error, status: r.status };
            }
            case 'set_vendor_extraction': {
                const r = await api('POST', `/api/servers/${encoded}/vendor-extraction`, {
                    scan_extraction: !!args.scan_extraction,
                });
                return { ok: !!(r.data && r.data.ok) && r.ok, error: r.data && r.data.error, status: r.status };
            }
            default:
                return { ok: false, error: `Unknown action: ${action.action}`, status: 0 };
        }
    }

    // Open the destructive-toggle confirm modal. `confirm` is the
    // server-supplied blob: {kind: 'button'|'type', phrase, body}.
    // For kind='type', the submit button stays disabled until the
    // user types the exact phrase — defence in depth alongside the
    // backend guardrails.
    function _openConfirmModal(confirm, onConfirm) {
        const modalEl = document.getElementById('readinessConfirmModal');
        if (!modalEl || !window.bootstrap || !window.bootstrap.Modal) {
            // No modal wiring — fall back to native confirm dialog so
            // destructive actions still require explicit acknowledgement.
            if (window.confirm(confirm.body || 'Are you sure?')) onConfirm();
            return;
        }
        const titleEl = document.getElementById('readinessConfirmTitle');
        const bodyEl = document.getElementById('readinessConfirmBody');
        const typeWrap = document.getElementById('readinessConfirmTypeWrap');
        const phraseEl = document.getElementById('readinessConfirmPhrase');
        const inputEl = document.getElementById('readinessConfirmInput');
        const submitBtn = document.getElementById('readinessConfirmSubmit');

        if (titleEl) titleEl.textContent = 'Confirm action';
        if (bodyEl) bodyEl.textContent = confirm.body || '';
        const kind = confirm.kind || 'button';
        const phrase = confirm.phrase || '';

        // Dispose any prior submit handler by cloning the button
        // BEFORE binding any references — otherwise the input-handler
        // captures the node that's about to be detached and can't
        // toggle the live button in the DOM.
        const newSubmit = submitBtn.cloneNode(true);
        submitBtn.parentNode.replaceChild(newSubmit, submitBtn);

        if (kind === 'type' && phrase) {
            if (typeWrap) typeWrap.classList.remove('d-none');
            if (phraseEl) phraseEl.textContent = phrase;
            if (inputEl) inputEl.value = '';
            newSubmit.disabled = true;
            if (inputEl) {
                inputEl.oninput = () => {
                    newSubmit.disabled = inputEl.value !== phrase;
                };
            }
        } else {
            if (typeWrap) typeWrap.classList.add('d-none');
            newSubmit.disabled = false;
        }

        newSubmit.addEventListener('click', () => {
            window.bootstrap.Modal.getInstance(modalEl).hide();
            onConfirm();
        });

        const modal = window.bootstrap.Modal.getOrCreateInstance(modalEl);
        modal.show();
    }

    // Read plugin-installed bit from a unified envelope. Used by
    // reprobeUntilConverged for install/uninstall actions.
    function _pluginInstalledFromEnvelope(data) {
        const sections = (data && data.sections) || [];
        const plugin = sections.find((s) => s.id === 'plugin');
        if (!plugin || !plugin.checks || !plugin.checks.length) return null;
        return plugin.checks[0].current !== 'not installed';
    }

    function _makeSection(title, docsAnchor) {
        const sec = document.createElement('div');
        sec.className = 'mb-3';
        const heading = document.createElement('div');
        heading.className = 'text-muted small text-uppercase fw-bold mb-1 d-flex align-items-center gap-1';
        heading.style.letterSpacing = '0.5px';
        const label = document.createElement('span');
        label.textContent = title;
        heading.appendChild(label);
        if (docsAnchor) {
            const link = document.createElement('a');
            link.href = `/docs/guides/previews-readiness.html#${encodeURIComponent(docsAnchor)}`;
            link.target = '_blank';
            link.rel = 'noopener';
            link.className = 'text-muted';
            link.setAttribute('data-bs-toggle', 'tooltip');
            link.title = 'Open docs for this section';
            link.innerHTML = '<i class="bi bi-info-circle" style="font-size:0.8rem;"></i>';
            heading.appendChild(link);
        }
        sec.appendChild(heading);
        return sec;
    }

    function _makeRow({ ok, severity, label, reason, htmlLabel }) {
        const row = document.createElement('div');
        row.className = 'd-flex align-items-start gap-2 mb-1';
        const icon = ok
            ? '<i class="bi bi-check-circle-fill text-success mt-1"></i>'
            : (severity === 'critical'
                ? '<i class="bi bi-x-circle-fill text-danger mt-1"></i>'
                : '<i class="bi bi-exclamation-triangle-fill text-warning mt-1"></i>');
        const renderedLabel = htmlLabel ? label : escapeHtml(label);
        const detail = reason
            ? `<div class="small text-muted">${escapeHtml(reason)}</div>`
            : '';
        row.innerHTML = `${icon}<div class="flex-grow-1">${renderedLabel}${detail}</div>`;
        return row;
    }

    // Poll /previews-readiness until ``predicate(data)`` holds, or we
    // hit ``deadlineMs``. Used after install/fix actions to reflect the
    // post-action state without racing Jellyfin's ~15-30s restart.
    async function reprobeUntilConverged(serverId, serverType, predicate, { deadlineMs = 60_000, intervalMs = 3_000 } = {}) {
        const start = Date.now();
        while (Date.now() - start < deadlineMs) {
            try {
                const r = await api('GET', `/api/servers/${encodeURIComponent(serverId)}/previews-readiness`);
                if (r.ok && r.data && predicate(r.data)) {
                    renderReadiness(serverId, serverType, r.data);
                    return r.data;
                }
            } catch (_) {
                // Jellyfin restarting — keep polling.
            }
            await new Promise((res) => setTimeout(res, intervalMs));
        }
        // Deadline hit — render the best-effort current state so the
        // user sees WHY we stopped waiting (e.g. badge flips to
        // "action needed" if install failed).
        await runReadinessProbe(serverId, serverType);
        return null;
    }

    async function runReadinessFixAll(serverId, serverType) {
        const fixCtl = document.getElementById('editReadinessFixControls');
        const fixBtn = document.getElementById('editReadinessFixAllBtn');
        const fixResult = document.getElementById('editReadinessFixResult');
        if (!fixCtl || !fixBtn) return;

        const vendor = (serverType || '').toLowerCase();
        const isJellyfin = vendor === 'jellyfin';

        // Figure out whether we actually need to install the plugin.
        // Default: install if Jellyfin AND plugin currently absent per
        // the last-rendered readiness. Legacy checkbox from the old
        // collapse-block is honoured if someone has it ticked.
        const pluginOptIn = document.getElementById('editReadinessPluginOptIn');
        // If the checkbox exists and user explicitly unchecked it,
        // respect that. Otherwise default based on current state.
        let installPlugin = true;
        if (pluginOptIn && pluginOptIn.dataset.userTouched === 'true') {
            installPlugin = !!pluginOptIn.checked;
        }

        const original = fixBtn.innerHTML;
        fixBtn.disabled = true;
        fixBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Fixing…';
        if (fixResult) { fixResult.className = 'small text-muted'; fixResult.textContent = ''; }

        try {
            let r;
            if (isJellyfin) {
                r = await api('POST', `/api/servers/${encodeURIComponent(serverId)}/trickplay-fix-all`, {
                    install_plugin: installPlugin,
                });
            } else {
                // Emby + Plex: the only fixable things are the
                // vendor-extraction toggles. Drive those via the
                // existing endpoints.
                r = await api('POST', `/api/servers/${encodeURIComponent(serverId)}/health-check/apply`, {});
            }
            if (!r.ok || !r.data) {
                if (fixResult) {
                    fixResult.className = 'small text-danger';
                    fixResult.textContent = `Failed: HTTP ${r.status}`;
                }
                return;
            }
            const allOk = !!r.data.ok;
            if (fixResult) {
                if (allOk) {
                    fixResult.className = 'small text-success';
                    fixResult.textContent = installPlugin && isJellyfin
                        ? '✓ Fix applied. Waiting for Jellyfin restart…'
                        : '✓ Fix applied. Re-probing…';
                } else {
                    fixResult.className = 'small text-warning';
                    fixResult.textContent = `Some steps failed: ${escapeHtml(r.data.error || 'see logs')}`;
                }
            }
            // Re-probe with convergence polling. If we requested a plugin
            // install, wait until plugin.installed=true (Jellyfin takes
            // 15-30s to restart). Otherwise just reflect the current
            // state immediately.
            if (isJellyfin && installPlugin) {
                await reprobeUntilConverged(
                    serverId,
                    serverType,
                    (d) => _pluginInstalledFromEnvelope(d) === true,
                    { deadlineMs: 90_000, intervalMs: 3_000 },
                );
            } else {
                await runReadinessProbe(serverId, serverType);
            }
        } finally {
            fixBtn.disabled = false;
            fixBtn.innerHTML = original;
        }
    }

    // Legacy health-check probe kept for back-compat; new Edit modal
    // flow uses runReadinessProbe instead.
    async function runHealthCheckProbe(serverId) {
        // Deprecated — no-op. Left in place so external callers (if any)
        // don't raise ReferenceError.
        void serverId;
    }

    function formatHealthValue(v) {
        if (v === true) return 'on';
        if (v === false) return 'off';
        if (v === null || v === undefined) return '—';
        return escapeHtml(String(v));
    }

    function escapeHtml(s) {
        return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }
    function escapeAttr(s) {
        return escapeHtml(s);
    }

    async function applyHealthFixes(serverId) {
        const fixCtl = document.getElementById('editHealthFixControls');
        const fixBtn = document.getElementById('editHealthFixAllBtn');
        const fixResult = document.getElementById('editHealthFixResult');
        if (!fixCtl || !fixBtn) return;

        const original = fixBtn.innerHTML;
        fixBtn.disabled = true;
        fixBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Applying…';
        if (fixResult) { fixResult.className = 'small text-muted'; fixResult.textContent = ''; }

        try {
            const r = await api('POST', `/api/servers/${encodeURIComponent(serverId)}/health-check/apply`, {});
            if (!r.ok || !r.data) {
                if (fixResult) {
                    fixResult.className = 'small text-danger';
                    fixResult.textContent = `Failed: HTTP ${r.status}`;
                }
                return;
            }
            const allOk = !!r.data.ok;
            const okCount = Object.values(r.data.results || {}).filter((v) => v === 'ok').length;
            const errCount = Object.values(r.data.results || {}).filter((v) => v !== 'ok').length;
            if (fixResult) {
                if (allOk) {
                    fixResult.className = 'small text-success';
                    fixResult.textContent = `✓ Applied ${okCount} setting${okCount === 1 ? '' : 's'}`;
                } else if (okCount > 0) {
                    fixResult.className = 'small text-warning';
                    fixResult.textContent = `Applied ${okCount}, ${errCount} failed — see logs`;
                } else {
                    fixResult.className = 'small text-danger';
                    fixResult.textContent = `Failed: ${errCount} error${errCount === 1 ? '' : 's'}`;
                }
            }
            // Re-probe so the panel reflects the new state.
            runHealthCheckProbe(serverId);
        } finally {
            fixBtn.disabled = false;
            fixBtn.innerHTML = original;
        }
    }

    // ─── Media Preview Bridge plugin status / install ──────────────────
    // Drives the Jellyfin-only "Media Preview Bridge plugin" card in the
    // Edit Server modal. Visible only when the connection succeeded AND
    // the server is Jellyfin.
    function updateJellyfinPluginPanel(plugin) {
        const group = document.getElementById('editJellyfinPluginGroup');
        const badge = document.getElementById('editJellyfinPluginBadge');
        const installBtn = document.getElementById('editInstallPluginBtn');
        if (!group || !badge) return;

        if (!plugin) {
            // Non-Jellyfin or connection failed — hide the panel entirely.
            group.classList.add('d-none');
            return;
        }
        group.classList.remove('d-none');
        if (plugin.installed) {
            badge.className = 'badge bg-success ms-1';
            badge.textContent = `installed${plugin.version ? ` · v${plugin.version}` : ''}`;
            if (installBtn) installBtn.classList.add('d-none');
        } else {
            badge.className = 'badge bg-warning text-dark ms-1';
            badge.textContent = 'not installed';
            if (installBtn) installBtn.classList.remove('d-none');
        }
    }

    async function installJellyfinPlugin() {
        const id = ($('#editServerId').value || '').trim();
        if (!id) return;
        const btn = document.getElementById('editInstallPluginBtn');
        const result = document.getElementById('editInstallPluginResult');
        const original = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Installing…';
        result.className = 'small text-muted';
        result.textContent = 'Adding repo, queuing install, requesting Jellyfin restart…';
        try {
            const r = await api('POST', `/api/servers/${encodeURIComponent(id)}/install-plugin`);
            const data = r.data || {};
            if (!data.ok) {
                result.className = 'small text-danger';
                result.innerHTML = `<i class="bi bi-x-circle me-1"></i>${escapeHtml(data.error || 'Install failed')}`;
                return;
            }
            result.className = 'small text-info';
            result.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Jellyfin restarting — polling for plugin (up to 60s)…';

            // Poll the test-connection endpoint every 3s; flip the badge
            // when it reports plugin.installed=true. 60s deadline matches
            // a typical Jellyfin restart on a small install.
            const deadline = Date.now() + 60_000;
            while (Date.now() < deadline) {
                await new Promise((res) => setTimeout(res, 3000));
                try {
                    const probe = await api('POST', `/api/servers/${encodeURIComponent(id)}/test-connection`);
                    const probePlugin = probe.data && probe.data.plugin;
                    if (probePlugin && probePlugin.installed) {
                        updateJellyfinPluginPanel(probePlugin);
                        result.className = 'small text-success';
                        result.innerHTML = `<i class="bi bi-check-circle me-1"></i>Plugin installed (v${escapeHtml(probePlugin.version || '?')}) — Jellyfin will now register published trickplay instantly.`;
                        return;
                    }
                } catch (_) {
                    // Keep polling — Jellyfin may still be down mid-restart.
                }
            }
            result.className = 'small text-warning';
            result.innerHTML = '<i class="bi bi-clock-history me-1"></i>Restart taking longer than expected. Click Test Connection in a minute to re-check the plugin status.';
        } catch (e) {
            result.className = 'small text-danger';
            result.textContent = String(e);
        } finally {
            btn.disabled = false;
            btn.innerHTML = original;
        }
    }

    function copyPluginRepoUrl() {
        const input = document.getElementById('editJellyfinPluginRepoUrl');
        if (!input) return;
        navigator.clipboard.writeText(input.value).then(
            () => showToast('Copied', 'Plugin repo URL copied to clipboard.', 'success'),
            () => {
                input.select();
                document.execCommand('copy');
                showToast('Copied', 'Plugin repo URL copied (fallback).', 'success');
            }
        );
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
        const testConnBtn = document.getElementById('editTestConnectionBtn');
        if (testConnBtn) testConnBtn.addEventListener('click', testEditConnection);
        // Unified "Previews readiness" card (v3).
        const readinessFixBtn = document.getElementById('editReadinessFixAllBtn');
        if (readinessFixBtn) readinessFixBtn.addEventListener('click', () => {
            const id = (_editState && _editState.server && _editState.server.id) || '';
            const type = (_editState && _editState.server && _editState.server.type) || '';
            if (id) runReadinessFixAll(id, type);
        });
        const readinessRecheckBtn = document.getElementById('editReadinessRecheckBtn');
        if (readinessRecheckBtn) readinessRecheckBtn.addEventListener('click', () => {
            const id = (_editState && _editState.server && _editState.server.id) || '';
            const type = (_editState && _editState.server && _editState.server.type) || '';
            if (id) runReadinessProbe(id, type);
        });
        // Plugin opt-out warning — show when the user unticks the checkbox.
        const pluginOptIn = document.getElementById('editReadinessPluginOptIn');
        const pluginOptOutWarning = document.getElementById('editReadinessPluginOptOutWarning');
        if (pluginOptIn && pluginOptOutWarning) {
            pluginOptIn.addEventListener('change', () => {
                pluginOptOutWarning.classList.toggle('d-none', pluginOptIn.checked);
            });
        }

        // D24 — vendor-aware re-auth wiring inside the Edit modal.
        document.querySelectorAll('input[name="editReauthJfMethod"]').forEach((r) =>
            r.addEventListener('change', _onEditReauthMethodChange));
        document.querySelectorAll('input[name="editReauthEmbyMethod"]').forEach((r) =>
            r.addEventListener('change', _onEditReauthMethodChange));
        const jfQcBtn = document.getElementById('editReauthJfQcStart');
        if (jfQcBtn) jfQcBtn.addEventListener('click', _editReauthStartQuickConnect);
        const jfPwBtn = document.getElementById('editReauthJfPwSubmit');
        if (jfPwBtn) jfPwBtn.addEventListener('click', () => _editReauthVerifyPassword('jellyfin'));
        const embyPwBtn = document.getElementById('editReauthEmbyPwSubmit');
        if (embyPwBtn) embyPwBtn.addEventListener('click', () => _editReauthVerifyPassword('emby'));

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
