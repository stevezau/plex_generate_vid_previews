"""Schedule management API routes."""

import traceback

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
        return jsonify(
            {"error": "Either cron_expression or interval_minutes is required"}
        ), 400

    try:
        schedule_manager = get_schedule_manager()
        schedule = schedule_manager.create_schedule(
            name=data["name"],
            cron_expression=data.get("cron_expression"),
            interval_minutes=data.get("interval_minutes"),
            library_id=data.get("library_id"),
            library_name=data.get("library_name", ""),
            config=data.get("config", {}),
            enabled=data.get("enabled", True),
        )
        return jsonify(schedule), 201
    except ValueError as e:
        logger.warning(f"Schedule validation error: {e}")
        return jsonify({"error": "Invalid schedule parameters"}), 400
    except Exception as e:
        logger.error(f"Failed to create schedule: {e}\n{traceback.format_exc()}")
        return jsonify({"error": "Failed to create schedule"}), 500


@api.route("/schedules/<schedule_id>", methods=["PUT"])
@api_token_required
def update_schedule(schedule_id):
    """Update a schedule."""
    data = request.get_json() or {}

    schedule_manager = get_schedule_manager()
    schedule = schedule_manager.update_schedule(
        schedule_id=schedule_id,
        name=data.get("name"),
        cron_expression=data.get("cron_expression"),
        interval_minutes=data.get("interval_minutes"),
        library_id=data.get("library_id"),
        library_name=data.get("library_name"),
        config=data.get("config"),
        enabled=data.get("enabled"),
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
