// Per-GPU configuration panel — shared between /settings and /setup wizard step 4.
//
// Each detected GPU renders as one card with an enable toggle, Workers count,
// and FFmpeg Threads count. Failed GPUs render disabled with the error inline.
//
// Callers must supply:
//   - <div id="gpuConfigList"></div>          (the panel mounts here)
//   - global function markDirty()              (called when fields change;
//                                               can be a no-op for non-autosave pages)
// Callers read per-GPU state back via collectGpuConfig() which returns the
// array shape expected by /api/settings (gpu_config[]).

function _gpuPanelEscapeHtml(str) {
    if (str == null) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function renderGpuConfigPanel(detectedGpus, savedConfig) {
    const configByDevice = {};
    (savedConfig || []).forEach(c => { if (c.device) configByDevice[c.device] = c; });

    const container = document.getElementById('gpuConfigList');
    container.innerHTML = '';

    detectedGpus.forEach((gpu, idx) => {
        const deviceId = (gpu.device || 'gpu' + idx).replace(/[^a-zA-Z0-9]/g, '_');
        const isFailed = gpu.status === 'failed';

        if (isFailed) {
            const card = document.createElement('div');
            card.className = 'card mb-2 border-danger';
            card.style.opacity = '0.85';
            const errorHtml = gpu.error_detail
                ? `<br><span class="text-muted mt-1 d-inline-block">${_gpuPanelEscapeHtml(gpu.error_detail)}</span>`
                : '';
            card.innerHTML = `
            <div class="card-body py-2 px-3">
                <div class="d-flex flex-column">
                    <div class="d-flex align-items-center mb-1">
                        <div class="form-check form-switch mb-0">
                            <input class="form-check-input" type="checkbox" disabled>
                            <label class="form-check-label fw-semibold text-muted">
                                ${_gpuPanelEscapeHtml(gpu.name || 'Unknown GPU')}
                            </label>
                        </div>
                        <span class="badge bg-danger ms-2">failed</span>
                    </div>
                    <small class="text-muted mb-2">${_gpuPanelEscapeHtml(gpu.type || 'UNKNOWN')} &mdash; ${_gpuPanelEscapeHtml(gpu.device || 'N/A')}</small>
                    <div class="alert alert-danger mb-0 py-2 px-3" style="font-size: 0.85em;">
                        <i class="bi bi-exclamation-triangle-fill me-1"></i>
                        <strong>${_gpuPanelEscapeHtml(gpu.error || 'Acceleration test failed')}</strong>
                        ${errorHtml}
                        <br><small class="text-muted">Fix the issue and click <strong>Re-scan GPUs</strong> below.</small>
                    </div>
                </div>
            </div>`;
            container.appendChild(card);
            return;
        }

        const saved = configByDevice[gpu.device] || {};
        const enabled = saved.enabled !== undefined ? saved.enabled : true;
        const workers = saved.workers !== undefined ? saved.workers : 1;
        const ffmpegThreads = saved.ffmpeg_threads !== undefined ? saved.ffmpeg_threads : 2;

        const card = document.createElement('div');
        card.className = 'card mb-2';
        card.innerHTML = `
            <div class="card-body py-2 px-3">
                <div class="row align-items-start">
                    <div class="col-md-4 d-flex flex-column justify-content-center" style="min-height: 3.5rem;">
                        <div class="form-check form-switch mb-0">
                            <input class="form-check-input gpu-enable-toggle" type="checkbox"
                                   id="gpuEnable_${deviceId}" data-device="${gpu.device}"
                                   data-gpu-name="${gpu.name}" data-gpu-type="${gpu.type}"
                                   ${enabled ? 'checked' : ''}
                                   onchange="markDirty(); toggleGpuRow('${deviceId}')">
                            <label class="form-check-label fw-semibold" for="gpuEnable_${deviceId}">
                                ${gpu.name}
                            </label>
                        </div>
                        <small class="text-muted">${gpu.type} &mdash; ${gpu.device || 'N/A'}</small>
                    </div>
                    <div class="col-md-4 gpu-settings-${deviceId}" ${enabled ? '' : 'style="opacity:0.5;pointer-events:none"'}>
                        <label class="form-label form-label-sm mb-1">Workers
                            <i class="bi bi-info-circle text-muted ms-1" style="cursor: help;"
                               data-bs-toggle="tooltip" data-bs-placement="top"
                               title="How many items this GPU processes at the same time. More workers = faster overall, but each worker uses GPU memory. Start with 1 and increase if your GPU can handle it."></i>
                        </label>
                        <input type="number" class="form-control form-control-sm gpu-workers has-stepper"
                               data-device="${gpu.device}" min="1" max="16"
                               value="${workers}" onchange="onGpuWorkersChange(this, '${deviceId}')">
                    </div>
                    <div class="col-md-4 gpu-settings-${deviceId}" ${enabled ? '' : 'style="opacity:0.5;pointer-events:none"'}>
                        <label class="form-label form-label-sm mb-1">FFmpeg Threads
                            <i class="bi bi-info-circle text-muted ms-1" style="cursor: help;"
                               data-bs-toggle="tooltip" data-bs-placement="top"
                               title="Limits how many CPU cores FFmpeg uses per worker for tasks like decoding and filtering. Lower values free up CPU for other workers. Set to 0 to let FFmpeg decide automatically."></i>
                        </label>
                        <input type="number" class="form-control form-control-sm gpu-ffmpeg-threads has-stepper"
                               data-device="${gpu.device}" min="0" max="32"
                               value="${ffmpegThreads}" onchange="markDirty()">
                        <small class="text-muted">0 = use all CPU cores &middot; recommended: 2</small>
                    </div>
                </div>
            </div>
        `;
        container.appendChild(card);
    });

    container.querySelectorAll('[data-bs-toggle="tooltip"]').forEach(el => {
        new bootstrap.Tooltip(el);
    });
    // Apply −/+ stepper buttons to the per-GPU Workers + FFmpeg Threads
    // inputs. Safe no-op if the helper isn't loaded (older pages).
    if (window.MPGShared && window.MPGShared.attachSteppersTo) {
        window.MPGShared.attachSteppersTo(container);
    }
}

function toggleGpuRow(deviceId) {
    const toggle = document.getElementById('gpuEnable_' + deviceId);
    const enabled = toggle.checked;
    const els = document.querySelectorAll('.gpu-settings-' + deviceId);
    els.forEach(el => {
        el.style.opacity = enabled ? '1' : '0.5';
        el.style.pointerEvents = enabled ? '' : 'none';
    });
    if (enabled) {
        const row = toggle.closest('.card');
        const workersInput = row.querySelector('.gpu-workers');
        if (workersInput && (parseInt(workersInput.value) || 0) < 1) {
            workersInput.value = 1;
        }
    }
}

function onGpuWorkersChange(input, deviceId) {
    markDirty();
    const val = parseInt(input.value) || 0;
    if (val <= 0) {
        input.value = 0;
        const toggle = document.getElementById('gpuEnable_' + deviceId);
        if (toggle && toggle.checked) {
            toggle.checked = false;
            toggleGpuRow(deviceId);
        }
    }
}

function collectGpuConfig() {
    const config = [];
    document.querySelectorAll('.gpu-enable-toggle').forEach(toggle => {
        const device = toggle.dataset.device;
        const row = toggle.closest('.card');
        const workersInput = row.querySelector('.gpu-workers');
        const ffmpegInput = row.querySelector('.gpu-ffmpeg-threads');
        let enabled = toggle.checked;
        let workers = parseInt(workersInput.value) || 0;
        if (enabled && workers <= 0) {
            enabled = false;
            workers = 0;
        }
        config.push({
            device: device,
            name: toggle.dataset.gpuName || 'GPU',
            type: toggle.dataset.gpuType || '',
            enabled: enabled,
            workers: workers,
            ffmpeg_threads: parseInt(ffmpegInput.value) || 0,
        });
    });
    return config;
}
