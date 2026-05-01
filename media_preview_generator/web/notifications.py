"""Notification center source registry.

Assembles the list of active system notifications shown in the dashboard
bell-icon dropdown.  Each notification has a stable ``id`` so users can
dismiss it permanently (persisted in ``settings.json`` via
``settings_manager.dismissed_notifications``) without being unsuppressed
when the warning body evolves between releases.

Current sources:

- ``vulkan_software_fallback`` — Dolby Vision Profile 5 green-overlay
  warning, sourced from ``api_vulkan._get_vulkan_info``.
- ``timezone_misconfigured`` — container is running UTC without an
  explicit TZ env var, sourced from ``api_system._get_timezone_info``.

Session-only dismissals live in ``_SESSION_DISMISSED`` (process memory,
cleared on restart); permanent dismissals live in ``settings.json``.
"""

from __future__ import annotations

import threading
from typing import Any

from loguru import logger

VULKAN_SOFTWARE_FALLBACK_ID = "vulkan_software_fallback"
TIMEZONE_MISCONFIGURED_ID = "timezone_misconfigured"
SCHEMA_MIGRATION_ID = "schema_migration_completed"
DEPRECATED_IMAGE_ID = "deprecated_docker_image_name"

# Image names recognised by the deprecation banner. The deprecated image
# keeps publishing alongside the canonical name until 2026-10-29 (six months
# after the rename); after that, only the canonical name receives updates.
DEPRECATED_IMAGE_NAME = "stevezzau/plex_generate_vid_previews"
CANONICAL_IMAGE_NAME = "stevezzau/media_preview_generator"
DEPRECATED_IMAGE_SUNSET_DATE = "2026-10-29"


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
    logger.debug("notifications: session-dismissed {}", notification_id)


def undismiss_session(notification_id: str) -> None:
    """Clear a session-dismissed notification (used by reset button)."""
    with _SESSION_DISMISSED_LOCK:
        _SESSION_DISMISSED.discard(notification_id)


def reset_session() -> None:
    """Clear all session-only dismissals."""
    with _SESSION_DISMISSED_LOCK:
        _SESSION_DISMISSED.clear()


def _build_vulkan_software_fallback_notification() -> dict[str, Any] | None:
    """Return a notification dict when the Vulkan probe fell back to software.

    Returns ``None`` when Vulkan is healthy (no warning needed).  When
    the warning fires, reuses the existing GPU-aware HTML body from
    ``api_vulkan._get_vulkan_info`` so there is a single source of
    truth for the remediation text.
    """
    # Deferred import to avoid circular import (api_vulkan imports from
    # gpu_detection which imports loguru which... keep it lazy).
    from .routes.api_vulkan import _get_vulkan_info

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


def _build_timezone_misconfigured_notification() -> dict[str, Any] | None:
    """Return a notification dict when the container is running UTC without TZ.

    Reuses the warning HTML produced by ``api_system._get_timezone_info``
    so there's a single source of truth for the remediation text.
    """
    from .routes.api_system import _get_timezone_info

    info = _get_timezone_info()
    warning_html = info.get("warning")
    if not warning_html:
        return None

    return {
        "id": TIMEZONE_MISCONFIGURED_ID,
        "severity": "warning",
        "title": "Timezone not configured",
        "body_html": warning_html,
        "dismissable": True,
        "source": "timezone_probe",
    }


