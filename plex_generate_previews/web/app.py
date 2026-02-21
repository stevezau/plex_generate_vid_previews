"""
Flask application factory for the web interface.

Creates and configures the Flask application with SocketIO support.
"""

import atexit
import hashlib
import hmac
import os
from datetime import timedelta
from pathlib import Path

from flask import Flask
from flask_socketio import SocketIO
from flask_wtf.csrf import CSRFProtect
from loguru import logger
from werkzeug.middleware.proxy_fix import ProxyFix

from .auth import log_token_on_startup
from .jobs import get_job_manager
from .scheduler import get_schedule_manager

# Global SocketIO instance
socketio = SocketIO()

# Global CSRF instance
csrf = CSRFProtect()


def run_scheduled_job(library_id=None, library_name="", config=None):
    """
    Callback for running scheduled jobs.

    Must be at module level (not inside create_app) so APScheduler
    can pickle it for the SQLAlchemy jobstore.

    Args:
        library_id: Plex library section ID
        library_name: Human-readable library name
        config: Job configuration dict
    """
    # Get job manager - uses singleton pattern
    job_manager = get_job_manager()

    job = job_manager.create_job(
        library_id=library_id, library_name=library_name, config=config or {}
    )

    # Build config overrides - include library_id if specified
    job_config = dict(config) if config else {}
    if library_id:
        # Specific library selected - run only that library
        job_config["selected_libraries"] = [library_id]
    else:
        # All libraries - empty list means process all
        job_config["selected_libraries"] = []

    # Import here to avoid circular imports
    from .routes import _start_job_async

    _start_job_async(job.id, job_config)


def get_cors_origins() -> str:
    """
    Get CORS allowed origins from environment.

    Defaults to ``"*"`` (allow all) because this tool is typically accessed
    over a LAN via IP address, Docker bridge, or hostname — not just
    ``localhost``.  Restricting to ``localhost`` breaks WebSocket upgrades
    from any other origin and causes SocketIO 400 errors.

    Set ``CORS_ORIGINS`` to a comma-separated list to lock it down, e.g.
    ``CORS_ORIGINS=http://192.168.1.10:8080,http://mynas:8080``.

    Returns:
        CORS origins string or ``"*"``
    """
    explicit = os.environ.get("CORS_ORIGINS")
    if explicit:
        return explicit
    return "*"


def _derive_secret(seed: bytes, config_dir: str) -> str:
    """Derive a Flask secret key from a stored seed and deployment-specific salt.

    Uses HMAC-SHA256 so the actual secret is never stored on disk.

    Args:
        seed: Random bytes read from (or generated for) the seed file.
        config_dir: Configuration directory used as an additional salt.

    Returns:
        Hex-encoded derived secret key.
    """
    return hmac.new(seed, config_dir.encode("utf-8"), hashlib.sha256).hexdigest()


def get_or_create_flask_secret(config_dir: str) -> str:
    """
    Get Flask secret key from environment or persistent seed file.

    The seed file stores random bytes — *not* the secret itself.
    The actual secret is derived at runtime via HMAC-SHA256(seed, config_dir)
    so that sensitive key material is never written to disk in clear text.

    Priority:
    1. FLASK_SECRET_KEY environment variable
    2. Derived from /config/flask_secret.key seed file
    3. Generate new seed, save it, and derive secret

    Args:
        config_dir: Configuration directory path

    Returns:
        Flask secret key string
    """
    # Check environment variable first
    env_secret = os.environ.get("FLASK_SECRET_KEY")
    if env_secret:
        logger.debug("Using Flask secret from FLASK_SECRET_KEY environment variable")
        return env_secret

    # Check for persistent seed file
    seed_file = Path(config_dir) / "flask_secret.key"

    if seed_file.exists():
        try:
            seed = seed_file.read_bytes()
            if seed:
                logger.debug(f"Using Flask secret derived from seed in {seed_file}")
                return _derive_secret(seed, config_dir)
        except IOError as e:
            logger.warning(f"Failed to read Flask secret seed file: {e}")

    # Generate new random seed and persist it with restrictive permissions.
    # os.open with 0o600 creates the file atomically with the correct mode
    # to avoid a TOCTOU race between creation and chmod.
    random_seed = os.urandom(32)
    try:
        seed_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(seed_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, random_seed)
        finally:
            os.close(fd)
        logger.info(f"Generated new Flask secret seed and saved to {seed_file}")
    except IOError as e:
        logger.warning(f"Failed to save Flask secret seed to file: {e}")

    return _derive_secret(random_seed, config_dir)


