"""Schedule management API routes."""

from flask import jsonify, request
from loguru import logger

from ..auth import api_token_required
from ..scheduler import _parse_hhmm, get_schedule_manager, is_in_quiet_window
from ..settings_manager import get_settings_manager
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

    # Both schedule types work for every vendor: full-library scans go
    # through _run_full_scan_multi_server and recently-added through
    # _run_recently_added_multi_server, both of which dispatch via the
    # per-vendor VendorProcessor.
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
            stop_time=str(data.get("stop_time") or "").strip(),
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

    schedule_manager = get_schedule_manager()
    try:
        # stop_time: pass through only when the client included the
        # field (None = leave alone; "" = clear; "HH:MM" = set).
        update_kwargs = dict(
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
        if "stop_time" in data:
            update_kwargs["stop_time"] = str(data.get("stop_time") or "").strip()
        schedule = schedule_manager.update_schedule(**update_kwargs)
    except ValueError as e:
        # APScheduler's CronTrigger.from_crontab raises ValueError on malformed
        # input. Surface a friendly 400 instead of the generic 500 the
        # framework would otherwise emit, mirroring create_schedule's contract.
        logger.warning(
            "Schedule {} update rejected — invalid parameters ({}: {}). "
            "Common causes: malformed cron syntax, or both cron_expression and "
            "interval_minutes set to mutually-incompatible values.",
            schedule_id,
            type(e).__name__,
            e,
        )
        return jsonify({"error": "Invalid schedule parameters"}), 400

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


# ---------------------------------------------------------------------------
# Quiet Hours (D21) — global queue pause/resume schedule
# ---------------------------------------------------------------------------

_QUIET_HOURS_DEFAULT = {"enabled": False, "start": "08:00", "end": "01:00"}


def _quiet_hours_payload(qh: dict | None) -> dict:
    """Normalise the persisted shape to a stable JSON response."""
    qh = qh if isinstance(qh, dict) else {}
    enabled = bool(qh.get("enabled"))
    start = str(qh.get("start") or _QUIET_HOURS_DEFAULT["start"])
    end = str(qh.get("end") or _QUIET_HOURS_DEFAULT["end"])
    in_window = False
    if enabled:
        try:
            shm = _parse_hhmm(start)
            ehm = _parse_hhmm(end)
        except ValueError:
            shm = ehm = None
        if shm and ehm and shm != ehm:
            from datetime import datetime as _dt

            now = _dt.now()
            in_window = is_in_quiet_window((now.hour, now.minute), shm, ehm)
    return {
        "enabled": enabled,
        "start": start,
        "end": end,
        "currently_in_quiet_window": in_window,
    }


@api.route("/quiet-hours")
@api_token_required
def get_quiet_hours():
    """Return the current quiet-hours config (D21)."""
    sm = get_settings_manager()
    return jsonify(_quiet_hours_payload(sm.get("quiet_hours")))


@api.route("/quiet-hours", methods=["POST"])
@api_token_required
def update_quiet_hours():
    """Update the quiet-hours config and re-register the boundary crons (D21).

    Body: ``{"enabled": bool, "start": "HH:MM", "end": "HH:MM"}``.
    Validation rejects malformed times. Equal times disable the window
    even when ``enabled=True`` (we just don't register the crons).
    """
    data = request.get_json() or {}
    try:
        start = str(data.get("start") or "").strip() or _QUIET_HOURS_DEFAULT["start"]
        end = str(data.get("end") or "").strip() or _QUIET_HOURS_DEFAULT["end"]
        # Validate both up front; ValueError → 400.
        _parse_hhmm(start)
        _parse_hhmm(end)
    except ValueError as exc:
        return jsonify({"error": f"Invalid time: {exc}"}), 400

    qh = {"enabled": bool(data.get("enabled")), "start": start, "end": end}
    sm = get_settings_manager()
    sm.set("quiet_hours", qh)

    schedule_manager = get_schedule_manager()
    try:
        schedule_manager.apply_quiet_hours(qh)
    except Exception:
        logger.exception("Could not apply quiet-hours cron registration")
        return jsonify({"error": "Could not apply quiet-hours schedule"}), 500

    return jsonify(_quiet_hours_payload(qh))
