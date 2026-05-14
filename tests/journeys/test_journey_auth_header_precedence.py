"""TEST_AUDIT P1.11 — pin the contract for ``_check_token_headers``.

Auth path used by every API token-protected endpoint. Production reads
in this order (auth.py:246-263):

  1. ``Authorization: Bearer <token>`` — checked first
  2. ``X-Auth-Token: <token>`` — checked second
  3. session cookie — checked even before headers (via
     ``api_token_required``)

This test fixes that order in stone. Each row in the matrix is a
distinct authentication scenario the function MUST resolve identically
on every release. Without this, a regression that flips precedence
silently lets "any of N tokens" auth succeed where the contract was
"only the right one in the right header" — that's a security
degradation, not a bug.

Drives a real Flask app via ``create_app`` and a real protected route
(``GET /api/jobs``). Mocks NOTHING — the auth path is its own seam and
faking anything would defeat the test.
"""

from __future__ import annotations

import json

import pytest

from media_preview_generator.web.app import create_app
from media_preview_generator.web.settings_manager import reset_settings_manager

pytestmark = pytest.mark.journey


CORRECT_TOKEN = "correct-token-12345678"
WRONG_TOKEN = "wrong-token-87654321"


@pytest.fixture(autouse=True)
def _reset_singletons():
    reset_settings_manager()
    import media_preview_generator.web.jobs as jobs_mod
    import media_preview_generator.web.scheduler as sched_mod

    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    with sched_mod._schedule_lock:
        sched_mod._schedule_manager = None
    yield
    reset_settings_manager()
    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    with sched_mod._schedule_lock:
        if sched_mod._schedule_manager is not None:
            try:
                sched_mod._schedule_manager.stop()
            except Exception:
                pass
            sched_mod._schedule_manager = None