def create_app(config_dir: str = None) -> Flask:
    """
    Create and configure the Flask application.

    Args:
        config_dir: Configuration directory path (default: /config or CONFIG_DIR env)

    Returns:
        Configured Flask application
    """
    if config_dir is None:
        config_dir = os.environ.get("CONFIG_DIR", "/config")

    # Create Flask app
    app = Flask(__name__, template_folder="templates", static_folder="static")

    # Configuration with persistent secret key
    app.config["SECRET_KEY"] = get_or_create_flask_secret(config_dir)
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)
    # SESSION_COOKIE_SECURE is set dynamically via ProxyFix + Talisman or per-request.
    # ProxyFix below trusts X-Forwarded-Proto so request.scheme == 'https' works.
    app.config["SESSION_COOKIE_SECURE"] = (
        os.environ.get("HTTPS", "false").lower() == "true"
    )
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["CONFIG_DIR"] = config_dir
    app.config["WTF_CSRF_CHECK_DEFAULT"] = False  # We apply CSRF selectively

    # Trust reverse-proxy headers (X-Forwarded-For, X-Forwarded-Proto, X-Forwarded-Host)
    # so request.scheme and request.remote_addr are correct behind nginx/traefik/etc.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    # Get CORS configuration
    cors_origins = get_cors_origins()

    # Initialize CSRF protection
    csrf.init_app(app)

    # CSRF exemptions are applied selectively per-endpoint after
    # blueprint registration.  See the loop below register_blueprint().

    # Initialize SocketIO with the app.
    # async_mode="threading" uses native Python threads + simple-websocket
    # for WebSocket support.  This avoids gevent monkey-patching which
    # caused worker starvation (all requests "Pending") on page refresh.
    socketio.init_app(
        app,
        async_mode="threading",
        cors_allowed_origins=cors_origins,
        ping_timeout=60,
        ping_interval=25,
    )

    # Initialize settings manager with the config_dir FIRST
    from .settings_manager import get_settings_manager

    sm = get_settings_manager(config_dir)

    # Apply saved log level so the web server starts with the correct verbosity
    from ..logging_config import setup_logging

    setup_logging(
        log_level=sm.get("log_level", "INFO"),
        rotation=sm.get("log_rotation_size", "10 MB"),
        retention=sm.get("log_retention_count", 5),
    )

    # Initialize job manager with SocketIO
    get_job_manager(config_dir=config_dir, socketio=socketio)

    # Initialize schedule manager
    schedule_manager = get_schedule_manager(config_dir=config_dir)

    # Set up scheduled job callback (uses module-level function for pickling)
    schedule_manager.set_run_job_callback(run_scheduled_job)

    # Register blueprints
    from .routes import api, limiter, main, register_socketio_handlers
    from .webhooks import webhooks_bp

    app.register_blueprint(main)
    app.register_blueprint(api)
    app.register_blueprint(webhooks_bp)

    # Selectively exempt API endpoints that use Bearer/X-Auth-Token
    # (external API calls, not browser-initiated).  Browser-initiated
    # POST endpoints remain CSRF-protected.
    _csrf_exempt_endpoints = [
        # Jobs — @api_token_required, called by external API / dashboard
        "api.get_jobs",
        "api.get_job",
        "api.create_job",
        "api.cancel_job",
        "api.get_job_logs",
        "api.get_worker_statuses",
        "api.delete_job",
        "api.clear_jobs",
        "api.get_job_stats",
        # Schedules — @api_token_required
        "api.get_schedules",
        "api.get_schedule",
        "api.create_schedule",
        "api.update_schedule",
        "api.delete_schedule",
        "api.enable_schedule",
        "api.disable_schedule",
        "api.run_schedule_now",
        # Token management
        "api.api_regenerate_token",
        # System config
        "api.get_config",
        # Libraries
        "api.get_libraries",
        # Webhooks — external POST from Radarr/Sonarr
        "webhooks_bp.radarr_webhook",
        "webhooks_bp.sonarr_webhook",
        "webhooks_bp.get_webhook_history",
        "webhooks_bp.clear_webhook_history",
    ]
    for _ep in _csrf_exempt_endpoints:
        _view = app.view_functions.get(_ep)
        if _view:
            csrf.exempt(_view)

    # Initialize rate limiter with app
    limiter.init_app(app)

    # Register SocketIO handlers
    register_socketio_handlers(socketio)

    # Setup redirect middleware - redirect to setup wizard if not configured
    @app.before_request
    def check_setup():
        """Redirect to setup wizard if not configured."""
        from flask import redirect, request, url_for

        from .auth import is_authenticated
        from .settings_manager import get_settings_manager

        # Skip for static files, API, login, setup, and logout
        exempt_endpoints = [
            "static",
            "main.login",
            "main.logout",
            "main.setup_wizard",
            # Auth endpoints
            "api.auth_status",
            "api.api_login",
            "api.health_check",
            # Setup wizard endpoints
            "api.get_setup_status",
            "api.get_setup_state",
            "api.save_setup_state",
            "api.complete_setup",
            "api.get_setup_token_info",
            "api.set_setup_token",
            "api.validate_paths",
            # Plex OAuth + server discovery (needed during setup)
            "api.create_plex_pin",
            "api.check_plex_pin",
            "api.get_plex_servers",
            "api.get_plex_libraries",
            "api.test_plex_connection",
            # Settings (read/write during setup)
            "api.get_settings",
            "api.save_settings",
            # System status (GPU detection during setup)
            "api.get_system_status",
            # Webhooks work pre-setup (return "disabled" gracefully)
            "webhooks_bp.radarr_webhook",
            "webhooks_bp.sonarr_webhook",
        ]

        # Only exempt specific setup-related API endpoints, not all api.*
        if request.endpoint and (request.endpoint in exempt_endpoints):
            return None

        # Skip static files
        if request.endpoint == "static" or request.path.startswith("/static"):
            return None

        # Check if setup is needed
        try:
            settings = get_settings_manager()
            if not settings.is_setup_complete() and not settings.is_configured():
                # Not configured and setup not complete - redirect to setup
                if (
                    request.endpoint not in ["main.login", "main.setup_wizard"]
                    and is_authenticated()
                ):
                    return redirect(url_for("main.setup_wizard"))
        except Exception as e:
            logger.debug(f"Setup check error: {e}")

        return None

    # Start scheduler
    schedule_manager.start()

    # Ensure the scheduler is shut down when the process exits to prevent
    # orphaned threads from lingering after gunicorn/Flask stops.
    # Call scheduler.shutdown() directly instead of schedule_manager.stop()
    # to avoid logger.info() writing to a closed stream during atexit.
    def _shutdown_scheduler() -> None:
        try:
            if schedule_manager.scheduler.running:
                schedule_manager.scheduler.shutdown(wait=False)
        except Exception:
            pass

    atexit.register(_shutdown_scheduler)

    # Log token on startup
    log_token_on_startup()

    logger.info(f"Flask app created with config_dir: {config_dir}")

    return app


def run_server(host: str = "0.0.0.0", port: int = 8080, debug: bool = False):
    """
    Run the web server with SocketIO.

    In production (Docker), use gunicorn via ``wrapper.sh`` instead.
    This function is kept for local development and tests.

    Args:
        host: Host to bind to
        port: Port to listen on
        debug: Enable debug mode
    """
    app = create_app()

    logger.info(f"Starting web server on {host}:{port}")
    logger.info(f"Access the dashboard at: http://{host}:{port}")

    socketio.run(
        app,
        host=host,
        port=port,
        debug=debug,
        use_reloader=False,  # Disable reloader to prevent issues with scheduler
        log_output=True,
        allow_unsafe_werkzeug=True,
    )


if __name__ == "__main__":
    run_server(debug=True)
