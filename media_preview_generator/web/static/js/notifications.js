// =========================================================================
// Notification center (bell-icon dropdown)
//
// Fetches active system notifications from /api/system/notifications,
// renders them into the bell dropdown, and wires dismiss / dismiss-permanent
// buttons.  Notifications a user dismissed permanently are stored in
// settings.json and stay dismissed across container restarts.
//
// Split out of app.js (which previously held this section at lines 904-1117)
// so the notification flow can be edited without scrolling past 4K LOC of
// unrelated dashboard glue. Loaded AFTER app.js in base.html so this file
// can call escapeHtml / sanitizeNotificationHtml / copyVulkanDiagnosticBundle
// / showToast as window-level globals defined there.
// =========================================================================

function loadNotifications() {
    var list = document.getElementById('notificationList');
    var empty = document.getElementById('notificationEmpty');
    var badge = document.getElementById('notificationBellBadge');
    if (!list || !badge) return;

    fetch('/api/system/notifications')
        .then(function (r) {
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.json();
        })
        .then(function (data) {
            var notifications = (data && data.notifications) || [];
            renderNotifications(notifications);
        })
        .catch(function (err) {
            console.error('loadNotifications failed:', err);
            // Leave the list in its previous state; failure should not
            // clobber any currently-visible notifications.
        });
}

function renderNotifications(notifications) {
    var list = document.getElementById('notificationList');
    var empty = document.getElementById('notificationEmpty');
    var badge = document.getElementById('notificationBellBadge');
    if (!list) return;

    // Wipe previous entries but keep the 'empty' placeholder at the top.
    list.querySelectorAll('.notification-entry').forEach(function (el) { el.remove(); });

    if (!notifications || notifications.length === 0) {
        if (empty) empty.classList.remove('d-none');
        if (badge) {
            badge.textContent = '0';
            badge.classList.add('d-none');
        }
        return;
    }

    if (empty) empty.classList.add('d-none');
    if (badge) {
        badge.textContent = notifications.length.toString();
        badge.classList.remove('d-none');
    }

    notifications.forEach(function (notif) {
        var entry = document.createElement('div');
        entry.className = 'notification-entry border-bottom p-3';
        entry.dataset.notificationId = notif.id;

        var severity = notif.severity || 'info';
        var iconClass = severity === 'warning'
            ? 'bi-exclamation-triangle-fill text-warning'
            : severity === 'error'
                ? 'bi-x-octagon-fill text-danger'
                : 'bi-info-circle-fill text-info';

        var header = document.createElement('div');
        header.className = 'd-flex align-items-start';
        header.innerHTML =
            '<i class="bi ' + iconClass + ' me-2 mt-1 flex-shrink-0"></i>' +
            '<div class="flex-grow-1">' +
            '<strong class="d-block notification-title">' + escapeHtml(notif.title || 'Notification') + '</strong>' +
            '<a href="#" class="small notification-toggle-details">Show details</a>' +
            '</div>';

        var details = document.createElement('div');
        details.className = 'notification-details small mt-2 d-none';
        details.innerHTML = sanitizeNotificationHtml(notif.body_html);

        var copyRow = document.createElement('div');
        copyRow.className = 'mt-2 d-none notification-copy-row';
        if (notif.source === 'vulkan_probe') {
            var copyBtn = document.createElement('button');
            copyBtn.type = 'button';
            copyBtn.className = 'btn btn-sm btn-outline-secondary';
            copyBtn.innerHTML = '<i class="bi bi-clipboard me-1"></i>Copy diagnostic bundle';
            copyBtn.onclick = function () { copyVulkanDiagnosticBundle(copyBtn); };
            copyRow.appendChild(copyBtn);
            copyRow.classList.remove('d-none');
        }

        var actions = document.createElement('div');
        actions.className = 'd-flex gap-2 mt-2';
        if (notif.dismissable !== false) {
            var dismiss = document.createElement('button');
            dismiss.type = 'button';
            dismiss.className = 'btn btn-sm btn-outline-secondary';
            dismiss.textContent = 'Dismiss';
            dismiss.title = 'Hide until next restart';
            dismiss.onclick = function () { dismissNotificationSession(notif.id); };
            actions.appendChild(dismiss);

            var dismissPerm = document.createElement('button');
            dismissPerm.type = 'button';
            dismissPerm.className = 'btn btn-sm btn-outline-danger';
            dismissPerm.textContent = 'Dismiss permanently';
            dismissPerm.title = 'Never show this notification again';
            dismissPerm.onclick = function () { dismissNotificationPermanent(notif.id); };
            actions.appendChild(dismissPerm);
        }

        entry.appendChild(header);
        entry.appendChild(details);
        entry.appendChild(copyRow);
        entry.appendChild(actions);
        list.appendChild(entry);

        // Wire "Show details" toggle.
        var toggle = entry.querySelector('.notification-toggle-details');
        if (toggle) {
            toggle.addEventListener('click', function (ev) {
                ev.preventDefault();
                var hidden = details.classList.toggle('d-none');
                toggle.textContent = hidden ? 'Show details' : 'Hide details';
            });
        }
    });
}

function dismissNotificationSession(notificationId) {
    fetch('/api/system/notifications/' + encodeURIComponent(notificationId) + '/dismiss', {
        method: 'POST',
    })
        .then(function (r) { return r.json(); })
        .then(function () {
            if (typeof showToast === 'function') {
                showToast('Dismissed', 'Notification hidden for this session', 'info');
            }
            loadNotifications();
        })
        .catch(function (err) {
            console.error('dismissNotificationSession failed:', err);
            if (typeof showToast === 'function') {
                showToast('Error', 'Could not dismiss notification', 'danger');
            }
        });
}

function dismissNotificationPermanent(notificationId) {
    fetch('/api/system/notifications/' + encodeURIComponent(notificationId) + '/dismiss-permanent', {
        method: 'POST',
    })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data && data.ok) {
                if (typeof showToast === 'function') {
                    showToast('Dismissed permanently', 'This notification will not appear again', 'info');
                }
                loadNotifications();
            } else {
                throw new Error((data && data.error) || 'Unknown error');
            }
        })
        .catch(function (err) {
            console.error('dismissNotificationPermanent failed:', err);
            if (typeof showToast === 'function') {
                showToast('Error', 'Could not permanently dismiss notification', 'danger');
            }
        });
}

function resetDismissedNotifications() {
    fetch('/api/system/notifications/reset-dismissed', { method: 'POST' })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data && data.ok) {
                if (typeof showToast === 'function') {
                    showToast('Reset', 'Dismissed notifications cleared', 'info');
                }
                loadNotifications();
            }
        })
        .catch(function (err) {
            console.error('resetDismissedNotifications failed:', err);
        });
}

// Bind reset button and initial load once DOM is ready.  loadNotifications()
// is safe to call on any page because the bell dropdown lives in the shared
// navbar (base.html).
document.addEventListener('DOMContentLoaded', function () {
    var resetBtn = document.getElementById('notificationResetBtn');
    if (resetBtn) {
        resetBtn.addEventListener('click', function (ev) {
            ev.preventDefault();
            resetDismissedNotifications();
        });
    }
    loadNotifications();
    // Refresh every 60s so a Vulkan state change (e.g. after a container
    // restart with fixed env) shows up without a manual page reload.
    setInterval(loadNotifications, 60000);
});
