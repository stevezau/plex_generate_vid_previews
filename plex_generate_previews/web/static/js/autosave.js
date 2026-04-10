/**
 * autosave.js — "save as you type" binding for settings pages.
 *
 * Replaces the explicit "Save Changes" button with per-control auto-save.
 * Each form control is bound to the appropriate event for its type
 * (toggles/selects/sliders fire on `change`, text inputs fire on blur
 * with a debounced `input` fallback, etc.), and all save calls for a
 * given container are serialised via a Promise chain so rapid edits
 * never race.
 *
 * Usage:
 *
 *   bindAutoSave({
 *       containerSelector: '#pageContent',
 *       saveFn: () => saveWebhookSettings(),
 *       indicatorId: 'saveStatusIndicator',
 *       skipSelectors: ['#plexWebhookPublicUrl', '.action-only'],
 *   });
 *
 * The `saveFn` should be an async function that POSTs whatever state
 * it wants to persist; it already exists on both of our settings
 * pages (saveAllSettings / saveWebhookSettings).  autosave.js just
 * calls it at the right times.
 */

(function () {
    'use strict';

    const TEXT_DEBOUNCE_MS = 1200;
    const SAVED_FADE_MS = 3000;

    // Per-container save state.  Keyed by container element.
    const _saveState = new WeakMap();

    function _getState(container) {
        let state = _saveState.get(container);
        if (!state) {
            state = {
                savePromise: Promise.resolve(),
                debounceTimers: new Map(),
                lastSavedAt: null,
                lastError: null,
                saveFn: null,
                indicator: null,
            };
            _saveState.set(container, state);
        }
        return state;
    }

    function _elementMatchesAny(el, selectors) {
        if (!selectors || selectors.length === 0) return false;
        for (const sel of selectors) {
            try {
                if (el.matches(sel)) return true;
            } catch (_) {
                // Invalid selector — ignore.
            }
        }
        return false;
    }

    function _classifyControl(el) {
        if (!el || !el.tagName) return 'ignore';
        const tag = el.tagName.toLowerCase();
        const type = (el.type || '').toLowerCase();

        if (tag === 'select') return 'instant';
        if (tag === 'textarea') return 'text';
        if (tag !== 'input') return 'ignore';

        switch (type) {
            case 'checkbox':
            case 'radio':
            case 'range':
            case 'color':
                return 'instant';
            case 'number':
                return 'number';
            case 'hidden':
            case 'button':
            case 'submit':
            case 'reset':
            case 'file':
            case 'image':
                return 'ignore';
            // text-ish
            case '':
            case 'text':
            case 'search':
            case 'url':
            case 'email':
            case 'tel':
            case 'password':
            case 'date':
            case 'datetime-local':
            case 'time':
            case 'month':
            case 'week':
                return 'text';
            default:
                return 'text';
        }
    }

    function _triggerSave(container) {
        const state = _getState(container);
        if (!state.saveFn) return;

        // Chain off the previous save (catch to break error propagation)
        // so we serialize per-container and never lose a later edit to an
        // earlier failed save.
        state.savePromise = state.savePromise.catch(() => {}).then(async () => {
            _render(state, 'saving');
            try {
                await state.saveFn();
                state.lastSavedAt = new Date();
                state.lastError = null;
                _render(state, 'saved');
                // Fade to idle after a moment so the indicator doesn't
                // stay "just saved" forever.
                setTimeout(() => {
                    // Only fade if still in the saved state (i.e. no
                    // newer save has kicked in since).
                    if (state.lastError === null) _render(state, 'idle');
                }, SAVED_FADE_MS);
            } catch (err) {
                console.error('Auto-save failed:', err);
                state.lastError = err;
                _render(state, 'error');
            }
        });
    }

    function _flushAndTriggerSave(container, el) {
        // Cancel any pending debounce for this element and save now.
        const state = _getState(container);
        const timer = state.debounceTimers.get(el);
        if (timer) {
            clearTimeout(timer);
            state.debounceTimers.delete(el);
        }
        _triggerSave(container);
    }

    function _debouncedTriggerSave(container, el) {
        const state = _getState(container);
        const existing = state.debounceTimers.get(el);
        if (existing) clearTimeout(existing);
        const timer = setTimeout(() => {
            state.debounceTimers.delete(el);
            _triggerSave(container);
        }, TEXT_DEBOUNCE_MS);
        state.debounceTimers.set(el, timer);
    }

    function _render(state, phase) {
        const ind = state.indicator;
        if (!ind) return;
        const timeStr = state.lastSavedAt
            ? state.lastSavedAt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
            : '';

        switch (phase) {
            case 'saving':
                ind.className = 'autosave-indicator saving text-muted small';
                ind.innerHTML =
                    '<i class="bi bi-arrow-clockwise me-1 autosave-spin"></i>Saving…';
                break;
            case 'saved':
                ind.className = 'autosave-indicator saved text-success small';
                ind.innerHTML =
                    '<i class="bi bi-check-circle me-1"></i>Saved' +
                    (timeStr ? ' at ' + timeStr : '');
                break;
            case 'idle':
                if (state.lastSavedAt) {
                    ind.className = 'autosave-indicator idle text-muted small';
                    ind.innerHTML =
                        '<i class="bi bi-check-circle me-1"></i>Saved at ' + timeStr;
                } else {
                    ind.className = 'autosave-indicator idle';
                    ind.innerHTML = '';
                }
                break;
            case 'error': {
                const msg = state.lastError
                    ? state.lastError.message || String(state.lastError)
                    : 'unknown error';
                ind.className = 'autosave-indicator error small';
                ind.innerHTML =
                    '<a href="#" class="text-danger text-decoration-none" data-autosave-retry="1">' +
                    '<i class="bi bi-exclamation-triangle me-1"></i>Save failed — click to retry' +
                    '</a>';
                // Attach retry handler
                const link = ind.querySelector('[data-autosave-retry]');
                if (link) {
                    link.addEventListener('click', function (e) {
                        e.preventDefault();
                        // Retry by triggering another save cycle.  We
                        // look up the container by walking from the
                        // indicator's stored container reference — but
                        // since WeakMap keys aren't iterable, the retry
                        // button is wired against the container captured
                        // in the closure below via _installRetryHook.
                        if (state._retryContainer) {
                            _triggerSave(state._retryContainer);
                        }
                    });
                }
                console.warn('Auto-save error state:', msg);
                break;
            }
        }
    }

    function _injectStyles() {
        if (document.getElementById('autosave-styles')) return;
        const style = document.createElement('style');
        style.id = 'autosave-styles';
        style.textContent =
            '.autosave-indicator { display: inline-flex; align-items: center; min-height: 1.75rem; padding: 0 0.25rem; }' +
            '.autosave-indicator.saving { opacity: 0.85; }' +
            '.autosave-spin { display: inline-block; animation: autosave-spin 1s linear infinite; }' +
            '@keyframes autosave-spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }';
        document.head.appendChild(style);
    }

    /**
     * Bind auto-save to all eligible form controls inside ``containerSelector``.
     *
     * @param {Object}   opts
     * @param {string}   opts.containerSelector - CSS selector for the root element.
     * @param {Function} opts.saveFn - Async function called on each save tick.
     * @param {string}   opts.indicatorId - id of the status indicator element.
     * @param {string[]} [opts.skipSelectors] - CSS selectors for controls to skip.
     */
    window.bindAutoSave = function bindAutoSave(opts) {
        _injectStyles();
        const container = document.querySelector(opts.containerSelector);
        if (!container) {
            console.warn('bindAutoSave: container not found:', opts.containerSelector);
            return;
        }
        const indicator = opts.indicatorId ? document.getElementById(opts.indicatorId) : null;
        const state = _getState(container);
        state.saveFn = opts.saveFn;
        state.indicator = indicator;
        state._retryContainer = container;

        const skip = opts.skipSelectors || [];

        function bindOne(el) {
            if (el.dataset.autosaveBound === '1') return;
            if (_elementMatchesAny(el, skip)) return;
            const kind = _classifyControl(el);
            if (kind === 'ignore') return;

            if (kind === 'instant') {
                el.addEventListener('change', () => _flushAndTriggerSave(container, el));
            } else if (kind === 'number') {
                el.addEventListener('change', () => _flushAndTriggerSave(container, el));
                el.addEventListener('blur', () => _flushAndTriggerSave(container, el));
            } else {
                // Text-like: save on blur (primary), with a debounced
                // input-event fallback so users who never blur still
                // get their changes persisted.
                el.addEventListener('blur', () => _flushAndTriggerSave(container, el));
                el.addEventListener('input', () => _debouncedTriggerSave(container, el));
            }
            el.dataset.autosaveBound = '1';
        }

        container.querySelectorAll('input, select, textarea').forEach(bindOne);

        // Watch for dynamically-added controls (e.g., library checkboxes
        // loaded from /api/libraries after page-ready).  A MutationObserver
        // catches children appended after bindAutoSave runs.
        const obs = new MutationObserver((mutations) => {
            for (const m of mutations) {
                for (const n of m.addedNodes) {
                    if (!(n instanceof Element)) continue;
                    if (n.matches && n.matches('input, select, textarea')) bindOne(n);
                    n.querySelectorAll && n.querySelectorAll('input, select, textarea').forEach(bindOne);
                }
            }
        });
        obs.observe(container, { childList: true, subtree: true });

        _render(state, 'idle');
    };

    /**
     * Manually trigger a save (e.g. from a button that wraps an async
     * action and wants to flush pending debounced changes).
     */
    window.autoSaveFlush = function autoSaveFlush(containerSelector) {
        const container = document.querySelector(containerSelector);
        if (!container) return;
        const state = _getState(container);
        // Flush any debounced timers
        for (const timer of state.debounceTimers.values()) clearTimeout(timer);
        state.debounceTimers.clear();
        _triggerSave(container);
    };
})();
