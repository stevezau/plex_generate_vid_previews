// Folder picker — Bootstrap modal for browsing the running container's
// filesystem so users don't have to type local paths into path-mapping inputs
// or the Plex config folder field. Calls /api/system/browse, server-side
// guarded against /proc, /sys, /dev etc.
//
// Usage:
//   openFolderPicker('/data', (pickedPath) => { input.value = pickedPath; });

(function () {
    'use strict';

    const MODAL_ID = 'folderPickerModal';
    let _onPickCallback = null;
    let _currentPath = '/';
    let _showHidden = false;

    function _ensureModalMarkup() {
        if (document.getElementById(MODAL_ID)) return;
        const html = `
            <div class="modal fade" id="${MODAL_ID}" tabindex="-1" aria-hidden="true">
                <div class="modal-dialog modal-dialog-centered modal-lg">
                    <div class="modal-content">
                        <div class="modal-header">
                            <h5 class="modal-title"><i class="bi bi-folder2-open me-2"></i>Pick a folder</h5>
                            <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
                        </div>
                        <div class="modal-body">
                            <div class="input-group input-group-sm mb-2">
                                <button type="button" class="btn btn-outline-secondary" id="folderPickerUpBtn" title="Go to parent folder">
                                    <i class="bi bi-arrow-up"></i>
                                </button>
                                <input type="text" class="form-control" id="folderPickerPathInput"
                                       placeholder="/path/to/folder" aria-label="Folder path"
                                       autocomplete="off" spellcheck="false">
                                <button type="button" class="btn btn-outline-secondary" id="folderPickerGoBtn" title="Go to typed path (Enter)">
                                    <i class="bi bi-arrow-right-circle"></i>
                                </button>
                            </div>
                            <nav id="folderPickerBreadcrumb" aria-label="folder breadcrumb" class="mb-2"></nav>
                            <div class="form-check form-check-inline mb-2 small">
                                <input class="form-check-input" type="checkbox" id="folderPickerShowHidden">
                                <label class="form-check-label" for="folderPickerShowHidden">Show hidden directories</label>
                            </div>
                            <div id="folderPickerList" class="list-group small" style="max-height: 360px; overflow-y: auto;"></div>
                            <div id="folderPickerError" class="alert alert-warning small mt-2 d-none"></div>
                        </div>
                        <div class="modal-footer">
                            <div class="me-auto small text-muted">
                                Selected: <code id="folderPickerSelectedPath">/</code>
                            </div>
                            <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
                            <button type="button" class="btn btn-primary" id="folderPickerConfirmBtn">
                                <i class="bi bi-check2 me-1"></i>Pick this folder
                            </button>
                        </div>
                    </div>
                </div>
            </div>
        `;
        const wrapper = document.createElement('div');
        wrapper.innerHTML = html;
        document.body.appendChild(wrapper.firstElementChild);

        document.getElementById('folderPickerShowHidden').addEventListener('change', (ev) => {
            _showHidden = ev.target.checked;
            _loadPath(_currentPath);
        });
        document.getElementById('folderPickerConfirmBtn').addEventListener('click', () => {
            if (_onPickCallback) {
                try { _onPickCallback(_currentPath); } catch (e) { console.error(e); }
            }
            const modal = bootstrap.Modal.getInstance(document.getElementById(MODAL_ID));
            if (modal) modal.hide();
        });

        // Path input — Enter key navigates to the typed path, mirroring the
        // Go button. Typing alone doesn't navigate (intrusive); the user has
        // to commit with Enter or the Go button.
        const pathInput = document.getElementById('folderPickerPathInput');
        pathInput.addEventListener('keydown', (ev) => {
            if (ev.key === 'Enter') {
                ev.preventDefault();
                const target = (pathInput.value || '').trim() || '/';
                _loadPath(target);
            }
        });
        document.getElementById('folderPickerGoBtn').addEventListener('click', () => {
            const target = (pathInput.value || '').trim() || '/';
            _loadPath(target);
        });

        // Up button — go to parent of the current path. _loadPath updates
        // _currentPath after a successful API response, so reading it here
        // always reflects what the user is actually browsing.
        document.getElementById('folderPickerUpBtn').addEventListener('click', () => {
            if (_currentPath === '/' || !_currentPath) return;
            const parent = _currentPath.replace(/\/+[^/]+\/?$/, '') || '/';
            _loadPath(parent);
        });
    }

    function _renderBreadcrumb(path) {
        const el = document.getElementById('folderPickerBreadcrumb');
        const segs = path === '/' ? [''] : path.split('/');
        let cum = '';
        const items = segs.map((seg, i) => {
            if (i === 0) {
                return `<li class="breadcrumb-item"><a href="#" data-fp-path="/">/</a></li>`;
            }
            cum += '/' + seg;
            const isLast = i === segs.length - 1;
            return isLast
                ? `<li class="breadcrumb-item active" aria-current="page">${escapeHtmlText(seg)}</li>`
                : `<li class="breadcrumb-item"><a href="#" data-fp-path="${escapeHtmlAttr(cum)}">${escapeHtmlText(seg)}</a></li>`;
        });
        el.innerHTML = `<ol class="breadcrumb mb-0">${items.join('')}</ol>`;
        el.querySelectorAll('a[data-fp-path]').forEach((a) => {
            a.addEventListener('click', (ev) => {
                ev.preventDefault();
                _loadPath(ev.currentTarget.dataset.fpPath);
            });
        });
    }

    function _renderEntries(entries) {
        const list = document.getElementById('folderPickerList');
        if (!entries.length) {
            list.innerHTML = '<div class="list-group-item text-muted">No subfolders.</div>';
            return;
        }
        list.innerHTML = entries.map((e) =>
            `<button type="button" class="list-group-item list-group-item-action d-flex justify-content-between align-items-center" data-fp-path="${escapeHtmlAttr(e.path)}">
                <span><i class="bi bi-folder2 me-2"></i>${escapeHtmlText(e.name)}</span>
                <i class="bi bi-chevron-right text-muted"></i>
            </button>`
        ).join('');
        list.querySelectorAll('button[data-fp-path]').forEach((btn) => {
            btn.addEventListener('click', () => _loadPath(btn.dataset.fpPath));
        });
    }

    function _syncChrome() {
        const pathInput = document.getElementById('folderPickerPathInput');
        if (pathInput) pathInput.value = _currentPath;
        const upBtn = document.getElementById('folderPickerUpBtn');
        if (upBtn) upBtn.disabled = _currentPath === '/' || !_currentPath;
        const sel = document.getElementById('folderPickerSelectedPath');
        if (sel) sel.textContent = _currentPath;
    }

    async function _loadPath(path) {
        const errEl = document.getElementById('folderPickerError');
        const list = document.getElementById('folderPickerList');
        errEl.classList.add('d-none');
        errEl.textContent = '';
        list.innerHTML = '<div class="list-group-item text-muted"><span class="spinner-border spinner-border-sm me-1"></span>Loading…</div>';
        try {
            const qs = new URLSearchParams({ path });
            if (_showHidden) qs.set('show_hidden', '1');
            const data = await apiGet('/api/system/browse?' + qs.toString());
            _currentPath = data.path || path || '/';
            _renderBreadcrumb(_currentPath);
            _renderEntries(data.entries || []);
            _syncChrome();
            if (data.error) {
                errEl.textContent = data.error;
                errEl.classList.remove('d-none');
            }
        } catch (e) {
            // API errored (404 not-found / 403 denied / etc). Show the
            // message but keep the typed path in the input so the user
            // can edit it instead of losing their work.
            list.innerHTML = '';
            const pathInput = document.getElementById('folderPickerPathInput');
            if (pathInput) pathInput.value = path;
            errEl.textContent = (e && e.message) || 'Failed to list folder';
            errEl.classList.remove('d-none');
        }
    }

    window.openFolderPicker = function (initialPath, onPick) {
        _ensureModalMarkup();
        _onPickCallback = typeof onPick === 'function' ? onPick : null;
        _currentPath = initialPath || '/';
        _showHidden = false;
        document.getElementById('folderPickerShowHidden').checked = false;
        _loadPath(_currentPath);
        const modal = new bootstrap.Modal(document.getElementById(MODAL_ID));
        modal.show();
    };
})();
