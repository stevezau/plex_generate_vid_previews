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
        // modal DIRECTLY on the Setup Health tab so a user clicking a
        // ⚠/❗ lands on the fix-it UI without another navigation click.
        // role="button" sets the screen-reader expectation that
        // Enter/Space work too.
        $$('.server-readiness-glyph').forEach((glyph) => {
            const open = () => openEditModal(glyph.dataset.id, { openTab: 'health' });
            glyph.addEventListener('click', open);
            glyph.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    open();
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
            // / anyRecommended ONLY when its checks[] is empty — when
            // checks[] is non-empty, the per-check walk is
            // authoritative AND lets dismissed flags silence the
            // recommended tier (issue #237).
            const sections = r.data.sections || [];
            let anyCritical = false;
            let anyRecommended = false;
            let criticalCount = 0;
            let recommendedCount = 0;
            for (const section of sections) {
                if (!(section.checks && section.checks.length)) {
                    if (section.ok === false) {
                        if (section.severity === 'critical') anyCritical = true;
                        else if (section.severity === 'recommended') anyRecommended = true;
                    }
                }
                for (const check of (section.checks || [])) {
                    if (check.ok === false && check.severity === 'critical') {
                        // Critical: dismiss flag is a no-op, mirrors
                        // partition-layer safety enforcement.
                        anyCritical = true;
                        criticalCount += 1;
                    } else if (check.ok === false && check.severity === 'recommended') {
                        if (check.dismissed === true) continue;
                        anyRecommended = true;
                        recommendedCount += 1;
                    }
                }
            }
            if (anyCritical) {
                const tooltip = criticalCount > 0
                    ? `${criticalCount} critical setup issue${criticalCount === 1 ? '' : 's'} — click to fix`
                    : 'Critical setup issue — click to fix';
                updateServerReadinessGlyph(serverId, { state: 'critical', tooltip, count: criticalCount });
            } else if (anyRecommended) {
                const tooltip = recommendedCount > 0
                    ? `${recommendedCount} recommended improvement${recommendedCount === 1 ? '' : 's'} — click to review`
                    : 'Recommended improvement available — click to review';
                updateServerReadinessGlyph(serverId, { state: 'recommended', tooltip, count: recommendedCount });
            } else {
                updateServerReadinessGlyph(serverId, {
                    state: 'ok',
                    tooltip: 'Setup healthy — click for details',
                    count: 0,
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
        // Visible count badge sits next to the icon for critical /
        // recommended states so users can compare "1 thing" vs "5
        // things" at a glance — pre-redesign the count was hidden in
        // the hover tooltip only. ok state stays icon-only because
        // count=0 + green tick is redundant noise.
        const count = typeof info.count === 'number' ? info.count : 0;
        const countBadge = (info.state !== 'ok' && count > 0)
            ? `<span class="badge readiness-count-badge ${info.state === 'critical' ? 'bg-danger' : 'bg-warning text-dark'}">${count}</span>`
            : '';
        if (info.state === 'critical') {
            glyph.className = `${base} text-danger`;
            glyph.innerHTML = `<i class="bi bi-exclamation-triangle-fill"></i>${countBadge}`;
        } else if (info.state === 'recommended') {
            glyph.className = `${base} text-warning`;
            glyph.innerHTML = `<i class="bi bi-exclamation-circle-fill"></i>${countBadge}`;
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
        // Batch-add was removed (silently mis-pinned config folder on the
        // second server, and forced multi-Plex installs to re-edit every
        // server anyway). One sign-in adds one server; the user signs in
        // again from the next "Add Server" click.

        // Browse button for the Add Server modal's Plex config folder
        // field. The Edit modal already has its own wiring at the bottom
        // of this file (editPlexConfigBrowseBtn → editPlexConfigFolder);
        // this is the matching pair for the partial used in #step-connect
        // and the setup wizard. The button lives inside the shared
        // _server_connection_form partial so both surfaces inherit it,
        // but only the modal needs the click handler bound — the setup
        // wizard hides this partial under #auth-fields-token-plex on
        // /setup (the wizard uses its own wizardPlexConfigFolder block
        // at step 3 with its own browse wiring).
        const plexCfgBrowseBtn = $('#plexConfigFolderBrowseBtn');
        if (plexCfgBrowseBtn) {
            plexCfgBrowseBtn.addEventListener('click', () => {
                const cfgInput = $('#plexConfigFolder');
                if (!cfgInput) return;
                const start = (cfgInput.value || '').trim() || '/';
                window.openFolderPicker(start, (picked) => {
                    cfgInput.value = picked;
                    _validateLocalPathInput(cfgInput);
                });
            });
        }
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
        // Radio (not checkbox) — adding more than one Plex server in a
        // single sign-in always required post-add per-server config
        // (config folder, path mappings) anyway, and the old multi-pick
        // path silently cleared the URL field on the second tick which
        // confused every user who saw it. One pick at a time matches
        // the linear flow: pick → Test connection → Save → (sign in
        // again to add another).
        list.innerHTML = servers.map((s, idx) => {
            const ownedBadge = s.owned ? '<span class="badge bg-success">owned</span>' : '<span class="badge bg-secondary">shared</span>';
            const localBadge = s.local ? '<span class="badge bg-info ms-1">local</span>' : '';
            const sslBadge = s.ssl ? '<span class="badge bg-secondary ms-1">https</span>' : '';
            return `
                <label class="list-group-item d-flex align-items-start gap-2">
                    <input type="radio" name="plexDiscoveredPick" class="form-check-input mt-1 plex-server-pick"
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
                if (!el.checked) return;  // radio "change" fires for the newly-selected one only
                // Always auto-fill: the user picked this server, the
                // form below is now the per-server config page (URL,
                // name pre-filled; config folder + path mappings stay
                // user-controlled). Force-overwrite serverUrl so a
                // stale value from a previous pick doesn't linger.
                $('#serverUrl').value = el.dataset.uri || '';
                if (!$('#serverName').value) $('#serverName').value = el.dataset.name || '';
                // Surface the test-connection CTA visually: scroll it
                // into view so a user on a tall list doesn't have to
                // hunt for "what's next?" after picking.
                const testBtn = document.getElementById('step-connect-test');
                if (testBtn && testBtn.scrollIntoView) {
                    testBtn.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
                }
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
            // Quick Connect only makes sense on Jellyfin — hide just
            // its radio + its label, NOT the parent .btn-group.
            // Issue #247: the previous ``.parentElement`` toggle hid
            // the WHOLE auth-method picker (Quick Connect + Password +
            // API Key) for Emby, leaving the user stuck on the default
            // Password method with no way to reach API Key.
            const hideQuick = type !== 'jellyfin';
            $('#auth-quick').classList.toggle('d-none', hideQuick);
            $$('label[for=auth-quick]').forEach((l) => l.classList.toggle('d-none', hideQuick));
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

    // Render the Emby/Jellyfin webhook info card on the per-server Edit
    // modal's "Webhook & Scanner" tab. Pre-fix the tab was Plex-only;
    // Emby and Jellyfin users had no place to find the webhook URL their
    // plugin should POST to. The backend at
    // /api/settings/<vendor>_webhook/info returns the URL + plugin
    // install instructions; this function paints them.
    async function _renderVendorWebhookSection(server) {
        const card = document.getElementById('editVendorWebhookCard');
        if (!card) return;
        const t = String(server.type || '').toLowerCase();
        if (t !== 'emby' && t !== 'jellyfin') {
            card.classList.add('d-none');
            return;
        }
        card.classList.remove('d-none');

        const headerEl = document.getElementById('editVendorWebhookHeader');
        const urlInput = document.getElementById('editVendorWebhookUrl');
        const headerNameEl = document.getElementById('editVendorWebhookHeaderName');
        const hintEl = document.getElementById('editVendorWebhookPluginHint');
        const stepsEl = document.getElementById('editVendorWebhookSteps');
        const copyBtn = document.getElementById('editVendorWebhookCopyBtn');

        if (headerEl) headerEl.textContent = (t === 'emby' ? 'Emby' : 'Jellyfin') + ' Webhook';
        if (urlInput) urlInput.value = 'Loading…';
        if (hintEl) hintEl.textContent = '';
        if (stepsEl) stepsEl.innerHTML = '';

        let info;
        try {
            const url = '/api/settings/' + t + '_webhook/info?server_id=' + encodeURIComponent(server.id);
            info = await apiGet(url);
        } catch (err) {
            if (urlInput) urlInput.value = '';
            if (hintEl) {
                hintEl.classList.remove('alert-info');
                hintEl.classList.add('alert-danger');
                hintEl.textContent = 'Could not load webhook info: ' + (err && err.message || err);
            }
            return;
        }

        if (urlInput) urlInput.value = info.webhook_url_per_server || info.webhook_url || '';
        // Header name is static prose (X-Auth-Token) — the API confirms
        // it but never returns the value. We deliberately don't expose
        // the actual token in this UI: the user looks it up under
        // Settings → Authentication and pastes it into their plugin.
        if (headerNameEl) headerNameEl.textContent = info.auth_header_name || 'X-Auth-Token';
        if (hintEl) {
            hintEl.classList.remove('alert-danger');
            hintEl.classList.add('alert-info');
            const plugin = info.plugin || {};
            const installLink = plugin.install_url
                ? ` <a href="${plugin.install_url}" target="_blank" rel="noopener">${escapeHtml(plugin.plugin_name || 'plugin')} install instructions ↗</a>`
                : '';
            hintEl.innerHTML = '<i class="bi bi-info-circle me-1"></i>' +
                'You need the ' + escapeHtml(plugin.plugin_name || 'webhook plugin') +
                ' configured on your ' + escapeHtml(t === 'emby' ? 'Emby' : 'Jellyfin') +
                ' server.' + installLink;
        }
        if (stepsEl) {
            const steps = (info.plugin || {}).config_steps || [];
            stepsEl.innerHTML = steps.map(s => '<li>' + escapeHtml(s) + '</li>').join('');
        }

        if (copyBtn && urlInput) {
            copyBtn.onclick = () => {
                const value = urlInput.value;
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    navigator.clipboard.writeText(value).catch(() => {});
                } else {
                    urlInput.select();
                    try { document.execCommand('copy'); } catch (_) {}
                }
                copyBtn.innerHTML = '<i class="bi bi-check2"></i>';
                setTimeout(() => { copyBtn.innerHTML = '<i class="bi bi-clipboard"></i>'; }, 1500);
            };
        }
    }

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

    async function openEditModal(serverId, { openTab = 'general' } = {}) {
        // Fetch the target server + the full server list (needed for the
        // "Apply to all" buttons so we know who to copy to).
        //
        // openTab — which tab to land on. Defaults to 'general' (the
        // historical behaviour). The /servers card glyph passes 'health'
        // so a user clicking a ⚠/❗ lands exactly on the fix-it UI.
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
        // Long server names (e.g. "My Plex Server In The Living Room With Lots Of Movies")
        // used to wrap the modal title onto two lines because the inner span had no
        // text-truncate. The CSS truncates with ellipsis; surface the full name on
        // hover so the truncation isn't lossy.
        const nameWrap = $('#editServerNameWrap');
        if (nameWrap) {
            nameWrap.title = `Edit ${server.name || ''}`.trim();
        }
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
        // itself when the probe returns. Disabled servers MUST NOT be
        // probed (we'd wake a server the user explicitly paused); show a
        // static "Disabled" state instead and let the user re-enable
        // before running checks.
        if (server.enabled === false) {
            _renderReadinessDisabled(server.id);
        } else {
            runReadinessProbe(server.id, server.type || '');
        }

        // D24 — vendor-aware re-auth UI: show ONE block matching the
        // server's type, hide the others, and reset all input state so
        // an old value from a previous Edit doesn't leak across.
        _resetEditReauthSection(server.type || '');

        // Plex-only: show the config folder field + wire its inline validator.
        const isPlex = (server.type || '').toLowerCase() === 'plex';
        $('#editPlexConfigGroup').classList.toggle('d-none', !isPlex);
        // The "Webhook & Scanner" tab now shows for ALL server types.
        // Pre-fix it was hidden for non-Plex servers — closing the user's
        // bug report "Plex has webhook register section but emby and jelly
        // does not? Why?". Per-vendor content is rendered by
        // _renderVendorWebhookSection() below.
        const automationTabLi = document.getElementById('editTabAutomationLi');
        if (automationTabLi) automationTabLi.classList.remove('d-none');
        // Plex Direct Webhook card stays Plex-only (the registration
        // API is Plex-specific). The Recently Added Scanner card
        // applies to every vendor — it just polls the server's
        // recently-added API and dispatches per-item jobs.
        const plexWebhookCard = document.getElementById('editPlexWebhookCard');
        const recentlyAddedCard = document.getElementById('editRecentlyAddedCard');
        if (plexWebhookCard) plexWebhookCard.classList.toggle('d-none', !isPlex);
        // The Recently Added Scanner card is universal — show it
        // for every vendor.
        if (recentlyAddedCard) recentlyAddedCard.classList.remove('d-none');
        _renderVendorWebhookSection(server);
        // Always force the General tab active on open. Without this, opening a
        // Plex server, clicking "Webhook & Scanner", closing, then opening a
        // non-Plex server leaves the now-hidden Plex pane visible because
        // Bootstrap doesn't auto-reset on modal hide. (Fix-2 from H code review.)
        try {
            document.querySelectorAll('#editServerModal .nav-link').forEach((el) => el.classList.remove('active'));
            document.querySelectorAll('#editServerModal .tab-pane').forEach((el) => el.classList.remove('show', 'active'));
            // Map the openTab key to the DOM ids. Unknown values fall
            // back to general rather than silently leaving every tab
            // hidden (the pre-fix symptom would be a blank modal body).
            const tabMap = {
                general: 'edit-tab-general',
                health: 'edit-tab-health',
            };
            const paneId = tabMap[openTab] || 'edit-tab-general';
            const activeTab = document.querySelector(`#editServerModal [data-bs-target="#${paneId}"]`);
            const activePane = document.getElementById(paneId);
            if (activeTab) activeTab.classList.add('active');
            if (activePane) activePane.classList.add('show', 'active');
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

            // Plex-only: load the Plex Direct webhook registration status.
            try {
                if (typeof loadPlexWebhookStatus === 'function') loadPlexWebhookStatus();
                const caption = document.getElementById('editPlexWebhookServerCaption');
                if (caption) caption.innerHTML = `This webhook will be registered with <strong>${escapeHtml(server.name || 'this Plex server')}</strong> using its own Plex token.`;
            } catch (_e) { }
        }

        // Recently Added Scanner panel applies to every vendor. Scope
        // the panel's server-id state so list-filter + create-default
        // target THIS server, then wire buttons + load the list. Pre-
        // fix this lived inside the ``if (isPlex)`` block which is why
        // Emby/Jellyfin users saw an empty "Loading scanners…" spinner.
        try {
            if (typeof setPlexWebhookPanelServerId === 'function') setPlexWebhookPanelServerId(server.id);
            if (typeof _wirePlexWebhookPanel === 'function') _wirePlexWebhookPanel();
            if (typeof loadRecentlyAddedScanners === 'function') loadRecentlyAddedScanners();
        } catch (_e) { }

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

    // Cache of the most recent successful readiness probe payload, keyed
    // by serverId so concurrent edit-modal opens for different servers
    // can't bleed into each other. Used by the bulk Fix buttons to
    // enumerate the change plan. Cleared on probe failure for the same
    // serverId so we don't operate on stale data after that server goes
    // offline. Declared above the function that mutates it so the
    // binding is initialised before any caller can reach it (avoids the
    // latent TDZ footgun a future top-level call would expose).
    const _readinessDataByServer = new Map();

    // Render the readiness card's "Server is disabled" state WITHOUT
    // probing the network. Mirrors the backend's _disabled_response
    // contract on /api/servers/<id>/previews-readiness — the two layers
    // agree so a disabled server is never woken by this modal whether
    // the user opened Edit from the card glyph or via an external link.
    function _renderReadinessDisabled(serverId) {
        const group = document.getElementById('editReadinessGroup');
        const badge = document.getElementById('editReadinessBadge');
        const body = document.getElementById('editReadinessBody');
        const fixCtl = document.getElementById('editReadinessFixControls');
        const fixResult = document.getElementById('editReadinessFixResult');
        const pluginCtl = document.getElementById('editReadinessPluginControls');
        if (!group || !badge || !body || !fixCtl) return;
        group.classList.remove('d-none');
        fixCtl.classList.add('d-none');
        if (pluginCtl) pluginCtl.classList.add('d-none');
        if (fixResult) { fixResult.className = 'small text-muted'; fixResult.textContent = ''; }
        badge.className = 'badge ms-1 bg-secondary';
        badge.textContent = 'disabled';
        body.innerHTML = '<div class="small text-muted"><i class="bi bi-pause-circle me-1"></i>This server is disabled — checks are paused. Re-enable it on the Servers page to run readiness checks.</div>';
        _readinessDataByServer.delete(serverId);
    }

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
            _readinessDataByServer.delete(serverId);
            return;
        }

        // Stash the latest probe payload so the Fix-all / Fix-critical
        // buttons can build a preview plan from it without re-probing
        // (the preview modal needs the data BEFORE making any change).
        _readinessDataByServer.set(serverId, r.data);
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
            // Section-level summary is the FALLBACK for sections that
            // emit no checks[] — when checks[] is non-empty, the
            // per-check walk below is authoritative. The section-level
            // line is dismissed-blind on purpose: dismissed flags live
            // on individual checks, not sections; a section with no
            // checks at all has nothing to silence.
            if (!(section.checks && section.checks.length)) {
                if (section.ok === false && section.severity === 'critical') {
                    anyCritical = true;
                }
                if (section.ok === false && section.severity === 'recommended') {
                    anyRecommended = true;
                }
            }
            if (section.id === 'plugin' && section.checks && section.checks.length) {
                // Plugin section carries the installed bit in the first row's
                // ``current`` value ("installed"/"not installed"/version).
                pluginInstalled = section.checks[0].current !== 'not installed';
            }
            for (const check of section.checks || []) {
                if (check.ok === false && check.severity === 'critical') {
                    // Issue #237: critical failures ignore the
                    // dismissed flag — the safety enforcement at the
                    // partition layer forces them into mustFix
                    // regardless, so the badge must escalate too. A
                    // user can't silence a true blocker by POSTing its
                    // id to the dismiss endpoint.
                    anyCritical = true;
                }
                if (check.ok === false && check.severity === 'recommended') {
                    // Issue #237: recommended dismissals DO silence
                    // the yellow tier. If every recommended check is
                    // dismissed, the badge falls through to "ready" —
                    // the user has acknowledged each row and doesn't
                    // need the modal header nagging anymore. They can
                    // still see what's dismissed under
                    // "All good → Dismissed (N)".
                    if (check.dismissed === true) continue;
                    anyRecommended = true;
                }
            }
        }
        // `tier` is the stable string consumers key off (badge, tab marker, glyph) —
        // using it avoids substring-matching the Bootstrap class list, which
        // would break silently if someone ever added `bg-warning-subtle` etc.
        if (anyCritical) return { tier: 'critical', cls: 'bg-danger', text: 'action needed' };
        if (anyRecommended) return { tier: 'recommended', cls: 'bg-warning text-dark', text: 'recommendations' };
        if (pluginInstalled === true) return { tier: 'ok', cls: 'bg-success', text: 'ready (instant)' };
        if (pluginInstalled === false) return { tier: 'ok', cls: 'bg-success', text: 'ready (next scan)' };
        return { tier: 'ok', cls: 'bg-success', text: 'ready' };
    }

    // Renders the unified previews-readiness card. Walks data.sections[]
    // and groups the checks into three buckets (Must fix / Recommended /
    // All good) so users immediately see what needs their attention vs.
    // what's just informational. Within each bucket the source section
    // appears as a small subheading so context like "Library settings"
    // isn't lost.
    //
    // No vendor branching in this function — everything is driven by
    // what the server emitted. Vendors control section set + copy.
    function renderReadiness(serverId, serverType, data) {
        const badge = document.getElementById('editReadinessBadge');
        const body = document.getElementById('editReadinessBody');
        const fixCtl = document.getElementById('editReadinessFixControls');
        const fixCritBtn = document.getElementById('editReadinessFixCriticalBtn');
        const pluginCtl = document.getElementById('editReadinessPluginControls');
        if (!badge || !body || !fixCtl) return;

        const sections = data.sections || [];

        // Badge.
        const badgeState = _deriveBadgeState(data);
        badge.className = `badge ms-1 ${badgeState.cls}`;
        badge.textContent = badgeState.text;

        // Tab-label marker — stamp ❗ / ⚠ / ✓ next to "Setup Health"
        // in the nav bar so the user sees there's something to
        // address even when they're on a different tab.
        const tabMarker = document.getElementById('editHealthTabMarker');
        if (tabMarker) {
            if (badgeState.tier === 'critical') {
                tabMarker.className = 'ms-1 text-danger';
                tabMarker.innerHTML = '<i class="bi bi-exclamation-triangle-fill"></i>';
                tabMarker.title = 'Action needed';
            } else if (badgeState.tier === 'recommended') {
                tabMarker.className = 'ms-1 text-warning';
                tabMarker.innerHTML = '<i class="bi bi-exclamation-circle-fill"></i>';
                tabMarker.title = 'Recommendations';
            } else {
                tabMarker.className = 'ms-1 d-none';
                tabMarker.innerHTML = '';
                tabMarker.title = '';
            }
        }

        // Bucket the checks once so each bucket can render its own
        // collapsible group with the right icon/colour/expanded state.
        // Bucketing rules:
        //   Must fix      = critical + !ok
        //   Recommended   = !ok (anything not critical, including info)
        //   All good      = ok (everything passing or info-pass)
        const partition = _partitionChecks(sections);

        body.innerHTML = '';
        if (partition.mustFix.length > 0) {
            body.appendChild(_renderBucket({
                serverId,
                serverType,
                tier: 'critical',
                title: 'Must fix',
                items: partition.mustFix,
                expanded: true,
                badgeCls: 'bg-danger',
                iconHtml: '<i class="bi bi-x-octagon-fill text-danger me-2"></i>',
                emptyHint: '',
            }));
        }
        if (partition.recommended.length > 0) {
            body.appendChild(_renderBucket({
                serverId,
                serverType,
                tier: 'recommended',
                title: 'Recommended',
                items: partition.recommended,
                expanded: partition.mustFix.length === 0,
                badgeCls: 'bg-warning text-dark',
                iconHtml: '<i class="bi bi-exclamation-triangle-fill text-warning me-2"></i>',
                emptyHint: '',
            }));
        }
        if (partition.allGood.length > 0) {
            body.appendChild(_renderBucket({
                serverId,
                serverType,
                tier: 'ok',
                title: 'All good',
                items: partition.allGood,
                // Only auto-expand when there's nothing actionable, so a
                // server that's fully healthy still shows its checks
                // up-front instead of an empty card.
                expanded: partition.mustFix.length === 0 && partition.recommended.length === 0,
                badgeCls: 'bg-success',
                iconHtml: '<i class="bi bi-check-circle-fill text-success me-2"></i>',
                emptyHint: '',
            }));
        }

        // Show the fix controls when there are fixable rows (any check
        // with an enable/disable action whose recommended-state flip is
        // actionable). The critical-only button only surfaces when at
        // least one critical row is fixable — otherwise it'd be a
        // dead button alongside "Fix all".
        const fixablePlan = _buildFixPlan(sections, 'all');
        const criticalPlan = _buildFixPlan(sections, 'critical');
        if (fixablePlan.length > 0) {
            fixCtl.classList.remove('d-none');
        } else {
            fixCtl.classList.add('d-none');
        }
        if (fixCritBtn) {
            if (criticalPlan.length > 0) {
                fixCritBtn.classList.remove('d-none');
            } else {
                fixCritBtn.classList.add('d-none');
            }
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

    // Walk every section's checks and assign each to one of three
    // buckets. The original section is preserved on each item so the
    // bucket renderer can group rows by their source section without
    // losing context (e.g. "Library settings → Movies — Trickplay
    // enabled").
    //
    // Bucketing rules:
    //   critical + !ok      → Must fix    (red, expanded)
    //   recommended + !ok   → Recommended (amber, expanded if no critical)
    //   info        + !ok   → All good    (probe-failure rows are not
    //                         actionable improvements; surfacing them
    //                         under "Recommended" would cry-wolf about
    //                         a transient connectivity blip).
    //   ok          + any   → All good
    // Vendor display-name lookup for badge copy that references the
    // user's media server admin UI (e.g. "Change in Plex UI"). Falls
    // back to "server admin" when the type is missing/unknown so the
    // badge still reads sensibly.
    function _vendorDisplayName(serverType) {
        switch ((serverType || '').toLowerCase()) {
            case 'plex': return 'Plex';
            case 'jellyfin': return 'Jellyfin';
            case 'emby': return 'Emby';
            default: return 'server admin';
        }
    }

    function _partitionChecks(sections) {
        const mustFix = [];
        const recommended = [];
        const allGood = [];
        for (const section of sections || []) {
            for (const check of (section.checks || [])) {
                // Drop pure-info rows entirely — they have no
                // recommendation to apply and no failure to fix, so
                // they're decorative noise on a card whose whole job
                // is to surface checks the user should act on. Things
                // that NEED to surface (skipped custom-agent libraries,
                // probe failures, plugin-required-but-absent) must be
                // emitted by the backend as severity="recommended" or
                // "critical" — not "info". This filter is the safety
                // net so any stray info row never sneaks back into the
                // user's eyeline.
                if ((check.severity || 'info') === 'info') continue;
                const item = {
                    check,
                    sectionTitle: section.title || section.id || '',
                    sectionAnchor: section.docs_anchor || '',
                    sectionId: section.id || '',
                };
                // Issue #237: critical checks ALWAYS go to mustFix
                // even if dismissed — the user can't silence a true
                // blocker by POSTing its id to the dismiss endpoint.
                // The backend doesn't validate the dismissed id
                // against current severity; this partition step is
                // the safety enforcement instead.
                if (check.ok === false && check.severity === 'critical') {
                    mustFix.push(item);
                } else if (check.dismissed === true) {
                    // Dismissed non-critical row → tucked under
                    // "All good → Dismissed" so the user can see what
                    // they've silenced and undismiss if they change
                    // their mind. Raw audit state (ok / severity) is
                    // preserved on the check; only the placement
                    // changes.
                    allGood.push(item);
                } else if (check.ok === false && check.severity === 'recommended') {
                    recommended.push(item);
                } else {
                    allGood.push(item);
                }
            }
        }
        return { mustFix, recommended, allGood };
    }

    // Render one bucket as a `<details>` block. Items are grouped by
    // their source section — the section title appears as a small
    // grey subheading so users can still tell whether a row is about
    // the library scan settings or the plugin or path mappings.
    //
    // Issue #237: the "All good" bucket has a second sub-group for
    // dismissed checks — rendered AFTER the passing rows, under a
    // "Dismissed" subhead, so users can see what they've silenced
    // without confusing them with passing-row checkmarks.
    function _renderBucket({ serverId, serverType, tier, title, items, expanded, badgeCls, iconHtml }) {
        const det = document.createElement('details');
        det.className = 'readiness-bucket mb-2';
        det.dataset.tier = tier;
        if (expanded) det.open = true;

        const sum = document.createElement('summary');
        sum.className = 'd-flex align-items-center gap-2 py-1 px-2 rounded user-select-none';
        sum.style.cursor = 'pointer';
        sum.innerHTML = `${iconHtml}<span class="fw-semibold">${escapeHtml(title)}</span>`
            + `<span class="badge ${badgeCls} ms-1">${items.length}</span>`;
        det.appendChild(sum);

        const inner = document.createElement('div');
        inner.className = 'readiness-bucket-body ps-2 pt-2';

        // Split dismissed items out so they render under a separate
        // "Dismissed" subhead at the bottom of the bucket. Done only
        // for the allGood tier (where dismissed items live); other
        // tiers carry no dismissed items by partition.
        const passing = [];
        const dismissed = [];
        for (const item of items) {
            if (tier === 'allGood' && item.check && item.check.dismissed === true) {
                dismissed.push(item);
            } else {
                passing.push(item);
            }
        }

        // Group passing items by section — same stable order as before.
        let lastSectionId = null;
        for (const item of passing) {
            if (item.sectionId !== lastSectionId) {
                lastSectionId = item.sectionId;
                inner.appendChild(_renderSectionSubhead(item.sectionTitle));
            }
            inner.appendChild(_renderCheckRow(serverId, serverType, item.check));
        }

        if (dismissed.length > 0) {
            // Use a distinct subhead so the dismissed rows don't read
            // as "passing under the same section as everything above".
            inner.appendChild(_renderSectionSubhead(`Dismissed (${dismissed.length})`));
            for (const item of dismissed) {
                inner.appendChild(_renderCheckRow(serverId, serverType, item.check));
            }
        }

        det.appendChild(inner);
        return det;
    }

    function _renderSectionSubhead(title) {
        // Section subhead is a plain label — pre-fix this rendered a
        // small ⓘ next to the title that opened
        // https://github.com/…/previews-readiness.md#anchor in a new
        // tab. Users hit 404s on environments where that path doesn't
        // resolve (private fork, unpublished branch, renamed file).
        // Every row already carries its OWN ⓘ that opens the inline
        // explanation modal — section-level docs links were redundant
        // even when they worked. Drop entirely.
        const wrap = document.createElement('div');
        wrap.className = 'text-muted small text-uppercase mb-1 mt-2 d-flex align-items-center gap-1';
        wrap.style.letterSpacing = '0.5px';
        wrap.style.fontWeight = '600';
        const lbl = document.createElement('span');
        lbl.textContent = title;
        wrap.appendChild(lbl);
        return wrap;
    }

    // Render one check row. Emits: status icon + label + ⓘ tooltip
    // (anchored to docs page) + severity badge + side-by-side
    // current/recommended diff + Manual chip when read-only +
    // inline [Enable] / [Disable] toggles built from check.actions.
    function _renderCheckRow(serverId, serverType, check) {
        const row = document.createElement('div');
        row.className = 'd-flex align-items-start gap-2 mb-1 small';

        const ok = check.ok !== false;
        const sev = check.severity || 'info';
        // Info severity is filtered out upstream in _partitionChecks —
        // it never reaches a rendered row. ALL passing rows get a
        // green filled check; pre-fix passing recommended rows got a
        // grey outlined check and users complained that a row "off
        // when recommended off" didn't show as passing.
        //
        // ONE badge per row — pre-fix a failing row with no auto-fix
        // got BOTH "Recommended — server still works" AND a separate
        // "Manual" chip and the dual-badge was confusing. When there's
        // no action button the badge becomes "Change in <vendor> UI"
        // — pre-fix this said "Manual fix needed" but the user noted
        // calling it a "fix" reads as "the app needs to apply a fix"
        // when the app actually CAN'T act on this row at all. The new
        // wording names the actual place the user has to go.
        const actionsObj = check.actions || {};
        const hasFixAction = !!(actionsObj.enable || actionsObj.disable);
        const vendorLabel = _vendorDisplayName(serverType);
        const manualBadgeText = `Change in ${vendorLabel} UI`;
        const manualBadgeTitle = `This app can't toggle this for you — open ${vendorLabel}'s admin UI and follow the instructions below.`;
        let icon;
        let tierBadge;
        if (ok && sev === 'critical') {
            icon = '<i class="bi bi-check-circle-fill text-success mt-1"></i>';
            tierBadge = '<span class="badge bg-success-subtle text-success-emphasis border border-success-subtle ms-1" title="Required check — currently passing.">Required</span>';
        } else if (ok) {
            icon = '<i class="bi bi-check-circle-fill text-success mt-1"></i>';
            tierBadge = '<span class="badge bg-secondary-subtle text-secondary-emphasis border border-secondary-subtle ms-1" title="Recommended optimisation — currently applied.">Recommended</span>';
        } else if (sev === 'critical' && !hasFixAction) {
            icon = '<i class="bi bi-x-circle-fill text-danger mt-1"></i>';
            tierBadge = `<span class="badge bg-danger-subtle text-danger-emphasis border border-danger-subtle ms-1" title="${escapeAttr(manualBadgeTitle)}">${escapeHtml(manualBadgeText)}</span>`;
        } else if (sev === 'critical') {
            icon = '<i class="bi bi-x-circle-fill text-danger mt-1"></i>';
            tierBadge = '<span class="badge bg-danger-subtle text-danger-emphasis border border-danger-subtle ms-1" title="Required for the server to work — apply the fix.">Required — fix to enable</span>';
        } else if (!hasFixAction) {
            icon = '<i class="bi bi-exclamation-triangle-fill text-warning mt-1"></i>';
            tierBadge = `<span class="badge bg-warning-subtle text-warning-emphasis border border-warning-subtle ms-1" title="${escapeAttr(manualBadgeTitle)}">${escapeHtml(manualBadgeText)}</span>`;
        } else {
            icon = '<i class="bi bi-exclamation-triangle-fill text-warning mt-1"></i>';
            tierBadge = '<span class="badge bg-warning-subtle text-warning-emphasis border border-warning-subtle ms-1" title="Recommended improvement — server still works without it.">Recommended</span>';
        }
        // No separate Manual chip — the badge above already says it.
        const manualChip = '';

        // No data-explain-docs — the row's rich explanation is shown
        // inline via the global explain modal (set up by app.js's
        // .info-icon click delegator), and we deliberately don't link
        // out to an external docs page. Pre-fix every row carried a
        // GitHub URL that 404'd on private forks / unpushed branches.
        const tooltip = check.tooltip || '';
        const explanationHtml = check.explanation || '';
        const hasMore = !!explanationHtml;
        const tooltipText = hasMore && tooltip
            ? `${tooltip} — click for details`
            : tooltip;
        const infoIcon = (tooltip || explanationHtml)
            ? `<button type="button" class="info-icon${hasMore ? ' info-icon-more' : ''} ms-1" `
                + `data-bs-toggle="tooltip" data-bs-placement="top" `
                + `title="${escapeAttr(tooltipText || 'Click for details')}" `
                + `data-explain-title="${escapeAttr(check.label || tooltip || 'About this check')}" `
                + `aria-label="Explain ${escapeAttr(check.label || '')}">`
                + `<i class="bi bi-info-circle"></i></button>`
            : '';

        const valuesHtml = _renderValueDiff(check.current, check.recommended, ok, check.label || '');

        const reasonStr = check.reason
            ? `<div class="text-muted mt-1">${escapeHtml(check.reason)}</div>`
            : '';

        const labelHtml = escapeHtml(check.label || check.id || '');
        row.innerHTML = `${icon}<div class="flex-grow-1">${labelHtml}${tierBadge}${manualChip}${infoIcon}${reasonStr}${valuesHtml}</div>`;

        // Attach the rich explanation HTML to the info-icon button as
        // a DOM property — can't round-trip multi-paragraph HTML through
        // a data-attr string (quote/entity soup). The document-level
        // .info-icon click delegator in app.js reads this back on open.
        //
        // Contract: renderReadiness() rebuilds every row on each probe
        // (full innerHTML swap, not diff-based), so the fresh DOM node
        // always carries the latest explanation — no stale-reference
        // window after a re-probe. See renderReadiness above.
        if (explanationHtml) {
            const infoBtn = row.querySelector('.info-icon');
            if (infoBtn) infoBtn._explanationHtml = explanationHtml;
        }

        // Per-check toggle buttons. Two semantic roles:
        //   FIX direction   — matches check.recommended (the "right" answer).
        //   OPPOSITE direction — moves AWAY from recommended (destructive/opt-out).
        //
        // Pre-redesign both rendered as primary-coloured buttons in the
        // same row. That meant on a row where current=on, recommended=off,
        // the user saw an "Enable" (green) button next to a "Disable"
        // (red) button — the FIX (Disable) had the danger colour while
        // the BREAK (Enable) had the encouraging colour. Click-confusion
        // bait. Now: failing-row fix is amber-filled (matches the bucket),
        // opposite is low-emphasis grey-outline; passing rows hide the
        // fix (no-op) and keep only the opt-out as a quiet outline.
        const actions = check.actions || {};
        // ``check.fix_action`` is an explicit hint from the backend
        // (string: "enable" or "disable") that names which action key
        // is the recommended fix. Required for rows whose ``recommended``
        // is a descriptive string (e.g. the scheduled-trickplay row's
        // "disabled (Bridge plugin handles registration)") because the
        // boolean fallback below treats any truthy ``recommended`` as
        // "enable" and would pick the wrong action — silently doing the
        // OPPOSITE of what the row recommends.
        const fixDir = (typeof check.fix_action === 'string' && (check.fix_action === 'enable' || check.fix_action === 'disable'))
            ? check.fix_action
            : (check.recommended ? 'enable' : 'disable');
        const breakDir = fixDir === 'enable' ? 'disable' : 'enable';
        const fixAction = actions[fixDir];
        const breakAction = actions[breakDir];
        const btnWrap = document.createElement('div');
        btnWrap.className = 'd-flex gap-1 flex-wrap';

        // Button labels are decoupled from the on/off DIRECTION the
        // action runs in — pre-fix the same word "Enable" meant "apply
        // the recommendation" on one row (recommended=On) and "override
        // the recommendation" on another (recommended=Off). Users had
        // to figure out which button was the fix on each row before
        // clicking. Now:
        //   * Fix button   — always reads "Apply recommended" (intent-
        //                    labelled). The amber colour reinforces it
        //                    as the primary CTA.
        //   * Break button — reads "Enable (override)" or "Disable
        //                    (override)" — outcome verb + a "(override)"
        //                    tag so the user reads it as "do the
        //                    opposite of the recommendation, on
        //                    purpose". Tooltip restates the target
        //                    state in full.
        if (!ok && fixAction) {
            const targetOn = fixDir === 'enable';
            const icon = targetOn ? 'bi-toggle-on' : 'bi-toggle-off';
            const btn = _makeActionButton('btn-warning', icon, 'Apply recommended', check, fixDir);
            btn.title = `Apply the recommendation — set ${check.label || check.id || 'this'} to ${targetOn ? 'On' : 'Off'}`;
            btn.addEventListener('click', () => _runCheckAction(serverId, serverType, check, fixDir, btn));
            btnWrap.appendChild(btn);
        }
        if (breakAction) {
            const targetOn = breakDir === 'enable';
            const verb = targetOn ? 'Enable' : 'Disable';
            const icon = targetOn ? 'bi-toggle-on' : 'bi-toggle-off';
            const btn = _makeActionButton(
                'btn-outline-secondary',
                icon,
                `${verb} <span class="text-body-tertiary fw-normal">(override)</span>`,
                check,
                breakDir,
            );
            btn.title = `Override the recommendation — set ${check.label || check.id || 'this'} to ${targetOn ? 'On' : 'Off'}`;
            btn.addEventListener('click', () => _runCheckAction(serverId, serverType, check, breakDir, btn));
            btnWrap.appendChild(btn);
        }
        if (btnWrap.children.length > 0) {
            row.querySelector('.flex-grow-1').appendChild(btnWrap);
        }

        // Issue #237: per-check dismiss control. Only on recommended-
        // severity rows — critical rows always belong in mustFix,
        // forced by _partitionChecks regardless of dismissed state.
        // Dismissed rows render under "All good → Dismissed" and get
        // an Undismiss link instead.
        if (sev === 'recommended') {
            if (check.dismissed === true) {
                const undismissBtn = document.createElement('button');
                undismissBtn.type = 'button';
                undismissBtn.className = 'btn btn-sm btn-link p-0 text-decoration-none ms-1';
                undismissBtn.style.fontSize = '0.85em';
                undismissBtn.innerHTML = '<i class="bi bi-arrow-counterclockwise me-1"></i>Undismiss';
                undismissBtn.title = `Restore this check to the Recommended bucket — ${check.label || check.id || 'this row'}.`;
                undismissBtn.addEventListener('click', () => _toggleCheckDismissal(serverId, serverType, check, false, undismissBtn));
                row.querySelector('.flex-grow-1').appendChild(undismissBtn);
            } else {
                const dismissBtn = document.createElement('button');
                dismissBtn.type = 'button';
                dismissBtn.className = 'btn btn-sm btn-link p-0 text-decoration-none ms-2 text-muted';
                dismissBtn.style.fontSize = '0.85em';
                dismissBtn.innerHTML = '<i class="bi bi-x-circle me-1"></i>Dismiss';
                dismissBtn.title = `Hide this recommendation — ${check.label || check.id || 'this row'} — from the Recommended bucket. Reversible from the All good section.`;
                dismissBtn.addEventListener('click', () => _toggleCheckDismissal(serverId, serverType, check, true, dismissBtn));
                row.querySelector('.flex-grow-1').appendChild(dismissBtn);
            }
        }

        return row;
    }

    // Issue #237: POST to /previews-readiness/(dis|un)dismiss and
    // re-probe to redraw the card. The dismiss endpoint stores the
    // check_id on the per-server health_dismissals list; the GET
    // handler tags the check with ``dismissed: true`` on the next
    // probe, which _partitionChecks moves to the "Dismissed"
    // subsection.
    async function _toggleCheckDismissal(serverId, serverType, check, dismiss, btn) {
        const checkId = check && check.id;
        if (!checkId) return;
        const original = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Working…';
        try {
            const encoded = encodeURIComponent(serverId);
            const suffix = dismiss ? 'dismiss' : 'undismiss';
            const r = await api('POST', `/api/servers/${encoded}/previews-readiness/${suffix}`, { check_id: checkId });
            if (!r.ok) {
                showToast('Dismiss failed', (r.data && r.data.error) || `HTTP ${r.status}`, 'danger');
                btn.disabled = false;
                btn.innerHTML = original;
                return;
            }
            await runReadinessProbe(serverId, serverType);
        } catch (e) {
            showToast('Dismiss error', String(e), 'danger');
            btn.disabled = false;
            btn.innerHTML = original;
        }
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
                // Per-library actions carry an optional library_ids
                // array; legacy aggregate actions omit it and apply
                // server-wide. Forward both shapes faithfully so the
                // backend doesn't have to guess.
                const body = { scan_extraction: !!args.scan_extraction };
                if (Array.isArray(args.library_ids) && args.library_ids.length > 0) {
                    body.library_ids = args.library_ids;
                }
                const r = await api('POST', `/api/servers/${encoded}/vendor-extraction`, body);
                return { ok: !!(r.data && r.data.ok) && r.ok, error: r.data && r.data.error, status: r.status };
            }
            case 'set_scheduled_trickplay': {
                // Toggle the Emby/Jellyfin daily Generate-Trickplay-Images
                // scheduled task. Body shape mirrors the backend route:
                // ``{enabled: bool}``. The readiness row builds the
                // recommendation conditionally on plugin state — the
                // dispatcher here just forwards the click.
                const r = await api('POST', `/api/servers/${encoded}/scheduled-trickplay`, {
                    enabled: !!args.enabled,
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
        // ``confirm.body`` is server-emitted HTML (Python code in the
        // readiness probes). Render it as HTML so ``<code>``,
        // ``<strong>``, ``<br>`` etc. format correctly — pre-fix this
        // used ``textContent`` and users saw literal tag markup in
        // the confirmation modal.
        if (bodyEl) bodyEl.innerHTML = confirm.body || '';
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

    function _makeSection(title) {
        // No external docs link — see _renderSectionSubhead. Row-level
        // ⓘ icons open the inline explain modal which is the only
        // place rich help text should live.
        const sec = document.createElement('div');
        sec.className = 'mb-3';
        const heading = document.createElement('div');
        heading.className = 'text-muted small text-uppercase fw-bold mb-1 d-flex align-items-center gap-1';
        heading.style.letterSpacing = '0.5px';
        const label = document.createElement('span');
        label.textContent = title;
        heading.appendChild(label);
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
        if (v === true) return 'On';
        if (v === false) return 'Off';
        if (v === null || v === undefined) return '—';
        // Objects (e.g. Jellyfin's TrickplayOptions geometry dict) don't
        // fit a one-cell value; render a compact JSON-ish summary so the
        // diff still surfaces *something* informative without breaking
        // the layout. Falls through to escapeHtml(String(v)) for
        // strings/numbers.
        if (typeof v === 'object') {
            try {
                return escapeHtml(JSON.stringify(v));
            } catch (_e) {
                return escapeHtml(String(v));
            }
        }
        return escapeHtml(String(v));
    }

    // Render the side-by-side current/recommended diff for a check row.
    // Empty string when there's no value to show (info-only checks with
    // current=null and recommended=null).
    //
    // - When recommended is null/undefined → only render the current value.
    // - When current === recommended (passing) → single neutral pill.
    // - When current !== recommended (failing) → two-column grid with the
    //   current value tinted red and the recommended tinted green so the
    //   mismatch jumps out at a glance. Replaces the prior `Currently <X>`
    //   one-liner that left users guessing whether <X> was good or bad.
    function _renderValueDiff(current, recommended, ok, label) {
        const hasCurrent = !(current === null || current === undefined);
        const hasRecommended = !(recommended === null || recommended === undefined);
        if (!hasCurrent && !hasRecommended) return '';

        // Avoid the "Plex 1.43.2.10687 / Currently 1.43.2.10687" double-up.
        // For info-only rows where the label already names the current
        // value, skip the Currently line — the label is the current value.
        if (
            !hasRecommended
            && hasCurrent
            && typeof current === 'string'
            && typeof label === 'string'
            && label.includes(current)
        ) {
            return '';
        }

        // Object-typed values (Jellyfin's TrickplayOptions geometry dict
        // and similar) don't fit a one-line pill. Pre-redesign rendered
        // them as a giant single-line JSON code-block that overflowed
        // the row. Now: when passing, hide the dump entirely (the user
        // already sees ✓ + "matches recommended"); when failing, show
        // a compact "see details" hint pointing at the ⓘ explanation.
        const currentIsObject = hasCurrent && typeof current === 'object';
        const recommendedIsObject = hasRecommended && typeof recommended === 'object';
        const isObjectRow = currentIsObject || recommendedIsObject;

        if (!hasRecommended) {
            if (isObjectRow) {
                return `<div class="readiness-currently text-muted mt-1">`
                    + `<span class="text-body-tertiary">Current state — open ⓘ for details</span></div>`;
            }
            return `<div class="readiness-currently text-muted mt-1">`
                + `Currently <code>${formatHealthValue(current)}</code></div>`;
        }
        if (!hasCurrent) {
            if (isObjectRow) {
                return `<div class="readiness-currently text-muted mt-1">`
                    + `<span class="text-body-tertiary">Recommended state — open ⓘ for details</span></div>`;
            }
            return `<div class="readiness-currently text-muted mt-1">`
                + `Recommended <code>${formatHealthValue(recommended)}</code></div>`;
        }

        // Both present.
        const matched = ok || _valuesEqual(current, recommended);
        if (matched) {
            if (isObjectRow) {
                return `<div class="readiness-currently text-muted mt-1">`
                    + `<i class="bi bi-check2 me-1"></i>Matches recommended — open ⓘ for details</div>`;
            }
            // Tautology guard: on a passing row where current and
            // recommended display IDENTICALLY as non-toggle values
            // (e.g. "Currently reachable — recommended reachable",
            // "Currently installed — recommended installed"), drop
            // the redundant "— recommended X" tail. The recommended
            // value is information only when there's a meaningful
            // OTHER state — true for bool toggles ("we recommend On"
            // tells you the direction), useless for unary status
            // strings where the only valid value IS the recommended
            // one. Bools keep the tail so the user can still see
            // which way the recommendation points.
            const sameDisplay = formatHealthValue(current) === formatHealthValue(recommended);
            const isBoolToggle = typeof current === 'boolean' && typeof recommended === 'boolean';
            if (sameDisplay && !isBoolToggle) {
                return `<div class="readiness-currently text-muted mt-1">`
                    + `<i class="bi bi-check2 me-1"></i>Currently <code>${formatHealthValue(current)}</code></div>`;
            }
            return `<div class="readiness-currently text-muted mt-1">`
                + `<i class="bi bi-check2 me-1"></i>Currently <code>${formatHealthValue(current)}</code> `
                + `<span class="text-body-tertiary">— recommended <code>${formatHealthValue(recommended)}</code></span></div>`;
        }

        if (isObjectRow) {
            // Mismatch on a structured value — pointing at ⓘ keeps the row
            // scannable, and the rich explanation modal already carries
            // the field-by-field breakdown.
            return `<div class="readiness-currently text-muted mt-1">`
                + `<span class="text-danger-emphasis">Doesn't match recommended</span> `
                + `— open ⓘ for details</div>`;
        }

        return `<div class="readiness-values d-flex flex-wrap gap-2 mt-1">`
            + `<div class="readiness-current px-2 py-1 rounded">`
            +   `<span class="readiness-label">Currently</span> `
            +   `<code>${formatHealthValue(current)}</code></div>`
            + `<div class="readiness-arrow text-body-tertiary align-self-center">→</div>`
            + `<div class="readiness-recommended px-2 py-1 rounded">`
            +   `<span class="readiness-label">Recommended</span> `
            +   `<code>${formatHealthValue(recommended)}</code></div></div>`;
    }

    // Loose equality for the value-diff renderer — treats true/false
    // distinctly from "true"/"false" strings, but treats `1 == "1"` as
    // equal because the underlying probes sometimes return numeric
    // strings while the recommended value is a number.
    function _valuesEqual(a, b) {
        if (a === b) return true;
        if (a === null || b === null) return false;
        if (typeof a === 'object' || typeof b === 'object') {
            try { return JSON.stringify(a) === JSON.stringify(b); } catch (_e) { return false; }
        }
        // eslint-disable-next-line eqeqeq
        return a == b;
    }

    // Pick which of (enable | disable) is the "fix" for this check —
    // i.e. the action whose post-condition matches check.recommended.
    // Falls back to whichever single action exists when there's no
    // boolean target (install_plugin / sync_trickplay_options).
    //
    // The fix-direction matters: getting it backwards on Plex's BIF
    // generation rows would TURN ON Plex's own preview generation
    // (the opposite of what the user clicked "Fix" for) and silently
    // burn duplicate CPU on every library scan.
    function _pickFixAction(check) {
        const a = check.actions || {};
        // Explicit backend hint wins. Required for rows where
        // ``recommended`` is a descriptive string (e.g. the
        // scheduled-trickplay row) and the boolean fallback would
        // pick the wrong direction. Same hint that ``_renderCheckRow``
        // reads so the per-row "Apply recommended" button and the
        // bulk "Apply all" button always agree.
        if (typeof check.fix_action === 'string' && a[check.fix_action]) {
            return a[check.fix_action];
        }
        // Boolean-recommended fallback for legacy rows (apply_flag,
        // set_vendor_extraction). args.value covers apply_flag;
        // args.scan_extraction covers set_vendor_extraction.
        const direction = check.recommended ? 'enable' : 'disable';
        const matched = a[direction];
        if (matched) {
            const args = matched.args || {};
            if (args.value !== undefined && args.value === check.recommended) return matched;
            if (args.scan_extraction !== undefined && args.scan_extraction === check.recommended) return matched;
            // No discriminating arg — assume convention holds (enable
            // moves toward "more on / installed", disable the opposite).
            return matched;
        }
        // Single-direction actions (install_plugin etc.) have no opposite.
        return a.enable || a.disable || null;
    }

    // Walk every section's checks and assemble a fix plan ordered for
    // execution. Items are scoped to:
    //   'critical' — only severity=critical failing checks
    //   'all'      — every failing check that has an automatic fix
    //
    // Manual rows (no enable/disable action — version upgrades,
    // skipped custom-agent libraries) are excluded — they need user
    // intervention outside this app.
    function _buildFixPlan(sections, scope) {
        const plan = [];
        for (const section of sections || []) {
            for (const check of (section.checks || [])) {
                if (check.ok !== false) continue;
                if (scope === 'critical' && check.severity !== 'critical') continue;
                const fixAction = _pickFixAction(check);
                if (!fixAction) continue;
                plan.push({
                    check,
                    fixAction,
                    sectionTitle: section.title || section.id || '',
                });
            }
        }
        return plan;
    }

    // Open the bulk fix-plan modal. Preview lists every change about to
    // happen (label + current → recommended). On Apply: dispatches each
    // item through _dispatchCheckAction sequentially, then re-probes.
    //
    // Sequential not parallel — installing a Jellyfin plugin restarts
    // the server, so subsequent calls have to wait. Parallel would
    // race the restart and half the calls would 502.
    async function _runFixPlanWithPreview(serverId, serverType, plan, label) {
        if (!plan.length) return;
        const modalEl = document.getElementById('readinessFixPlanModal');
        const titleEl = document.getElementById('readinessFixPlanTitle');
        const introEl = document.getElementById('readinessFixPlanIntro');
        const listEl = document.getElementById('readinessFixPlanList');
        const applyBtn = document.getElementById('readinessFixPlanApplyBtn');
        const cancelBtn = document.getElementById('readinessFixPlanCancelBtn');
        const resultsEl = document.getElementById('readinessFixPlanResults');
        if (!modalEl || !titleEl || !listEl || !applyBtn) return;

        titleEl.textContent = label;
        introEl.textContent = `This will change ${plan.length} setting${plan.length === 1 ? '' : 's'} on the media server. Review the list and click Apply to proceed.`;
        listEl.innerHTML = '';
        for (const item of plan) {
            const li = document.createElement('li');
            li.className = 'list-group-item d-flex justify-content-between align-items-start gap-2';
            const cur = formatHealthValue(item.check.current);
            const rec = formatHealthValue(item.check.recommended);
            li.innerHTML = `<div class="flex-grow-1">`
                + `<div class="fw-semibold">${escapeHtml(item.check.label || item.check.id || '')}</div>`
                + `<div class="text-body-secondary small">${escapeHtml(item.sectionTitle)}</div>`
                + `</div>`
                + `<div class="text-end small">`
                +   `<code class="text-danger-emphasis">${cur}</code> `
                +   `<span class="text-body-tertiary">→</span> `
                +   `<code class="text-success-emphasis">${rec}</code>`
                + `</div>`;
            listEl.appendChild(li);
        }
        if (resultsEl) {
            resultsEl.classList.add('d-none');
            resultsEl.innerHTML = '';
        }
        applyBtn.disabled = false;
        applyBtn.innerHTML = `<i class="bi bi-magic me-1"></i>Apply ${plan.length}`;

        const modal = window.bootstrap && window.bootstrap.Modal
            ? window.bootstrap.Modal.getOrCreateInstance(modalEl)
            : null;
        if (!modal) return;

        // Wire one-shot Apply handler. Replace the button element to
        // detach any previous listeners — calling .show() with stale
        // listeners would trigger every prior click handler (the modal
        // is reused across Critical/All/Per-server invocations).
        const newApplyBtn = applyBtn.cloneNode(true);
        applyBtn.parentNode.replaceChild(newApplyBtn, applyBtn);
        newApplyBtn.addEventListener('click', async () => {
            newApplyBtn.disabled = true;
            if (cancelBtn) cancelBtn.disabled = true;
            newApplyBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Applying…';
            const outcomes = [];
            for (let i = 0; i < plan.length; i++) {
                const item = plan[i];
                try {
                    const res = await _dispatchCheckAction(serverId, item.fixAction);
                    outcomes.push({ item, ok: !!res.ok, error: res.error });
                } catch (exc) {
                    outcomes.push({ item, ok: false, error: String(exc) });
                }
            }
            const okCount = outcomes.filter((o) => o.ok).length;
            const failCount = outcomes.length - okCount;
            if (resultsEl) {
                resultsEl.classList.remove('d-none');
                const cls = failCount === 0 ? 'text-success' : (okCount === 0 ? 'text-danger' : 'text-warning');
                let html = `<div class="${cls} mb-2"><strong>${okCount}/${outcomes.length} applied</strong>`;
                if (failCount > 0) html += ` (${failCount} failed)`;
                html += `</div>`;
                if (failCount > 0) {
                    html += '<ul class="mb-0 ps-3">';
                    for (const o of outcomes) {
                        if (o.ok) continue;
                        html += `<li>${escapeHtml(o.item.check.label || o.item.check.id || '')}: ${escapeHtml(o.error || 'failed')}</li>`;
                    }
                    html += '</ul>';
                }
                resultsEl.innerHTML = html;
            }
            newApplyBtn.innerHTML = '<i class="bi bi-check2 me-1"></i>Done';
            // Re-probe so the readiness card reflects post-apply truth.
            try { await runReadinessProbe(serverId, serverType); } catch (_e) { /* swallow */ }
            // Auto-close after a short pause so the user sees the result
            // confirmation. Keep the modal open if there were failures so
            // the user can read the per-failure detail.
            if (failCount === 0) {
                setTimeout(() => modal.hide(), 1200);
            }
            if (cancelBtn) {
                cancelBtn.disabled = false;
                cancelBtn.textContent = 'Close';
            }
        });

        modal.show();
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
        // Unified "Previews readiness" card (v3). Both Fix buttons go
        // through the preview modal so the user sees exactly what's
        // about to change before committing — no more silent flips.
        const readinessFixBtn = document.getElementById('editReadinessFixAllBtn');
        if (readinessFixBtn) readinessFixBtn.addEventListener('click', async () => {
            const id = (_editState && _editState.server && _editState.server.id) || '';
            const type = (_editState && _editState.server && _editState.server.type) || '';
            if (!id) return;
            // Look up by the in-flight serverId so a stale probe from a
            // previously-open modal can't leak into this one.
            const data = _readinessDataByServer.get(id);
            if (!data) return;
            const plan = _buildFixPlan(data.sections || [], 'all');
            if (!plan.length) return;
            await _runFixPlanWithPreview(id, type, plan, 'Apply all recommended fixes');
        });
        const readinessFixCriticalBtn = document.getElementById('editReadinessFixCriticalBtn');
        if (readinessFixCriticalBtn) readinessFixCriticalBtn.addEventListener('click', async () => {
            const id = (_editState && _editState.server && _editState.server.id) || '';
            const type = (_editState && _editState.server && _editState.server.type) || '';
            if (!id) return;
            const data = _readinessDataByServer.get(id);
            if (!data) return;
            const plan = _buildFixPlan(data.sections || [], 'critical');
            if (!plan.length) return;
            await _runFixPlanWithPreview(id, type, plan, 'Fix critical issues only');
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
