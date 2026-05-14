"""Tests for the /api/system/whats-new dismiss endpoint.

Specifically guards the dev_docker / PR-build pollution bug: dismissing
the modal while running a non-semver image (``dev@abc1234``, ``PR-42``,
``local build``) used to write that string into ``last_seen_version``,
which then made every future ``parse_version(last_seen)`` raise — silently
swallowing every release after that on the next upgrade.
"""

import json
import os
from unittest.mock import patch

import pytest

from media_preview_generator.web.app import create_app


@pytest.fixture()
def app_with_config(tmp_path):
    config_dir = str(tmp_path / "config")
    os.makedirs(config_dir, exist_ok=True)
    with open(os.path.join(config_dir, "auth.json"), "w") as fh:
        json.dump({"token": "test-token-12345678"}, fh)
    with open(os.path.join(config_dir, "settings.json"), "w") as fh:
        json.dump({"setup_complete": True, "last_seen_version": "3.7.5"}, fh)

    with patch.dict(
        os.environ,
        {
            "CONFIG_DIR": config_dir,
            "WEB_AUTH_TOKEN": "test-token-12345678",
            "WEB_PORT": "8099",
        },
    ):
        flask_app = create_app(config_dir=config_dir)
        flask_app.config["TESTING"] = True
        flask_app.config["WTF_CSRF_ENABLED"] = False
        yield flask_app, config_dir


@pytest.fixture()
def client(app_with_config):
    flask_app, _ = app_with_config
    return flask_app.test_client()


def _settings_manager():
    from media_preview_generator.web.settings_manager import get_settings_manager

    return get_settings_manager()


class TestDismissWhatsNewWriteback:
    """Each cell of the version-string matrix produces different writeback behaviour."""

    def test_semver_release_persists(self, client):
        """install_type=docker, current_version="4.0.0" → write back."""
        with patch(
            "media_preview_generator.web.routes.api_system._get_version_info",
            return_value={"current_version": "4.0.0", "install_type": "docker"},
        ):
            resp = client.post(
                "/api/system/whats-new/dismiss",
                headers={"Authorization": "Bearer test-token-12345678"},
            )
        assert resp.status_code == 200
        assert _settings_manager().get("last_seen_version") == "4.0.0"

    def test_dev_docker_does_not_pollute(self, client):
        """install_type=dev_docker, current_version="dev@abc1234" → keep prior baseline."""
        with patch(
            "media_preview_generator.web.routes.api_system._get_version_info",
            return_value={"current_version": "dev@abc1234", "install_type": "dev_docker"},
        ):
            resp = client.post(
                "/api/system/whats-new/dismiss",
                headers={"Authorization": "Bearer test-token-12345678"},
            )
        assert resp.status_code == 200
        assert _settings_manager().get("last_seen_version") == "3.7.5"

    def test_pr_build_does_not_pollute(self, client):
        """install_type=pr_build, current_version="PR-42" → keep prior baseline."""
        with patch(
            "media_preview_generator.web.routes.api_system._get_version_info",
            return_value={"current_version": "PR-42", "install_type": "pr_build"},
        ):
            resp = client.post(
                "/api/system/whats-new/dismiss",
                headers={"Authorization": "Bearer test-token-12345678"},
            )
        assert resp.status_code == 200
        assert _settings_manager().get("last_seen_version") == "3.7.5"

    def test_local_build_does_not_pollute(self, client):
        """install_type=local_docker, current_version="local build" → keep prior baseline."""
        with patch(
            "media_preview_generator.web.routes.api_system._get_version_info",
            return_value={"current_version": "local build", "install_type": "local_docker"},
        ):
            resp = client.post(
                "/api/system/whats-new/dismiss",
                headers={"Authorization": "Bearer test-token-12345678"},
            )
        assert resp.status_code == 200
        assert _settings_manager().get("last_seen_version") == "3.7.5"

    def test_empty_current_version_is_noop(self, client):
        """No version detected → keep prior baseline, don't crash."""
        with patch(
            "media_preview_generator.web.routes.api_system._get_version_info",
            return_value={"current_version": "", "install_type": "source"},
        ):
            resp = client.post(
                "/api/system/whats-new/dismiss",
                headers={"Authorization": "Bearer test-token-12345678"},
            )
        assert resp.status_code == 200
        assert _settings_manager().get("last_seen_version") == "3.7.5"

    def test_version_unknown_sentinel_does_not_pollute(self, client):
        """current_version="0.0.0" / "0.0.0.dev0" are version-unknown fallbacks —
        they parse cleanly but writing them would make every real release look
        "newer" forever on the next upgrade. Keep the prior baseline."""
        for sentinel in ("0.0.0", "0.0.0.dev0"):
            with patch(
                "media_preview_generator.web.routes.api_system._get_version_info",
                return_value={"current_version": sentinel, "install_type": "source"},
            ):
                resp = client.post(
                    "/api/system/whats-new/dismiss",
                    headers={"Authorization": "Bearer test-token-12345678"},
                )
            assert resp.status_code == 200
            assert _settings_manager().get("last_seen_version") == "3.7.5", (
                f"sentinel {sentinel!r} should not be persisted"
            )

    def test_post_release_suffix_persists(self, client):
        """parse_version accepts e.g. "3.7.5.post14" → write back (real release artifact)."""
        with patch(
            "media_preview_generator.web.routes.api_system._get_version_info",
            return_value={"current_version": "3.7.5.post14", "install_type": "source"},
        ):
            resp = client.post(
                "/api/system/whats-new/dismiss",
                headers={"Authorization": "Bearer test-token-12345678"},
            )
        assert resp.status_code == 200
        assert _settings_manager().get("last_seen_version") == "3.7.5.post14"


