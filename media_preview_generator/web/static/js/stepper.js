// Stepper widget — wraps an <input type="number"> in an input-group with
// −/+ buttons, mirroring the dashboard's GPU/CPU worker controls. Used by
// the wizard, /settings, and the per-GPU panel so every numeric worker /
// thread / count input shares the same UX.
//
// Usage:
//   <input type="number" class="form-control has-stepper" min="0" max="32" value="1">
//   …then either: window.MPGShared.attachStepper(input)
//                   — wraps a single input
//        or:     window.MPGShared.attachSteppersTo(container)
//                   — finds every .has-stepper inside container and wraps it
//
// Persistence: clicking − or + sets input.value and dispatches both `input`
// and `change` events on the underlying element, so existing autosave,
// markDirty(), and form-collect-on-Save logic all keep working unchanged.

(function () {
    'use strict';

    function _intOrDefault(s, fallback) {
        const n = parseInt(s, 10);
        return Number.isFinite(n) ? n : fallback;
    }

    function attachStepper(input) {
        if (!input || input.dataset.stepperAttached === '1') return;
        input.dataset.stepperAttached = '1';

        const min = _intOrDefault(input.getAttribute('min'), -Infinity);
        const max = _intOrDefault(input.getAttribute('max'), Infinity);
        const step = _intOrDefault(input.getAttribute('step'), 1) || 1;

        // Wrap the input in an input-group. If it's already inside one
        // (e.g. labelled as "seconds"), wrap a level deeper so we don't
        // disturb the existing trailing addon.
        const parent = input.parentElement;
        const alreadyInGroup = parent && parent.classList.contains('input-group');
        const wrapper = document.createElement('div');
        wrapper.className = alreadyInGroup ? 'input-group input-group-sm flex-nowrap me-2' : 'input-group';
        if (alreadyInGroup) {
            // Insert wrapper before the input, then move just the input inside.
            parent.insertBefore(wrapper, input);
        } else {
            input.replaceWith(wrapper);
        }

        const minusBtn = document.createElement('button');
        minusBtn.type = 'button';
        minusBtn.className = 'btn btn-outline-secondary stepper-minus';
        minusBtn.title = 'Decrease';
        minusBtn.innerHTML = '<i class="bi bi-dash-lg"></i>';

        const plusBtn = document.createElement('button');
        plusBtn.type = 'button';
        plusBtn.className = 'btn btn-outline-secondary stepper-plus';
        plusBtn.title = 'Increase';
        plusBtn.innerHTML = '<i class="bi bi-plus-lg"></i>';

        wrapper.appendChild(minusBtn);
        wrapper.appendChild(input);
        wrapper.appendChild(plusBtn);

        function _refreshDisabled() {
            const v = _intOrDefault(input.value, min);
            minusBtn.disabled = input.disabled || v <= min;
            plusBtn.disabled = input.disabled || v >= max;
        }

        function _bump(direction) {
            if (input.disabled) return;
            const cur = _intOrDefault(input.value, 0);
            const next = Math.max(min, Math.min(max, cur + direction * step));
            if (next === cur) return;
            input.value = String(next);
            // Dispatch the same events a real user keystroke would, so
            // autosave / markDirty / Next-button collectors all fire.
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
            _refreshDisabled();
        }

        minusBtn.addEventListener('click', () => _bump(-1));
        plusBtn.addEventListener('click', () => _bump(1));
        input.addEventListener('input', _refreshDisabled);
        input.addEventListener('change', _refreshDisabled);
        _refreshDisabled();
    }

    function attachSteppersTo(container) {
        const root = container || document;
        root.querySelectorAll('input.has-stepper').forEach(attachStepper);
    }

    window.MPGShared = window.MPGShared || {};
    window.MPGShared.attachStepper = attachStepper;
    window.MPGShared.attachSteppersTo = attachSteppersTo;
})();
