"""Notification center source registry.

Assembles the list of active system notifications shown in the dashboard
bell-icon dropdown.  Each notification has a stable ``id`` so users can
dismiss it permanently (persisted in ``settings.json`` via
``settings_manager.dismissed_notifications``) without being unsuppressed
when the warning body evolves between releases.

Current sources:

- ``vulkan_software_fallback`` — the existing Dolby Vision Profile 5
  green-overlay warning previously rendered as a settings-page banner.
  The detailed HTML body is produced by ``api_system._get_vulkan_info``
  and relocated into a notification entry here.  No content changes —
  this module only wraps the existing warning.

Session-only dismissals live in ``_SESSION_DISMISSED`` (process memory,
cleared on restart); permanent dismissals live in ``settings.json``.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from loguru import logger


VULKAN_SOFTWARE_FALLBACK_ID = "vulkan_software_fallback"


_SESSION_DISMISSED: set[str] = set()
_SESSION_DISMISSED_LOCK = threading.Lock()


def _session_is_dismissed(notification_id: str) -> bool:
    with _SESSION_DISMISSED_LOCK:
        return notification_id in _SESSION_DISMISSED


def dismiss_session(notification_id: str) -> None:
    """Mark a notification as dismissed for the current process only.

    Cleared on container restart.  Used by the "Dismiss" button in the
    bell-icon dropdown when users want to hide a notification now but
    might want to see it again next time the app restarts.
    """
    with _SESSION_DISMISSED_LOCK:
        _SESSION_DISMISSED.add(notification_id)
    logger.debug(f"notifications: session-dismissed {notification_id}")


def undismiss_session(notification_id: str) -> None:
    """Clear a session-dismissed notification (used by reset button)."""
    with _SESSION_DISMISSED_LOCK:
        _SESSION_DISMISSED.discard(notification_id)


def reset_session() -> None:
    """Clear all session-only dismissals."""
    with _SESSION_DISMISSED_LOCK:
        _SESSION_DISMISSED.clear()


def _build_vulkan_software_fallback_notification() -> Optional[Dict[str, Any]]:
    """Return a notification dict when the Vulkan probe fell back to software.

    Returns ``None`` when Vulkan is healthy (no warning needed).  When
    the warning fires, reuses the existing GPU-aware HTML body from
    ``api_system._get_vulkan_info`` so there is a single source of
    truth for the remediation text.
    """
    # Deferred import to avoid circular import (api_system imports from
    # gpu_detection which imports loguru which... keep it lazy).
    from .routes.api_system import _get_vulkan_info

    info = _get_vulkan_info()
    warning_html = info.get("warning")
    if not warning_html:
        return None

    return {
        "id": VULKAN_SOFTWARE_FALLBACK_ID,
        "severity": "warning",
        "title": "Dolby Vision Profile 5 thumbnails may show a green overlay",
        "body_html": warning_html,
        "dismissable": True,
        "source": "vulkan_probe",
        "device": info.get("device"),
    }


def _notification_sources() -> List[Optional[Dict[str, Any]]]:
    """All notification builders.  Add new sources here as they arrive."""
    return [
        _build_vulkan_software_fallback_notification(),
    ]


def build_active_notifications(
    dismissed_permanent: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Return the list of notifications the UI should currently display.

    Filters out any notification whose ID is in ``dismissed_permanent``
    (from ``settings.json``) or in the in-process session dismissal set.
    Notifications that are not currently firing (builder returned
    ``None``) are simply not included.

    Args:
        dismissed_permanent: IDs the user has permanently dismissed.

    Returns:
        List of active notification dicts, each with at minimum
        ``id``, ``severity``, ``title``, ``body_html``, ``dismissable``.
    """
    persisted = set(dismissed_permanent or [])
    notifications: List[Dict[str, Any]] = []
    for entry in _notification_sources():
        if entry is None:
            continue
        notif_id = entry.get("id")
        if not notif_id:
            continue
        if notif_id in persisted:
            logger.debug(
                f"notifications: {notif_id} suppressed (permanently dismissed)"
            )
            continue
        if _session_is_dismissed(notif_id):
            logger.debug(f"notifications: {notif_id} suppressed (session dismissed)")
            continue
        notifications.append(entry)
    return notifications