class TestWhatsNewReadPathPoisonedLastSeen:
    """The dismiss endpoint guards against writing non-semver values, but
    older builds (pre-fix) and direct settings.json edits could leave a
    poisoned ``last_seen_version`` like ``dev@SHA`` already in place.

    On the read path, ``parse_version("dev@SHA")`` raises ``ValueError``,
    which the wrapping ``except ValueError: continue`` silently swallows —
    the user upgrading from dev → release never sees the modal. Treat any
    unparseable baseline as the "very old" sentinel so all known releases
    qualify as unseen.
    """

    _RELEASES = [
        {"version": "4.0.0", "name": "Multi-Server Support", "date": "2026-05-14", "body": "headline"},
        {"version": "3.7.5", "name": "Multi-GPU + Webhook Polish", "date": "2026-04-22", "body": "older"},
    ]

    def _get(self, client, current_version: str, last_seen: str):
        _settings_manager().update({"last_seen_version": last_seen})
        with (
            patch(
                "media_preview_generator.web.routes.api_system._get_version_info",
                return_value={"current_version": current_version, "install_type": "docker"},
            ),
            patch(
                "media_preview_generator.web.routes.api_system._fetch_github_releases",
                return_value=self._RELEASES,
            ),
        ):
            return client.get(
                "/api/system/whats-new",
                headers={"Authorization": "Bearer test-token-12345678"},
            )

    def test_dev_sha_baseline_shows_releases(self, client):
        """last_seen="dev@abc1234" should treat as "very old" → all releases unseen."""
        resp = self._get(client, current_version="4.0.0", last_seen="dev@abc1234")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["has_new"] is True
        assert [e["version"] for e in body["entries"]] == ["4.0.0", "3.7.5"]

    def test_pr_build_baseline_shows_releases(self, client):
        """last_seen="PR-42" should treat as "very old" → all releases unseen."""
        resp = self._get(client, current_version="4.0.0", last_seen="PR-42")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["has_new"] is True
        assert "4.0.0" in [e["version"] for e in body["entries"]]

    def test_local_build_baseline_shows_releases(self, client):
        """last_seen="local build" should treat as "very old" → all releases unseen."""
        resp = self._get(client, current_version="4.0.0", last_seen="local build")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["has_new"] is True
        assert "4.0.0" in [e["version"] for e in body["entries"]]

    def test_semver_baseline_still_filters_correctly(self, client):
        """Sanity check: valid semver baseline still filters releases > baseline only."""
        resp = self._get(client, current_version="4.0.0", last_seen="3.7.5")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["has_new"] is True
        assert [e["version"] for e in body["entries"]] == ["4.0.0"]

    def test_baseline_equals_current_returns_empty(self, client):
        """Sanity check: nothing new when last_seen == current."""
        resp = self._get(client, current_version="4.0.0", last_seen="4.0.0")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["has_new"] is False
        assert body["entries"] == []