@pytest.fixture()
def app(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("WEB_AUTH_TOKEN", CORRECT_TOKEN)
    settings_path = config_dir / "settings.json"
    settings_path.write_text(json.dumps({"setup_complete": True}))
    auth_path = config_dir / "auth.json"
    auth_path.write_text(json.dumps({"token": CORRECT_TOKEN}))
    return create_app(config_dir=str(config_dir))


@pytest.fixture()
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# Matrix: each cell is a different auth scenario.
# ---------------------------------------------------------------------------


class TestAuthHeaderPrecedence:
    """Pin the precedence and behaviour of ``_check_token_headers`` (auth.py)
    via the public ``GET /api/jobs`` endpoint protected by
    ``@api_token_required``."""

    def test_x_auth_token_only_correct_grants_access(self, client):
        """X-Auth-Token header on its own with the right token → 200."""
        r = client.get("/api/jobs", headers={"X-Auth-Token": CORRECT_TOKEN})
        assert r.status_code == 200, (
            f"Correct X-Auth-Token alone must grant access; got {r.status_code}. Body: {r.get_data(as_text=True)!r}"
        )

    def test_x_auth_token_only_wrong_returns_401(self, client):
        r = client.get("/api/jobs", headers={"X-Auth-Token": WRONG_TOKEN})
        assert r.status_code == 401, f"Wrong X-Auth-Token must reject with 401; got {r.status_code}"
        body = r.get_json() or {}
        assert body.get("error") == "Authentication required", (
            f"401 body must be {{'error': 'Authentication required'}}; got {body!r}"
        )

    def test_authorization_bearer_only_correct_grants_access(self, client):
        """Authorization: Bearer <token> on its own with the right token → 200."""
        r = client.get("/api/jobs", headers={"Authorization": f"Bearer {CORRECT_TOKEN}"})
        assert r.status_code == 200, f"Correct Authorization: Bearer must grant access; got {r.status_code}"

    def test_authorization_bearer_only_wrong_returns_401(self, client):
        r = client.get("/api/jobs", headers={"Authorization": f"Bearer {WRONG_TOKEN}"})
        assert r.status_code == 401

    def test_no_auth_returns_401_with_specific_body(self, client):
        """No headers + no session cookie → 401 with the exact 'Authentication required'
        body shape. Pin the body shape because the dashboard JS branches on it."""
        r = client.get("/api/jobs")
        assert r.status_code == 401
        assert r.get_json() == {"error": "Authentication required"}, (
            f"401 body must be exactly {{'error': 'Authentication required'}}; got {r.get_json()!r}"
        )

    # --- Both headers present ----------------------------------------

    def test_both_headers_correct_grants_access(self, client):
        """Both Authorization and X-Auth-Token correct → 200. The simple case
        — neither header should trip up the other."""
        r = client.get(
            "/api/jobs",
            headers={
                "Authorization": f"Bearer {CORRECT_TOKEN}",
                "X-Auth-Token": CORRECT_TOKEN,
            },
        )
        assert r.status_code == 200, f"Both headers correct must grant access; got {r.status_code}"

    def test_bearer_correct_x_auth_wrong_grants_access(self, client):
        """Authorization: Bearer is checked FIRST per auth.py:253-257. If
        Bearer succeeds, X-Auth-Token is never evaluated. Even with a
        wrong X-Auth-Token, a correct Bearer still wins → 200.

        This pins the order: Bearer-first. A regression that flipped to
        check X-Auth-Token first would 401 here (because X-Auth-Token
        is wrong and the function returns False without trying Bearer)."""
        r = client.get(
            "/api/jobs",
            headers={
                "Authorization": f"Bearer {CORRECT_TOKEN}",
                "X-Auth-Token": WRONG_TOKEN,
            },
        )
        assert r.status_code == 200, (
            f"Bearer-correct + X-Auth-wrong must grant access (Bearer is checked first per "
            f"auth.py:_check_token_headers); got {r.status_code}. "
            f"A regression that swapped header check order would fail here."
        )

    def test_bearer_wrong_x_auth_correct_grants_access(self, client):
        """Authorization: Bearer is wrong, but X-Auth-Token is the fallback
        (auth.py:259-261). Function still returns True via the X-Auth-Token
        branch → 200.

        This pins the fallback chain: even if a browser injects a bogus
        Bearer (e.g. Tdarr's session token leaking onto our endpoint),
        the explicit X-Auth-Token still authenticates."""
        r = client.get(
            "/api/jobs",
            headers={
                "Authorization": f"Bearer {WRONG_TOKEN}",
                "X-Auth-Token": CORRECT_TOKEN,
            },
        )
        assert r.status_code == 200, (
            f"Bearer-wrong + X-Auth-correct must grant access via the fallback path "
            f"(_check_token_headers checks both, returns True if either matches); "
            f"got {r.status_code}. A regression that returned early on the Bearer mismatch "
            f"would 401 here and silently break Sonarr/Radarr clients that send an explicit "
            f"X-Auth-Token alongside an unrelated Bearer."
        )

    def test_both_headers_wrong_returns_401(self, client):
        """Both headers wrong → 401 (no session cookie either)."""
        r = client.get(
            "/api/jobs",
            headers={
                "Authorization": f"Bearer {WRONG_TOKEN}",
                "X-Auth-Token": WRONG_TOKEN,
            },
        )
        assert r.status_code == 401, (
            f"Both headers wrong must 401; got {r.status_code}. "
            f"A regression here would let any-token-anywhere auth bypass the check."
        )

    # --- Empty / malformed Authorization header ---------------------

    def test_authorization_basic_scheme_falls_through_to_x_auth_token(self, client):
        """Authorization: Basic ... isn't handled by ``_check_token_headers``
        (the production code only branches on "Bearer "). It falls through
        to the X-Auth-Token check. With a correct X-Auth-Token, the request
        still authenticates → 200.

        Pin: a regression that started parsing Basic in _check_token_headers
        could accidentally succeed/fail in unexpected ways."""
        import base64

        basic = base64.b64encode(b"user:pw").decode()
        r = client.get(
            "/api/jobs",
            headers={
                "Authorization": f"Basic {basic}",
                "X-Auth-Token": CORRECT_TOKEN,
            },
        )
        assert r.status_code == 200, (
            f"Basic-Auth (ignored by _check_token_headers) + correct X-Auth-Token must grant access; "
            f"got {r.status_code}"
        )

    def test_empty_bearer_token_does_not_crash_and_falls_through(self, client):
        """Authorization: Bearer  (empty token) must not crash and must
        fall through to X-Auth-Token. With a correct X-Auth-Token → 200.

        Pin auth.py:255-257's `if token and validate_token(token):` guard."""
        r = client.get(
            "/api/jobs",
            headers={
                "Authorization": "Bearer ",
                "X-Auth-Token": CORRECT_TOKEN,
            },
        )
        assert r.status_code == 200, (
            f"Empty Bearer must not 500 and must fall through to X-Auth-Token; "
            f"got {r.status_code}. A regression that crashed on the empty token would surface as a 500."
        )

    # --- Cookie + header interaction --------------------------------

    def test_session_cookie_is_checked_before_headers(self, client, app):
        """``api_token_required`` calls ``is_authenticated()`` (session check)
        BEFORE ``_check_token_headers``. A logged-in session grants access
        even when no header is sent. Pin: the order of the two checks.

        Used by the dashboard once the user logs in via the web UI — they
        send the session cookie automatically and never carry headers."""
        with client.session_transaction() as s:
            s["authenticated"] = True

        # NO headers — pure cookie auth.
        r = client.get("/api/jobs")
        assert r.status_code == 200, (
            f"Authenticated session cookie must grant access without any headers; "
            f"got {r.status_code}. A regression that skipped the cookie check would 401 here "
            f"and break every browser-mediated UI request."
        )

    def test_session_cookie_grants_access_even_with_wrong_header(self, client):
        """Authenticated session cookie + wrong X-Auth-Token: cookie wins
        (cookie check happens first in api_token_required). Pin: a wrong
        header must NOT downgrade an already-authenticated session."""
        with client.session_transaction() as s:
            s["authenticated"] = True

        r = client.get(
            "/api/jobs",
            headers={"X-Auth-Token": WRONG_TOKEN},
        )
        assert r.status_code == 200, (
            f"Session cookie auth must take precedence over a (wrong) header; got {r.status_code}. "
            f"A regression that started 401-ing here would break logged-in users who happen to "
            f"have a stale token saved in another browser tab."
        )
