"""Tests for the Plex webhook registration helper.

Mocks plexapi.MyPlexAccount entirely — no network is touched.
"""

from unittest.mock import MagicMock, patch

import pytest

from plex_generate_previews.web import plex_webhook_registration as pwh


@pytest.fixture
def fake_account():
    account = MagicMock()
    account.webhooks.return_value = []
    account.subscriptionActive = True
    return account


def test_register_adds_url_when_not_present(fake_account):
    fake_account.webhooks.side_effect = [
        [],
        ["http://host:8080/api/webhooks/plex?token=secret"],
    ]

    with patch("plexapi.myplex.MyPlexAccount", return_value=fake_account):
        result = pwh.register("token", "http://host:8080/api/webhooks/plex", auth_token="secret")

    fake_account.addWebhook.assert_called_once_with("http://host:8080/api/webhooks/plex?token=secret")
    assert "http://host:8080/api/webhooks/plex?token=secret" in result


def test_register_embeds_token_when_auth_provided():
    """The auth token must be appended to the URL Plex stores."""
    fake = MagicMock()
    fake.webhooks.side_effect = [[], ["http://host/api/webhooks/plex?token=abc"]]
    fake.subscriptionActive = True
    with patch("plexapi.myplex.MyPlexAccount", return_value=fake):
        pwh.register("token", "http://host/api/webhooks/plex", auth_token="abc")
    fake.addWebhook.assert_called_once_with("http://host/api/webhooks/plex?token=abc")


def test_register_replaces_stale_url_with_old_token():
    """Re-registering after rotating the secret should remove the old URL and add a fresh one."""
    fake = MagicMock()
    fake.webhooks.side_effect = [
        ["http://host/api/webhooks/plex?token=OLD"],
        ["http://host/api/webhooks/plex?token=NEW"],
    ]
    fake.subscriptionActive = True
    with patch("plexapi.myplex.MyPlexAccount", return_value=fake):
        pwh.register("token", "http://host/api/webhooks/plex", auth_token="NEW")
    fake.deleteWebhook.assert_called_once_with("http://host/api/webhooks/plex?token=OLD")
    fake.addWebhook.assert_called_once_with("http://host/api/webhooks/plex?token=NEW")


def test_register_is_idempotent_when_url_already_present(fake_account):
    fake_account.webhooks.return_value = ["http://host:8080/api/webhooks/plex?token=secret"]
    with patch("plexapi.myplex.MyPlexAccount", return_value=fake_account):
        result = pwh.register("token", "http://host:8080/api/webhooks/plex", auth_token="secret")
    fake_account.addWebhook.assert_not_called()
    assert "http://host:8080/api/webhooks/plex?token=secret" in result


def test_register_strips_trailing_slash():
    fake = MagicMock()
    fake.webhooks.side_effect = [[], ["http://host/api/webhooks/plex?token=t"]]
    fake.subscriptionActive = True
    with patch("plexapi.myplex.MyPlexAccount", return_value=fake):
        pwh.register("token", "http://host/api/webhooks/plex/", auth_token="t")
    fake.addWebhook.assert_called_once_with("http://host/api/webhooks/plex?token=t")


def test_register_missing_token_raises():
    with pytest.raises(pwh.PlexWebhookError) as exc_info:
        pwh.register("", "http://host/api/webhooks/plex")
    assert exc_info.value.reason == "missing_token"


def test_register_missing_url_raises():
    with pytest.raises(pwh.PlexWebhookError) as exc_info:
        pwh.register("token", "")
    assert exc_info.value.reason == "missing_url"


def test_register_no_plex_pass_surfaces_clean_error():
    fake = MagicMock()
    fake.webhooks.side_effect = Exception("401 Unauthorized")
    fake.subscriptionActive = False
    with patch("plexapi.myplex.MyPlexAccount", return_value=fake):
        with pytest.raises(pwh.PlexWebhookError) as exc_info:
            pwh.register("token", "http://host/api/webhooks/plex")
    assert exc_info.value.reason == "plex_pass_required"


def test_unregister_removes_existing_webhook_with_token():
    """unregister should match the base URL even if the registered URL has ?token=…"""
    fake = MagicMock()
    fake.webhooks.side_effect = [["http://host/api/webhooks/plex?token=abc"], []]
    fake.subscriptionActive = True
    with patch("plexapi.myplex.MyPlexAccount", return_value=fake):
        pwh.unregister("token", "http://host/api/webhooks/plex")
    fake.deleteWebhook.assert_called_once_with("http://host/api/webhooks/plex?token=abc")


def test_unregister_url_not_present_is_noop():
    fake = MagicMock()
    fake.webhooks.return_value = []
    fake.subscriptionActive = True
    with patch("plexapi.myplex.MyPlexAccount", return_value=fake):
        pwh.unregister("token", "http://host/api/webhooks/plex")
    fake.deleteWebhook.assert_not_called()


def test_is_registered_returns_true_for_url_with_embedded_token():
    """The base URL should match a registered URL that has ?token=… appended."""
    fake = MagicMock()
    fake.webhooks.return_value = ["http://host/api/webhooks/plex?token=secret"]
    fake.subscriptionActive = True
    with patch("plexapi.myplex.MyPlexAccount", return_value=fake):
        assert pwh.is_registered("token", "http://host/api/webhooks/plex") is True


def test_build_authenticated_url_appends_token():
    url = pwh._build_authenticated_url("http://host/api/webhooks/plex", "abc")
    assert url == "http://host/api/webhooks/plex?token=abc"


def test_build_authenticated_url_replaces_existing_token():
    url = pwh._build_authenticated_url("http://host/api/webhooks/plex?token=OLD", "NEW")
    assert url == "http://host/api/webhooks/plex?token=NEW"


def test_build_authenticated_url_empty_token_returns_base():
    url = pwh._build_authenticated_url("http://host/api/webhooks/plex", "")
    assert url == "http://host/api/webhooks/plex"


def test_is_registered_swallows_errors():
    """is_registered is a UI status probe — must never raise."""
    with patch("plexapi.myplex.MyPlexAccount", side_effect=Exception("network down")):
        assert pwh.is_registered("token", "http://host/api/webhooks/plex") is False


def test_has_plex_pass_true_when_subscription_active():
    fake = MagicMock()
    fake.subscriptionActive = True
    with patch("plexapi.myplex.MyPlexAccount", return_value=fake):
        assert pwh.has_plex_pass("token") is True


def test_has_plex_pass_false_when_no_subscription_and_webhooks_fail():
    fake = MagicMock()
    fake.subscriptionActive = False
    fake.hasPlexPass = False
    fake.webhooks.side_effect = Exception("401")
    with patch("plexapi.myplex.MyPlexAccount", return_value=fake):
        assert pwh.has_plex_pass("token") is False


def test_has_plex_pass_swallows_missing_token():
    assert pwh.has_plex_pass("") is False


def test_list_webhooks_normalizes_urls():
    fake = MagicMock()
    fake.webhooks.return_value = [
        "http://host/api/webhooks/plex/",
        "http://host:8080/other/",
    ]
    fake.subscriptionActive = True
    with patch("plexapi.myplex.MyPlexAccount", return_value=fake):
        urls = pwh.list_webhooks("token")
    assert urls == ["http://host/api/webhooks/plex", "http://host:8080/other"]
