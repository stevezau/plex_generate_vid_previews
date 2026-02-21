"""
Mock plex.tv OAuth server for E2E testing.

Mimics the plex.tv API for OAuth PIN-based authentication.
Run standalone: python -m tests.mocks.mock_plex_tv
"""

import html
import os
import time
import uuid
from flask import Flask, jsonify, request, Response
from flask_cors import CORS


app = Flask(__name__)
CORS(app)  # Enable CORS for OAuth popup testing

# In-memory PIN storage
pins = {}

# Mock user data
MOCK_USER = {
    "id": 12345,
    "uuid": "user-uuid-12345",
    "username": "testuser",
    "email": "test@example.com",
    "authToken": "mock-auth-token-xyz789",
    "subscription": {
        "active": True,
        "plan": "lifetime",
    },
}

# Mock server resources
MOCK_RESOURCES = [
    {
        "name": "Test Plex Server",
        "product": "Plex Media Server",
        "productVersion": "1.40.0.7775",
        "platform": "Linux",
        "platformVersion": "5.15.0",
        "device": "PC",
        "clientIdentifier": "abc123def456",
        "createdAt": "2024-01-01T00:00:00Z",
        "lastSeenAt": "2024-01-15T12:00:00Z",
        "provides": "server",
        "ownerId": 12345,
        "sourceTitle": None,
        "publicAddress": "203.0.113.1",
        "accessToken": "server-access-token-123",
        "owned": True,
        "home": False,
        "synced": False,
        "relay": True,
        "presence": True,
        "httpsRequired": False,
        "publicAddressMatches": True,
        "dnsRebindingProtection": False,
        "natLoopbackSupported": True,
        "connections": [
            {
                "protocol": "http",
                "address": "192.168.1.100",
                "port": 32400,
                "uri": "http://192.168.1.100:32400",
                "local": True,
                "relay": False,
                "IPv6": False,
            },
            {
                "protocol": "http",
                "address": "localhost",
                "port": 32401,  # Mock server port
                "uri": "http://localhost:32401",
                "local": True,
                "relay": False,
                "IPv6": False,
            },
        ],
    }
]


@app.route("/api/v2/pins", methods=["POST"])
def create_pin():
    """Create a new PIN for OAuth authentication."""
    # Generate a unique PIN
    pin_id = len(pins) + 1
    code = str(uuid.uuid4())[:8].upper()

    pin_data = {
        "id": pin_id,
        "code": code,
        "product": request.headers.get("X-Plex-Product", "Plex Web"),
        "trusted": False,
        "clientIdentifier": request.headers.get("X-Plex-Client-Identifier", "unknown"),
        "expiresIn": 1800,
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "expiresAt": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 1800)
        ),
        "authToken": None,  # Not authenticated yet
        "newRegistration": None,
    }

    pins[pin_id] = pin_data

    return jsonify(pin_data)


@app.route("/api/v2/pins/<int:pin_id>", methods=["GET"])
def check_pin(pin_id: int):
    """Check if a PIN has been authenticated."""
    if pin_id not in pins:
        return jsonify({"errors": [{"code": 1020, "message": "PIN not found"}]}), 404

    pin_data = pins[pin_id]
    return jsonify(pin_data)


@app.route("/api/v2/pins/<int:pin_id>/link", methods=["PUT"])
def link_pin(pin_id: int):
    """Simulate user authenticating the PIN (called by test to approve)."""
    if pin_id not in pins:
        return jsonify({"errors": [{"code": 1020, "message": "PIN not found"}]}), 404

    # Mark as authenticated
    pins[pin_id]["authToken"] = MOCK_USER["authToken"]
    pins[pin_id]["trusted"] = True

    return jsonify(pins[pin_id])


@app.route("/api/v2/user", methods=["GET"])
def get_user():
    """Get current user information."""
    token = request.headers.get("X-Plex-Token")
    if not token:
        return jsonify({"errors": [{"code": 1001, "message": "Unauthorized"}]}), 401

    return jsonify(MOCK_USER)


@app.route("/api/v2/resources", methods=["GET"])
def get_resources():
    """Get user's Plex resources (servers)."""
    token = request.headers.get("X-Plex-Token")
    if not token:
        return jsonify({"errors": [{"code": 1001, "message": "Unauthorized"}]}), 401

    # Return JSON for newer API
    return jsonify(MOCK_RESOURCES)


@app.route("/pms/resources", methods=["GET"])
def get_resources_xml():
    """Get user's Plex resources in XML format (legacy)."""
    token = request.headers.get("X-Plex-Token") or request.args.get("X-Plex-Token")
    if not token:
        return Response(
            '<?xml version="1.0"?><Response code="401" status="Unauthorized"/>',
            status=401,
            mimetype="application/xml",
        )

    resources_xml = ""
    for resource in MOCK_RESOURCES:
        connections = ""
        for conn in resource.get("connections", []):
            connections += f'<Connection protocol="{conn["protocol"]}" address="{conn["address"]}" port="{conn["port"]}" uri="{conn["uri"]}" local="{1 if conn["local"] else 0}"/>'

        resources_xml += f'''<Device name="{resource["name"]}" product="{resource["product"]}"
            productVersion="{resource["productVersion"]}" platform="{resource["platform"]}"
            clientIdentifier="{resource["clientIdentifier"]}" provides="{resource["provides"]}"
            owned="{1 if resource["owned"] else 0}" accessToken="{resource.get("accessToken", "")}">
            {connections}
        </Device>'''

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<MediaContainer size="{len(MOCK_RESOURCES)}">
{resources_xml}
</MediaContainer>"""
    return Response(xml, mimetype="application/xml")


# OAuth login page simulation
@app.route("/auth")
def auth_page():
    """Simulated OAuth login page."""
    pin_id = html.escape(request.args.get("pin", ""))
    code = html.escape(request.args.get("code", ""))

    # In real testing, the test would call /api/v2/pins/<id>/link to approve
    page = f"""<!DOCTYPE html>
<html>
<head><title>Mock Plex Login</title></head>
<body>
    <h1>Mock Plex Login</h1>
    <p>PIN ID: {pin_id}</p>
    <p>Code: {code}</p>
    <p>This is a mock login page for E2E testing.</p>
    <p>Call PUT /api/v2/pins/{pin_id}/link to simulate successful authentication.</p>
</body>
</html>"""
    return page


# Health check
@app.route("/health")
def health():
    """Health check endpoint."""
    return jsonify({"status": "healthy", "server": "mock_plex_tv"})


if __name__ == "__main__":
    port = int(os.environ.get("MOCK_PLEX_TV_PORT", 32402))
    print(f"Starting mock plex.tv server on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
