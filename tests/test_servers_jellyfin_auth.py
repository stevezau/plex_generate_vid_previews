"""Tests for the Jellyfin auth helpers (Quick Connect + password)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from media_preview_generator.servers.jellyfin_auth import (
    JellyfinAuthResult,
    QuickConnectInitiation,
    authenticate_jellyfin_with_password,
    exchange_quick_connect,
    initiate_quick_connect,
    poll_quick_connect,
    quick_connect_blocking,
)


class TestPasswordAuth:
    def test_success(self):
        with patch("media_preview_generator.servers._mediabrowser_auth.requests.post") as post:
            response = MagicMock(status_code=200)
            response.json.return_value = {
                "AccessToken": "tok-jf",
                "ServerId": "srv-jf",
                "User": {"Id": "user-jf", "ServerName": "Family Jellyfin"},
            }
            post.return_value = response

            result = authenticate_jellyfin_with_password(
                base_url="http://jellyfin:8096",
                username="admin",
                password="hunter2",
                device_id_override="d1",
            )

        assert result.ok is True
        assert result.access_token == "tok-jf"
        assert result.user_id == "user-jf"

    def test_uses_authenticatebyname_endpoint(self):
        with patch("media_preview_generator.servers._mediabrowser_auth.requests.post") as post:
            response = MagicMock(status_code=200)
            response.json.return_value = {"AccessToken": "tok", "User": {"Id": "1"}}
            post.return_value = response

            authenticate_jellyfin_with_password(
                base_url="http://jellyfin:8096",
                username="admin",
                password="pw",
            )

            assert post.call_args.args[0] == "http://jellyfin:8096/Users/AuthenticateByName"

    def test_unauthorized_returns_specific_message(self):
        with patch("media_preview_generator.servers._mediabrowser_auth.requests.post") as post:
            post.return_value = MagicMock(status_code=401, text="bad")

            result = authenticate_jellyfin_with_password(
                base_url="http://jellyfin:8096",
                username="admin",
                password="wrong",
            )

        assert not result.ok
        assert "401" in result.message

    def test_missing_url_short_circuits(self):
        result = authenticate_jellyfin_with_password(base_url="", username="x", password="y")
        assert not result.ok

    def test_missing_username_short_circuits(self):
        result = authenticate_jellyfin_with_password(base_url="http://jellyfin:8096", username="", password="y")
        assert not result.ok


class TestInitiateQuickConnect:
    def test_success_returns_code_and_secret(self):
        with patch("media_preview_generator.servers._mediabrowser_auth.requests.post") as post:
            response = MagicMock(status_code=200)
            response.json.return_value = {"Code": "ABC123", "Secret": "abc-secret"}
            post.return_value = response

            initiation, message = initiate_quick_connect(base_url="http://jellyfin:8096")

        assert isinstance(initiation, QuickConnectInitiation)
        assert initiation.code == "ABC123"
        assert initiation.secret == "abc-secret"
        assert message

    def test_401_explains_quick_connect_disabled(self):
        with patch("media_preview_generator.servers._mediabrowser_auth.requests.post") as post:
            post.return_value = MagicMock(status_code=401, text="forbidden")

            initiation, message = initiate_quick_connect(base_url="http://jellyfin:8096")

        assert initiation is None
        assert "Quick Connect" in message

    def test_missing_url(self):
        initiation, message = initiate_quick_connect(base_url="")
        assert initiation is None
        assert "url" in message.lower()

    def test_response_missing_fields(self):
        with patch("media_preview_generator.servers._mediabrowser_auth.requests.post") as post:
            response = MagicMock(status_code=200)
            response.json.return_value = {"Code": "ABC"}  # no Secret
            post.return_value = response

            initiation, message = initiate_quick_connect(base_url="http://jellyfin:8096")

        assert initiation is None


class TestPollQuickConnect:
    def test_pending_returns_false(self):
        with patch("media_preview_generator.servers.jellyfin_auth.requests.get") as get:
            response = MagicMock(status_code=200)
            response.json.return_value = {"Authenticated": False}
            get.return_value = response

            authenticated, message = poll_quick_connect(
                base_url="http://jellyfin:8096",
                secret="abc",
            )

        assert authenticated is False
        assert message

    def test_approved_returns_true(self):
        with patch("media_preview_generator.servers.jellyfin_auth.requests.get") as get:
            response = MagicMock(status_code=200)
            response.json.return_value = {"Authenticated": True}
            get.return_value = response

            authenticated, _ = poll_quick_connect(
                base_url="http://jellyfin:8096",
                secret="abc",
            )

        assert authenticated is True

    def test_404_handled(self):
        with patch("media_preview_generator.servers.jellyfin_auth.requests.get") as get:
            get.return_value = MagicMock(status_code=404)

            authenticated, message = poll_quick_connect(
                base_url="http://jellyfin:8096",
                secret="abc",
            )

        assert authenticated is False
        assert "expired" in message.lower() or "not found" in message.lower()


class TestExchangeQuickConnect:
    def test_success(self):
        with patch("media_preview_generator.servers._mediabrowser_auth.requests.post") as post:
            response = MagicMock(status_code=200)
            response.json.return_value = {
                "AccessToken": "qc-token",
                "ServerId": "srv",
                "User": {"Id": "u1"},
            }
            post.return_value = response

            result = exchange_quick_connect(
                base_url="http://jellyfin:8096",
                secret="abc",
            )

        assert result.ok is True
        assert result.access_token == "qc-token"

    def test_401_explains_not_yet_approved(self):
        with patch("media_preview_generator.servers._mediabrowser_auth.requests.post") as post:
            post.return_value = MagicMock(status_code=401, text="not approved")

            result = exchange_quick_connect(
                base_url="http://jellyfin:8096",
                secret="abc",
            )

        assert not result.ok
        assert "approved" in result.message.lower()

    def test_uses_authenticatewithquickconnect_endpoint(self):
        with patch("media_preview_generator.servers._mediabrowser_auth.requests.post") as post:
            response = MagicMock(status_code=200)
            response.json.return_value = {"AccessToken": "tok", "User": {"Id": "1"}}
            post.return_value = response

            exchange_quick_connect(
                base_url="http://jellyfin:8096",
                secret="abc",
            )

            assert post.call_args.args[0] == "http://jellyfin:8096/Users/AuthenticateWithQuickConnect"

    def test_missing_secret(self):
        result = exchange_quick_connect(base_url="http://jellyfin:8096", secret="")
        assert not result.ok


class TestQuickConnectBlocking:
    def test_returns_token_when_approved_first_poll(self):
        # Patch poll to return True immediately and exchange to return a token.
        with (
            patch(
                "media_preview_generator.servers.jellyfin_auth.poll_quick_connect",
                return_value=(True, "Approved"),
            ),
            patch("media_preview_generator.servers.jellyfin_auth.exchange_quick_connect") as exchange,
        ):
            exchange.return_value = JellyfinAuthResult(ok=True, access_token="tok")

            result = quick_connect_blocking(
                base_url="http://jellyfin:8096",
                secret="abc",
                deadline_seconds=10,
                poll_interval=0.0,
            )

        assert result.ok is True
        assert result.access_token == "tok"

    def test_times_out_when_never_approved(self):
        with patch(
            "media_preview_generator.servers.jellyfin_auth.poll_quick_connect",
            return_value=(False, "Pending"),
        ):
            result = quick_connect_blocking(
                base_url="http://jellyfin:8096",
                secret="abc",
                deadline_seconds=0,  # immediate timeout
                poll_interval=0.0,
            )

        assert not result.ok
        assert "deadline" in result.message.lower()
