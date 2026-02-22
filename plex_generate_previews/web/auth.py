"""
Token-based authentication for the web interface.

Generates and validates authentication tokens, with support for
environment variable override and persistent storage.
"""

import json
import os
import secrets
from functools import wraps

from flask import request, jsonify, session, redirect, url_for
from loguru import logger


# Default config directory
CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
AUTH_FILE = os.path.join(CONFIG_DIR, "auth.json")


def get_config_dir() -> str:
    """Get the configuration directory path."""
    return CONFIG_DIR


def generate_token() -> str:
    """Generate a cryptographically secure random token."""
    return secrets.token_urlsafe(32)


def load_auth_config() -> dict:
    """Load authentication configuration from file."""
    if os.path.exists(AUTH_FILE):
        try:
            with open(AUTH_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Failed to load auth config: {e}")
    return {}


def save_auth_config(config: dict) -> None:
    """Save authentication configuration to file."""
    os.makedirs(os.path.dirname(AUTH_FILE), exist_ok=True)
    try:
        with open(AUTH_FILE, "w") as f:
            json.dump(config, f, indent=2)
        os.chmod(AUTH_FILE, 0o600)
    except IOError as e:
        logger.error(f"Failed to save auth config: {e}")


def get_auth_token() -> str:
    """
    Get the authentication token.

    Priority:
    1. WEB_AUTH_TOKEN environment variable
    2. Token from /config/auth.json
    3. Generate new token and save it
    """
    # Check environment variable first
    env_token = os.environ.get("WEB_AUTH_TOKEN")
    if env_token:
        logger.info(
            "Using authentication token from WEB_AUTH_TOKEN environment variable"
        )
        return env_token

    # Check saved config
    config = load_auth_config()
    if "token" in config:
        return config["token"]

    # Generate new token
    new_token = generate_token()
    config["token"] = new_token
    save_auth_config(config)
    logger.info("Generated new authentication token (hidden)")
    return new_token


def regenerate_token() -> str:
    """Regenerate the authentication token."""
    # Don't regenerate if using environment variable
    if os.environ.get("WEB_AUTH_TOKEN"):
        logger.warning("Cannot regenerate token when WEB_AUTH_TOKEN is set")
        return os.environ.get("WEB_AUTH_TOKEN")

    new_token = generate_token()
    config = load_auth_config()
    config["token"] = new_token
    save_auth_config(config)
    logger.info("Regenerated authentication token (hidden)")
    return new_token


def validate_token(token: str) -> bool:
    """Validate the provided token against the stored token."""
    return secrets.compare_digest(token, get_auth_token())


def is_token_env_controlled() -> bool:
    """Check if the token is controlled by WEB_AUTH_TOKEN environment variable."""
    return bool(os.environ.get("WEB_AUTH_TOKEN"))


def set_auth_token(new_token: str) -> dict:
    """
    Set a custom authentication token.

    Args:
        new_token: The new token to set (minimum 8 characters)

    Returns:
        dict with 'success' bool and optional 'error' message
    """
    # Check if controlled by environment variable
    if is_token_env_controlled():
        return {
            "success": False,
            "error": "Token is controlled by WEB_AUTH_TOKEN environment variable and cannot be changed.",
        }

    # Validate minimum length
    if len(new_token) < 8:
        return {"success": False, "error": "Token must be at least 8 characters long."}

    # Save the new token
    config = load_auth_config()
    config["token"] = new_token
    save_auth_config(config)
    logger.info("Authentication token updated by user")

    return {"success": True}


def get_token_info() -> dict:
    """
    Get information about the current token for display in setup wizard.

    Returns:
        dict with token info (masked for security) and control status
    """
    env_controlled = is_token_env_controlled()
    token = get_auth_token()

    # Mask token: show only last 4 chars
    masked_token = "****" + token[-4:] if len(token) > 4 else "****"
    return {
        "env_controlled": env_controlled,
        "token": masked_token,
        "token_length": len(token),
        "source": "environment" if env_controlled else "config",
    }


def is_authenticated() -> bool:
    """Check if the current session is authenticated."""
    return session.get("authenticated", False)


def login_required(f):
    """Decorator to require authentication for a route."""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_authenticated():
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "Authentication required"}), 401
            return redirect(url_for("main.login"))
        return f(*args, **kwargs)

    return decorated_function


def api_token_required(f):
    """Decorator to require API token for API routes."""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Check session first
        if is_authenticated():
            return f(*args, **kwargs)

        # Check Authorization header
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            if validate_token(token):
                return f(*args, **kwargs)

        # Check X-Auth-Token header
        token = request.headers.get("X-Auth-Token", "")
        if token and validate_token(token):
            return f(*args, **kwargs)

        return jsonify({"error": "Authentication required"}), 401

    return decorated_function


def setup_or_auth_required(f):
    """Decorator that allows unauthenticated access during setup, requires auth after.

    If the setup wizard has not been completed, the request is allowed without
    authentication (the wizard needs open access to configure the app).
    Once setup is complete, falls through to the same logic as
    ``api_token_required`` — checking session, Bearer token, and X-Auth-Token.
    """

    @wraps(f)
    def decorated_function(*args, **kwargs):
        from .settings_manager import get_settings_manager

        settings = get_settings_manager()
        if not settings.is_setup_complete():
            # Setup not complete — allow unauthenticated access
            return f(*args, **kwargs)

        # Setup complete — require authentication (same checks as api_token_required)
        if is_authenticated():
            return f(*args, **kwargs)

        # Check Authorization header
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            if validate_token(token):
                return f(*args, **kwargs)

        # Check X-Auth-Token header
        token = request.headers.get("X-Auth-Token", "")
        if token and validate_token(token):
            return f(*args, **kwargs)

        return jsonify({"error": "Authentication required"}), 401

    return decorated_function


def log_token_on_startup() -> None:
    """
    Log authentication token information on startup.

    Tokens are never logged in full for security reasons.
    """
    token = get_auth_token()
    masked = "****" + token[-4:] if len(token) > 4 else "****"
    logger.info("=" * 60)
    logger.info("WEB AUTHENTICATION TOKEN")
    logger.info("=" * 60)
    logger.info(f"Token: {masked}")
    logger.info("Check /config/auth.json for the full token.")
    logger.info("You can also set WEB_AUTH_TOKEN environment variable.")
    logger.info("=" * 60)