def _build_schema_migration_notification() -> dict[str, Any] | None:
    """One-shot card shown after run_migrations actually moved the schema.

    Reads ``_pending_migration_notice`` from settings.json — set by
    ``upgrade._migrate_schema`` — and renders a friendly "we migrated your
    config" card with a pointer at the .bak file. Dismissing the card
    clears the flag so it never reappears (see ``dismiss_schema_migration_notice``).
    """
    from .settings_manager import get_settings_manager

    notice = get_settings_manager().get("_pending_migration_notice")
    if not isinstance(notice, dict):
        return None
    from_v = notice.get("from", "?")
    to_v = notice.get("to", "?")
    backup = notice.get("backup") or ""
    notes = notice.get("notes") or []
    notes_html = ""
    if isinstance(notes, list) and notes:
        notes_html = (
            "<details class='mt-2'><summary class='small text-muted' style='cursor:pointer;'>"
            "What changed</summary><ul class='small mb-0'>"
            + "".join(f"<li>{n}</li>" for n in notes)
            + "</ul></details>"
        )
    backup_html = (
        f"<div class='small mt-2'>A backup of your previous settings is at "
        f"<code>{backup}</code> &mdash; safe to ignore unless you need to roll back.</div>"
        if backup
        else ""
    )
    body = (
        f"<p class='mb-0'>Your settings were migrated from schema "
        f"<strong>v{from_v}</strong> to <strong>v{to_v}</strong>.</p>{backup_html}{notes_html}"
    )
    return {
        "id": SCHEMA_MIGRATION_ID,
        "severity": "info",
        "title": "Settings migrated",
        "body_html": body,
        "dismissable": True,
        "source": "schema_migration",
    }


def dismiss_schema_migration_notice() -> None:
    """Clear the one-shot migration flag from settings.

    Called by the notification dismiss endpoint when the user closes the
    "Settings migrated" card. Survives a restart (the flag stays gone),
    unlike session-only dismissals.
    """
    from .settings_manager import get_settings_manager

    sm = get_settings_manager()
    if sm.get("_pending_migration_notice") is not None:
        sm.set("_pending_migration_notice", None)


def _build_deprecated_image_notification() -> dict[str, Any] | None:
    """Banner shown when the running image is the deprecated Docker name.

    The Dockerfile bakes ``DOCKER_IMAGE_NAME`` at build time via a build
    arg; CI sets it to ``stevezzau/plex_generate_vid_previews`` for the
    deprecated mirror image and to ``stevezzau/media_preview_generator``
    for the canonical image. Local dev builds default to ``"local"``.
    Only fires for the deprecated value so users on the canonical name
    (and dev builds) never see it.
    """
    import os

    image_name = (os.environ.get("DOCKER_IMAGE_NAME") or "").strip()
    if image_name != DEPRECATED_IMAGE_NAME:
        return None

    body = (
        f"<p class='mb-1'>You're running the Docker image "
        f"<code>{DEPRECATED_IMAGE_NAME}</code>, which has been renamed to "
        f"<code>{CANONICAL_IMAGE_NAME}</code> to reflect that this app now "
        f"supports Plex, Emby, and Jellyfin.</p>"
        f"<p class='mb-1'>Both image names mirror the same builds until "
        f"<strong>{DEPRECATED_IMAGE_SUNSET_DATE}</strong>; after that, only "
        f"<code>{CANONICAL_IMAGE_NAME}</code> receives updates. Update your "
        f"<code>compose</code> file&apos;s <code>image:</code> line and pull "
        f"the new image to keep getting updates.</p>"
        f"<p class='mb-0 small text-muted'>Existing volumes, settings, and "
        f"configuration are unchanged — only the image name moves.</p>"
    )
    return {
        "id": DEPRECATED_IMAGE_ID,
        "severity": "warning",
        "title": "Update your Docker image",
        "body_html": body,
        "dismissable": True,
        "source": "image_deprecation",
    }


def _notification_sources() -> list[dict[str, Any] | None]:
    """All notification builders.  Add new sources here as they arrive."""
    return [
        _build_vulkan_software_fallback_notification(),
        _build_timezone_misconfigured_notification(),
        _build_schema_migration_notification(),
        _build_deprecated_image_notification(),
    ]


def build_active_notifications(
    dismissed_permanent: list[str] | None = None,
) -> list[dict[str, Any]]:
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
    notifications: list[dict[str, Any]] = []
    for entry in _notification_sources():
        if entry is None:
            continue
        notif_id = entry.get("id")
        if not notif_id:
            continue
        if notif_id in persisted:
            logger.debug("notifications: {} suppressed (permanently dismissed)", notif_id)
            continue
        if _session_is_dismissed(notif_id):
            logger.debug("notifications: {} suppressed (session dismissed)", notif_id)
            continue
        notifications.append(entry)
    return notifications
