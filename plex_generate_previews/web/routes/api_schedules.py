"""Schedule management API routes."""

from flask import jsonify, request
from loguru import logger

from ..auth import api_token_required
from ..scheduler import get_schedule_manager
from . import api


@api.route("/schedules")
@api_token_required
def get_schedules():
    """Get all schedules."""
    schedule_manager = get_schedule_manager()
    return jsonify({"schedules": schedule_manager.get_all_schedules()})


@api.route("/schedules/<schedule_id>")
@api_token_required
def get_schedule(schedule_id):
    """Get a specific schedule."""
    schedule_manager = get_schedule_manager()
    schedule = schedule_manager.get_schedule(schedule_id)
    if schedule:
        return jsonify(schedule)
    return jsonify({"error": "Schedule not found"}), 404


@api.route("/schedules", methods=["POST"])
@api_token_required
def create_schedule():
    """Create a new schedule."""
    data = request.get_json() or {}

    if not data.get("name"):
        return jsonify({"error": "Name is required"}), 400

    if not data.get("cron_expression") and not data.get("interval_minutes"):
        return jsonify({"error": "Either cron_expression or interval_minutes is required"}), 400

    # Both schedule types currently require Plex on the backend:
    #   * "recently_added"  — the scanner uses the Plex API
    #   * "full_library"    — the legacy run_processing walks Plex libraries
    # Reject non-Plex pins early so an Emby/Jellyfin user gets a clear error
    # at save time instead of a silent no-op every time the schedule fires.
    cfg = data.get("config") or {}
    job_type = str(cfg.get("job_type") or "full_library")
    target_server_id = data.get("server_id")
    if target_server_id:
        from ..settings_manager import get_settings_manager

        raw_servers = get_settings_manager().get("media_servers") or []
        target = next(
            (s for s in raw_servers if isinstance(s, dict) and s.get("id") == target_server_id),
            None,
        )
        if target and (target.get("type") or "").lower() != "plex":
            if job_type == "recently_added":
                return jsonify(
                    {
                        "error": (
                            "The Recently Added Scanner currently supports Plex only. "
                            "For Emby/Jellyfin, use the Sonarr/Radarr or Custom webhook on the "
                            "Triggers tab — those fire as soon as new media lands and work for any server."
                        )
                    }
                ), 400
            # Full-library scan path
            return jsonify(
                {
                    "error": (
                        "Full-library scan schedules currently support Plex only — the scan walks Plex's "
                        "library API. For Emby/Jellyfin, use the Sonarr/Radarr or Custom webhook on the "
                        "Triggers tab. Multi-server full-scan support is tracked as a follow-up feature."
                    )
                }
            ), 400

    try:
        schedule_manager = get_schedule_manager()
        schedule = schedule_manager.create_schedule(
            name=data["name"],
            cron_expression=data.get("cron_expression"),
            interval_minutes=data.get("interval_minutes"),
            library_id=data.get("library_id"),
            library_ids=data.get("library_ids"),
            library_name=data.get("library_name", ""),
            config=data.get("config", {}),
            enabled=data.get("enabled", True),
            priority=data.get("priority"),
            server_id=data.get("server_id") or None,
        )
        return jsonify(schedule), 201
    except ValueError as e:
        logger.warning(
            "New schedule rejected — invalid parameters ({}: {}). "
            "The Schedules page will show the validation error to you; "
            "common causes are an empty name, missing trigger (need either a cron expression "
            "or an interval in minutes), or a malformed cron syntax.",
            type(e).__name__,
            e,
        )
        return jsonify({"error": "Invalid schedule parameters"}), 400
    except Exception as e:
        logger.exception(
            "Could not save the new schedule {!r} ({}: {}). "
            "Most often this is a malformed cron expression or a clash with an existing schedule — "
            "check the cron syntax (e.g. '0 3 * * *' for 3am daily) and the schedules list for duplicates.",
            data.get("name", "<unnamed>"),
            type(e).__name__,
            e,
        )
        return jsonify({"error": "Failed to create schedule"}), 500


@api.route("/schedules/<schedule_id>", methods=["PUT"])
@api_token_required
def update_schedule(schedule_id):
    """Update a schedule."""
    data = request.get_json() or {}

    # Phase H7: same non-Plex pinned-server gate as POST. Recently Added
    # scanner + full-library scans currently only support Plex.
    cfg = data.get("config") or {}
    job_type = str(cfg.get("job_type") or "full_library")
    target_server_id = data.get("server_id")
    if target_server_id:
        from ..settings_manager import get_settings_manager

        raw_servers = get_settings_manager().get("media_servers") or []
        target = next(
            (s for s in raw_servers if isinstance(s, dict) and s.get("id") == target_server_id),
            None,
        )
        if target and (target.get("type") or "").lower() != "plex":
            if job_type == "recently_added":
                return jsonify(
                    {
                        "error": (
                            "The Recently Added Scanner currently supports Plex only. "
                            "For Emby/Jellyfin, use the Sonarr/Radarr or Custom webhook on the Triggers tab."
                        )
                    }
                ), 400
            return jsonify(
                {
                    "error": (
                        "Full-library scan schedules currently support Plex only. For Emby/Jellyfin, "
                        "use the Sonarr/Radarr or Custom webhook on the Triggers tab."
                    )
                }
            ), 400

    schedule_manager = get_schedule_manager()
    schedule = schedule_manager.update_schedule(
        schedule_id=schedule_id,
        name=data.get("name"),
        cron_expression=data.get("cron_expression"),
        interval_minutes=data.get("interval_minutes"),
        library_id=data.get("library_id"),
        library_ids=data.get("library_ids"),
        library_name=data.get("library_name"),
        config=data.get("config"),
        enabled=data.get("enabled"),
        priority=data.get("priority"),
        server_id=data.get("server_id"),
    )

    if schedule:
        return jsonify(schedule)
    return jsonify({"error": "Schedule not found"}), 404


@api.route("/schedules/<schedule_id>", methods=["DELETE"])
@api_token_required
def delete_schedule(schedule_id):
    """Delete a schedule."""
    schedule_manager = get_schedule_manager()
    if schedule_manager.delete_schedule(schedule_id):
        return jsonify({"success": True})
    return jsonify({"error": "Schedule not found"}), 404


@api.route("/schedules/<schedule_id>/enable", methods=["POST"])
@api_token_required
def enable_schedule(schedule_id):
    """Enable a schedule."""
    schedule_manager = get_schedule_manager()
    schedule = schedule_manager.enable_schedule(schedule_id)
    if schedule:
        return jsonify(schedule)
    return jsonify({"error": "Schedule not found"}), 404


@api.route("/schedules/<schedule_id>/disable", methods=["POST"])
@api_token_required
def disable_schedule(schedule_id):
    """Disable a schedule."""
    schedule_manager = get_schedule_manager()
    schedule = schedule_manager.disable_schedule(schedule_id)
    if schedule:
        return jsonify(schedule)
    return jsonify({"error": "Schedule not found"}), 404


@api.route("/schedules/<schedule_id>/run", methods=["POST"])
@api_token_required
def run_schedule_now(schedule_id):
    """Run a schedule immediately."""
    schedule_manager = get_schedule_manager()
    if schedule_manager.run_now(schedule_id):
        return jsonify({"success": True})
    return jsonify({"error": "Schedule not found"}), 404
