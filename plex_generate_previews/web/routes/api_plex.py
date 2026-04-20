"""Plex OAuth, server discovery, and connection test API routes."""

import urllib3
from flask import jsonify, request
from loguru import logger

from ..auth import setup_or_auth_required
from . import api
from ._helpers import _param_to_bool

PLEX_HEADERS = {
    "X-Plex-Product": "Plex Preview Generator",
    "X-Plex-Version": "1.0.0",
    "X-Plex-Platform": "Web",
    "Accept": "application/json",
}


@api.route("/plex/auth/pin", methods=["POST"])
@setup_or_auth_required
def create_plex_pin():
    """Create a new PIN for Plex OAuth authentication."""
    import requests

    from ..settings_manager import get_settings_manager

    settings = get_settings_manager()
    client_id = settings.get_client_identifier()

    headers = {
        **PLEX_HEADERS,
        "X-Plex-Client-Identifier": client_id,
    }

    try:
        response = requests.post(
            "https://plex.tv/api/v2/pins",
            headers=headers,
            data={"strong": "true"},
            timeout=10,
        )
        response.raise_for_status()
        pin_data = response.json()

        auth_url = f"https://app.plex.tv/auth#?clientID={client_id}&code={pin_data['code']}&context%5Bdevice%5D%5Bproduct%5D=Plex%20Preview%20Generator"

        return jsonify(
            {
                "id": pin_data["id"],
                "code": pin_data["code"],
                "auth_url": auth_url,
            }
        )
    except requests.RequestException as e:
        logger.error(f"Failed to create Plex PIN: {e}")
        return jsonify({"error": "Failed to create PIN"}), 500


@api.route("/plex/auth/pin/<int:pin_id>")
@setup_or_auth_required
def check_plex_pin(pin_id: int):
    """Check if a PIN has been authenticated."""
    import requests

    from ..settings_manager import get_settings_manager

    settings = get_settings_manager()
    client_id = settings.get_client_identifier()

    headers = {
        **PLEX_HEADERS,
        "X-Plex-Client-Identifier": client_id,
    }

    try:
        response = requests.get(
            f"https://plex.tv/api/v2/pins/{pin_id}",
            headers=headers,
            timeout=10,
        )
        response.raise_for_status()
        pin_data = response.json()

        auth_token = pin_data.get("authToken")

        if auth_token:
            settings.plex_token = auth_token
            logger.info("Plex authentication successful, token saved")

        return jsonify(
            {
                "authenticated": bool(auth_token),
            }
        )
    except requests.RequestException as e:
        logger.error(f"Failed to check Plex PIN: {e}")
        return jsonify({"error": "Failed to check PIN"}), 500


@api.route("/plex/servers")
@setup_or_auth_required
def get_plex_servers():
    """Get user's Plex servers."""
    import requests

    from ..settings_manager import get_settings_manager

    settings = get_settings_manager()
    client_id = settings.get_client_identifier()

    token = request.headers.get("X-Plex-Token") or settings.plex_token
    if not token:
        return jsonify({"error": "No Plex token available", "servers": []}), 401

    headers = {
        **PLEX_HEADERS,
        "X-Plex-Client-Identifier": client_id,
        "X-Plex-Token": token,
    }

    try:
        response = requests.get(
            "https://plex.tv/api/v2/resources",
            headers=headers,
            params={"includeHttps": "1", "includeRelay": "1"},
            timeout=15,
        )
        response.raise_for_status()
        resources = response.json()

        servers = []
        for resource in resources:
            if resource.get("provides") != "server":
                continue
            connections = resource.get("connections", []) or []
            local_conn = next((c for c in connections if c.get("local")), None)
            any_conn = connections[0] if connections else None
            best_conn = local_conn or any_conn
            if not best_conn:
                continue

            # Full list of usable connections so the Settings page can
            # render a picker when a server publishes more than one
            # connection (local IP + public URI + relay, etc.).
            connection_list = []
            for c in connections:
                uri = c.get("uri") or ""
                host = c.get("address") or ""
                port = c.get("port", 32400)
                protocol = c.get("protocol") or ("https" if str(uri).startswith("https") else "http")
                if not uri and host:
                    uri = f"{protocol}://{host}:{port}"
                connection_list.append(
                    {
                        "uri": uri,
                        "address": host,
                        "port": port,
                        "protocol": protocol,
                        "ssl": protocol == "https",
                        "local": bool(c.get("local")),
                        "relay": bool(c.get("relay")),
                    }
                )

            servers.append(
                {
                    "name": resource.get("name"),
                    "machine_id": resource.get("clientIdentifier"),
                    "host": best_conn.get("address"),
                    "port": best_conn.get("port", 32400),
                    "ssl": best_conn.get("protocol") == "https",
                    "uri": best_conn.get("uri"),
                    "owned": bool(resource.get("owned", False)),
                    "local": bool(best_conn.get("local")),
                    "connections": connection_list,
                }
            )

        return jsonify({"servers": servers})
    except requests.RequestException as e:
        logger.error(f"Failed to get Plex servers: {e}")
        return jsonify({"error": "Failed to get servers", "servers": []}), 500


