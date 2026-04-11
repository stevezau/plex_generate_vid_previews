"""Page routes for the web interface (main blueprint)."""

from flask import redirect, render_template, request, session, url_for
from loguru import logger

from ..auth import is_authenticated, login_required, validate_token
from . import main
from ._helpers import limiter


@main.route("/")
@login_required
def index():
    """Dashboard page. Redirects to setup wizard if setup is incomplete."""
    from ..settings_manager import get_settings_manager

    if not get_settings_manager().is_setup_complete():
        return redirect(url_for("main.setup_wizard"))
    return render_template("index.html")


@main.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def login():
    """Login page. Rate limited to 5 POST requests per minute."""
    if request.method == "POST":
        token = request.form.get("token", "")
        if validate_token(token):
            session["authenticated"] = True
            session.permanent = True
            logger.info("User logged in successfully")
            return redirect(url_for("main.index"))
        return render_template("login.html", error="Invalid token")

    if is_authenticated():
        return redirect(url_for("main.index"))
    return render_template("login.html")


@main.route("/logout")
def logout():
    """Logout and clear session."""
    session.clear()
    return redirect(url_for("main.login"))


@main.route("/settings")
@login_required
def settings():
    """Settings page."""
    return render_template("settings.html")


@main.route("/logs")
@login_required
def logs_page():
    """Live logs viewer page."""
    return render_template("logs.html")


@main.route("/webhooks")
@login_required
def webhooks_page():
    """Webhooks configuration page."""
    return render_template("webhooks.html")


@main.route("/schedules")
@login_required
def schedules_page():
    """Schedules management page."""
    return render_template("schedules.html")


@main.route("/bif-viewer")
@login_required
def bif_viewer():
    """BIF thumbnail viewer for troubleshooting preview quality."""
    return render_template("bif_viewer.html")


@main.route("/detection-debug")
@login_required
def detection_debug():
    """Detection debug viewer for troubleshooting credits/intro detection."""
    return render_template("detection_debug.html")


@main.route("/setup")
def setup_wizard():
    """Setup wizard page."""
    from ..settings_manager import get_settings_manager

    settings = get_settings_manager()

    if settings.is_setup_complete() and is_authenticated():
        return redirect(url_for("main.index"))

    return render_template("setup.html")
