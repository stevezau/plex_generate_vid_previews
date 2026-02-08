"""
Flask application factory for the web interface.

Creates and configures the Flask application with SocketIO support.
"""

import os
import secrets
from datetime import timedelta
from pathlib import Path

from flask import Flask
from flask_socketio import SocketIO
from flask_wtf.csrf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix
from loguru import logger

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

    Defaults to the app's own origin (localhost:WEB_PORT) instead of "*".
    Set CORS_ORIGINS environment variable to override.

    Returns:
        CORS origins string
    """
    explicit = os.environ.get("CORS_ORIGINS")
    if explicit:
        return explicit
    port = os.environ.get("WEB_PORT", "8080")
    return f"http://localhost:{port}"


def get_or_create_flask_secret(config_dir: str) -> str:
    """
    Get Flask secret key from environment or persistent file.

    Priority:
    1. FLASK_SECRET_KEY environment variable
    2. /config/flask_secret.key file
    3. Generate new secret and save it

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

    # Check for persistent secret file
    secret_file = Path(config_dir) / "flask_secret.key"

    if secret_file.exists():
        try:
            secret = secret_file.read_text().strip()
            if secret:
                logger.debug(f"Using Flask secret from {secret_file}")
                return secret
        except IOError as e:
            logger.warning(f"Failed to read Flask secret file: {e}")

    # Generate new secret and save it
    new_secret = secrets.token_hex(32)
    try:
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        secret_file.write_text(new_secret)
        secret_file.chmod(0o600)
        logger.info(f"Generated new Flask secret and saved to {secret_file}")
    except IOError as e:
        logger.warning(f"Failed to save Flask secret to file: {e}")

    return new_secret


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

    # Exempt API endpoints that use token auth (not cookie-based)
    from .routes import api

    csrf.exempt(api)

    # Initialize SocketIO with the app
    socketio.init_app(
        app,
        async_mode="gevent",
        cors_allowed_origins=cors_origins,
        ping_timeout=60,
        ping_interval=25,
    )

    # Initialize settings manager with the config_dir FIRST
    from .settings_manager import get_settings_manager

    get_settings_manager(config_dir)

    # Initialize job manager with SocketIO
    get_job_manager(config_dir=config_dir, socketio=socketio)

    # Initialize schedule manager
    schedule_manager = get_schedule_manager(config_dir=config_dir)

    # Set up scheduled job callback (uses module-level function for pickling)
    schedule_manager.set_run_job_callback(run_scheduled_job)

    # Register blueprints
    from .routes import main, api, register_socketio_handlers, limiter

    app.register_blueprint(main)
    app.register_blueprint(api)

    # Initialize rate limiter with app
    limiter.init_app(app)

    # Register SocketIO handlers
    register_socketio_handlers(socketio)

    # Setup redirect middleware - redirect to setup wizard if not configured
    @app.before_request
    def check_setup():
        """Redirect to setup wizard if not configured."""
        from flask import request, redirect, url_for
        from .settings_manager import get_settings_manager
        from .auth import is_authenticated

        # Skip for static files, API, login, setup, and logout
        exempt_endpoints = [
            "static",
            "main.login",
            "main.logout",
            "main.setup_wizard",
            "api.get_setup_status",
            "api.auth_status",
            "api.api_login",
            "api.health_check",
            "api.get_setup_token_info",
            "api.save_setup_state",
            "api.complete_setup",
            "api.set_setup_token",
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

    # Log token on startup
    log_token_on_startup()

    logger.info(f"Flask app created with config_dir: {config_dir}")

    return app


def run_server(host: str = "0.0.0.0", port: int = 8080, debug: bool = False):
    """
    Run the Flask development server with SocketIO.

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
    )


if __name__ == "__main__":
    run_server(debug=True)