@api.route("/plex/libraries")
@setup_or_auth_required
def get_plex_libraries():
    """Get libraries from a Plex server."""
    import requests

    from ..settings_manager import get_settings_manager
    from .api_system import _fetch_libraries_via_http

    settings = get_settings_manager()

    plex_url = request.args.get("url") or settings.plex_url
    plex_token = request.args.get("token") or settings.plex_token
    verify_ssl = _param_to_bool(request.args.get("verify_ssl"), settings.plex_verify_ssl)

    if not plex_url or not plex_token:
        return jsonify({"error": "Plex URL and token required", "libraries": []}), 400

    try:
        libraries = _fetch_libraries_via_http(
            plex_url,
            plex_token,
            verify_ssl=verify_ssl,
        )
        return jsonify({"libraries": libraries})
    except requests.ConnectionError:
        detail = f"Could not connect to Plex at {plex_url}"
        logger.error(f"Failed to get Plex libraries: {detail}")
        return jsonify(
            {
                "error": f"{detail}. Check the server URL and ensure Plex is running and reachable from this host.",
                "libraries": [],
            }
        ), 502
    except requests.Timeout:
        detail = f"Connection to Plex at {plex_url} timed out"
        logger.error(f"Failed to get Plex libraries: {detail}")
        return jsonify(
            {
                "error": f"{detail}. The server may be overloaded or unreachable.",
                "libraries": [],
            }
        ), 504
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else "unknown"
        if status == 401:
            detail = "Plex rejected the authentication token"
            hint = "Re-authenticate with Plex or check your token."
        elif status == 403:
            detail = "Access denied by Plex server"
            hint = "Ensure your account has access to this server."
        else:
            detail = f"Plex returned HTTP {status}"
            hint = "Check Plex server logs for details."
        logger.error(f"Failed to get Plex libraries: {detail} (HTTP {status})")
        return jsonify({"error": f"{detail}. {hint}", "libraries": []}), 502
    except requests.RequestException as e:
        logger.error(f"Failed to get Plex libraries: {e}")
        return jsonify({"error": f"Failed to get libraries: {e}", "libraries": []}), 500


@api.route("/plex/test", methods=["POST"])
@setup_or_auth_required
def test_plex_connection():
    """Test connection to a Plex server."""
    import requests

    from ..settings_manager import get_settings_manager

    settings = get_settings_manager()
    data = request.get_json() or {}

    plex_url = data.get("url") or settings.plex_url
    plex_token = data.get("token") or settings.plex_token
    verify_ssl = _param_to_bool(data.get("verify_ssl"), settings.plex_verify_ssl)

    if not plex_url or not plex_token:
        return jsonify({"success": False, "error": "URL and token required"}), 400

    try:
        if not verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        response = requests.get(
            f"{plex_url.rstrip('/')}/",
            headers={"X-Plex-Token": plex_token, "Accept": "application/json"},
            timeout=10,
            verify=verify_ssl,
        )
        response.raise_for_status()
        data = response.json()

        server_name = data.get("MediaContainer", {}).get("friendlyName", "Unknown Server")

        return jsonify(
            {
                "success": True,
                "server_name": server_name,
                "error": None,
            }
        )
    except requests.exceptions.SSLError as e:
        logger.error(f"Plex connection test failed (SSL): {e}")
        return jsonify(
            {
                "success": False,
                "server_name": None,
                "error": (
                    f"SSL certificate verification failed: {e}. "
                    "If you're using a self-signed certificate or an internal CA, "
                    "uncheck 'Verify SSL'."
                ),
            }
        )
    except requests.exceptions.Timeout:
        logger.error(f"Plex connection test failed: timed out contacting {plex_url}")
        return jsonify(
            {
                "success": False,
                "server_name": None,
                "error": (
                    f"Connection to {plex_url} timed out after 10s. The server may be unreachable or overloaded."
                ),
            }
        )
    except requests.exceptions.ConnectionError as e:
        logger.error(f"Plex connection test failed: {e}")
        return jsonify(
            {
                "success": False,
                "server_name": None,
                "error": (
                    f"Could not connect to Plex at {plex_url}. "
                    "Check the URL and that the server is running and reachable from this host."
                ),
            }
        )
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status == 401:
            detail = "Plex rejected the authentication token (401). Re-authenticate with Plex or check your token."
        elif status == 403:
            detail = "Access denied by Plex server (403). Ensure your account has access."
        elif status == 404:
            detail = f"URL reachable but did not return Plex server identity (404). Check '{plex_url}' is your Plex base URL."
        else:
            detail = f"Plex returned HTTP {status}."
        logger.error(f"Plex connection test failed: HTTP {status}")
        return jsonify({"success": False, "server_name": None, "error": detail})
    except requests.exceptions.MissingSchema:
        return jsonify(
            {
                "success": False,
                "server_name": None,
                "error": f"Invalid URL '{plex_url}'. Include the scheme, e.g. http://plex:32400.",
            }
        )
    except requests.exceptions.InvalidURL as e:
        return jsonify(
            {
                "success": False,
                "server_name": None,
                "error": f"Invalid URL '{plex_url}': {e}",
            }
        )
    except requests.RequestException as e:
        logger.error(f"Plex connection test failed: {e}")
        return jsonify(
            {
                "success": False,
                "server_name": None,
                "error": f"Connection test failed: {e}",
            }
        )
    except ValueError as e:
        logger.error(f"Plex connection test failed (invalid JSON): {e}")
        return jsonify(
            {
                "success": False,
                "server_name": None,
                "error": (
                    "Server responded but did not return valid Plex data. The URL may not be pointing at a Plex server."
                ),
            }
        )
