"""Tests for the Emby username+password authentication helper."""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import requests

from media_preview_generator.servers.emby_auth import (
    EmbyAuthResult,
    _emby_authorization_header,
    authenticate_emby_with_password,
)


class TestAuthorizationHeader:
    def test_includes_required_fields(self):
        header = _emby_authorization_header(device_id="abc123")
        # Emby/Jellyfin reject auth without all four fields.
        assert "Client=" in header
        assert "Device=" in header
        assert 'DeviceId="abc123"' in header
        assert "Version=" in header
        assert header.startswith("MediaBrowser ")


class TestAuthenticateEmbyWithPassword:
    def test_success_returns_token_and_user_id(self):
        with patch("media_preview_generator.servers._mediabrowser_auth.requests.post") as post:
            response = MagicMock(status_code=200)
            response.json.return_value = {
                "AccessToken": "tok-abc",
                "ServerId": "srv-xyz",
                "User": {"Id": "user-1", "ServerName": "Office Emby"},
            }
            post.return_value = response

            result = authenticate_emby_with_password(
                base_url="http://emby:8096",
                username="admin",
                password="hunter2",
                device_id_override="test-device",
            )

        assert isinstance(result, EmbyAuthResult)
        assert result.ok is True
        assert result.access_token == "tok-abc"
        assert result.user_id == "user-1"
        assert result.server_id == "srv-xyz"

    def test_calls_correct_endpoint_with_correct_body(self):
        with patch("media_preview_generator.servers._mediabrowser_auth.requests.post") as post:
            response = MagicMock(status_code=200)
            response.json.return_value = {"AccessToken": "tok", "User": {"Id": "1"}}
            post.return_value = response

            authenticate_emby_with_password(
                base_url="http://emby:8096",
                username="admin",
                password="pw",
                device_id_override="d1",
            )

            args, kwargs = post.call_args
            assert args[0] == "http://emby:8096/Users/AuthenticateByName"
            assert kwargs["json"] == {"Username": "admin", "Pw": "pw"}
            # Authorization header contains the strict scheme.
            assert "MediaBrowser " in kwargs["headers"]["Authorization"]
            assert 'DeviceId="d1"' in kwargs["headers"]["Authorization"]

    def test_strips_trailing_slash_from_base_url(self):
        with patch("media_preview_generator.servers._mediabrowser_auth.requests.post") as post:
            response = MagicMock(status_code=200)
            response.json.return_value = {"AccessToken": "tok", "User": {"Id": "1"}}
            post.return_value = response

            authenticate_emby_with_password(
                base_url="http://emby:8096/",
                username="admin",
                password="pw",
            )

            url = post.call_args.args[0]
            assert url == "http://emby:8096/Users/AuthenticateByName"

    def test_401_returns_specific_message(self):
        with patch("media_preview_generator.servers._mediabrowser_auth.requests.post") as post:
            response = MagicMock(status_code=401, text="Bad credentials")
            post.return_value = response

            result = authenticate_emby_with_password(
                base_url="http://emby:8096",
                username="admin",
                password="wrong",
            )

        # Production format: "Emby rejected the username/password (401)".
        # Anchor on word-boundary so a regression returning "4015" or
        # "HTTP 4010" (status drift) still trips this assertion.
        assert result.ok is False
        assert post.call_count == 1, "401 path must have made the HTTP call (not short-circuited)"
        assert re.search(r"\b401\b", result.message), f"expected '401' as a standalone token in {result.message!r}"
        assert "rejected" in result.message.lower(), (
            f"401 message must explain it was a credential rejection, not a generic 401: {result.message!r}"
        )

    def test_403_returns_specific_message(self):
        with patch("media_preview_generator.servers._mediabrowser_auth.requests.post") as post:
            response = MagicMock(status_code=403, text="Forbidden")
            post.return_value = response

            result = authenticate_emby_with_password(
                base_url="http://emby:8096",
                username="admin",
                password="pw",
            )

        # Production format: "Access denied by Emby server (403)".
        assert result.ok is False
        assert post.call_count == 1, "403 path must have made the HTTP call"
        assert re.search(r"\b403\b", result.message), f"expected '403' as a standalone token in {result.message!r}"
        assert "denied" in result.message.lower(), f"403 message must explain access was denied: {result.message!r}"

    def test_other_4xx_5xx_returns_status_in_message(self):
        with patch("media_preview_generator.servers._mediabrowser_auth.requests.post") as post:
            response = MagicMock(status_code=500, text="server fire")
            post.return_value = response

            result = authenticate_emby_with_password(
                base_url="http://emby:8096",
                username="admin",
                password="pw",
            )

        # Production format: "Emby returned HTTP 500: <body>".
        assert not result.ok
        assert post.call_count == 1, "500 path must have made the HTTP call"
        assert re.search(r"\b500\b", result.message), f"expected '500' as a standalone token in {result.message!r}"
        # The body should be surfaced (truncated) so users can see what
        # the server actually said.
        assert "server fire" in result.message, f"server response body must be surfaced in error: {result.message!r}"

    def test_missing_access_token_treated_as_failure(self):
        with patch("media_preview_generator.servers._mediabrowser_auth.requests.post") as post:
            response = MagicMock(status_code=200)
            response.json.return_value = {"User": {"Id": "1"}}  # no AccessToken
            post.return_value = response

            result = authenticate_emby_with_password(
                base_url="http://emby:8096",
                username="admin",
                password="pw",
            )

        assert result.ok is False
        assert "AccessToken" in result.message

    def test_invalid_json_treated_as_failure(self):
        """Non-JSON 200 response → ok=False AND a useful error message.

        Audit fix — original asserted only ok=False. A regression returning
        ok=False with empty/None message would leave the user staring at a
        blank UI error. Assert the message is non-empty + mentions parsing.
        """
        with patch("media_preview_generator.servers._mediabrowser_auth.requests.post") as post:
            response = MagicMock(status_code=200)
            response.json.side_effect = ValueError("not json")
            post.return_value = response

            result = authenticate_emby_with_password(
                base_url="http://emby:8096",
                username="admin",
                password="pw",
            )

        assert result.ok is False
        assert result.message, "ok=False must surface a non-empty user-visible message"

    def test_timeout_returns_specific_message(self):
        with patch("media_preview_generator.servers._mediabrowser_auth.requests.post") as post:
            post.side_effect = requests.exceptions.Timeout()

            result = authenticate_emby_with_password(
                base_url="http://emby:8096",
                username="admin",
                password="pw",
            )

        assert not result.ok
        assert "timed out" in result.message.lower()

    def test_ssl_error_returns_specific_message(self):
        with patch("media_preview_generator.servers._mediabrowser_auth.requests.post") as post:
            post.side_effect = requests.exceptions.SSLError("bad cert")

            result = authenticate_emby_with_password(
                base_url="http://emby:8096",
                username="admin",
                password="pw",
            )

        assert not result.ok
        assert "ssl" in result.message.lower()

    def test_missing_url(self):
        result = authenticate_emby_with_password(
            base_url="",
            username="admin",
            password="pw",
        )
        assert not result.ok
        assert "url" in result.message.lower()

    def test_missing_username(self):
        result = authenticate_emby_with_password(
            base_url="http://emby:8096",
            username="",
            password="pw",
        )
        assert not result.ok
        assert "username" in result.message.lower()
