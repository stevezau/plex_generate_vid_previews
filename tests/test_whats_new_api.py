"""Tests for the /api/system/whats-new dismiss endpoint.

Two guarantees:

1. The dev_docker / PR-build / local-build images report non-semver
   ``current_version`` strings like ``dev@abc1234`` / ``PR-42`` /
   ``local build``. Writing those into ``last_seen_version`` would
   make every future ``parse_version(last_seen)`` raise — silently
   swallowing every release after that on the next upgrade. The
   endpoint MUST NOT do that.

2. Issue #237: pre-fix, the dismiss endpoint on a non-semver build
   was a silent no-op. The next ``GET /whats-new`` recomputed the
   same unseen list and the modal re-rendered on every Dashboard
   visit. Fix: on a non-semver build, write the MAX parseable
   release version among recent releases instead. Keeps
   ``last_seen_version`` semver-parseable AND lets the read path
   short-circuit on subsequent visits.
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

    # Recent-releases payload used for non-semver dismiss tests. The
    # endpoint walks this list, takes the max parseable version, and
    # writes it to ``last_seen_version`` when the running build is
    # non-semver. Issue #237 fix verification.
    _RELEASES = [
        {"version": "4.0.0", "name": "Multi-Server Support", "date": "2026-05-14"},
        {"version": "3.7.5", "name": "Multi-GPU + Webhook Polish", "date": "2026-04-22"},
        {"version": "3.7.0", "name": "Setup Health revamp", "date": "2026-03-10"},
    ]

    def _post(self, client):
        return client.post(
            "/api/system/whats-new/dismiss",
            headers={"Authorization": "Bearer test-token-12345678"},
        )

    def _patch_version(self, current: str, install_type: str = "docker"):
        return patch(
            "media_preview_generator.web.routes.api_system._get_version_info",
            return_value={"current_version": current, "install_type": install_type},
        )

    def _patch_releases(self, releases):
        return patch(
            "media_preview_generator.web.routes.api_system._fetch_github_releases",
            return_value=releases,
        )

    def test_semver_release_persists(self, client):
        """install_type=docker, current_version="4.0.0" → write back directly.
        Releases endpoint MUST NOT be called on the parseable-semver path
        (proven by the absence of a releases mock — any call would hit
        real network / bundled JSON, but we don't care because the
        parseable branch returns before that call)."""
        with self._patch_version("4.0.0"):
            resp = self._post(client)
        assert resp.status_code == 200
        assert _settings_manager().get("last_seen_version") == "4.0.0"

    def test_dev_docker_persists_max_release_version(self, client):
        """Issue #237: install_type=dev_docker, current_version="dev@a2d0362"
        — the dismiss MUST persist the max parseable release version so
        ``last_seen_version`` stays semver-parseable AND the next
        Dashboard visit sees ``last_seen >= max(releases)`` and
        short-circuits. Pre-fix this branch was a silent no-op and the
        modal re-rendered on every visit."""
        with self._patch_version("dev@a2d0362", "dev_docker"), self._patch_releases(self._RELEASES):
            resp = self._post(client)
        assert resp.status_code == 200
        # Max parseable release among the 3 in the mock list.
        assert _settings_manager().get("last_seen_version") == "4.0.0"

    def test_pr_build_persists_max_release_version(self, client):
        """install_type=pr_build, current_version="PR-42" → same as dev_docker."""
        with self._patch_version("PR-42", "pr_build"), self._patch_releases(self._RELEASES):
            resp = self._post(client)
        assert resp.status_code == 200
        assert _settings_manager().get("last_seen_version") == "4.0.0"

    def test_local_build_persists_max_release_version(self, client):
        """install_type=local_docker, current_version="local build" → same."""
        with self._patch_version("local build", "local_docker"), self._patch_releases(self._RELEASES):
            resp = self._post(client)
        assert resp.status_code == 200
        assert _settings_manager().get("last_seen_version") == "4.0.0"

    def test_dev_build_no_releases_keeps_prior_baseline(self, client):
        """Offline / GitHub-blocked: ``_fetch_github_releases`` returns []
        and no bundled JSON shipped. The endpoint MUST NOT write
        anything — keep the prior baseline rather than writing an
        invalid string. Modal will re-show next visit (acceptable —
        the only alternative is making something up)."""
        with self._patch_version("dev@a2d0362", "dev_docker"), self._patch_releases([]):
            resp = self._post(client)
        assert resp.status_code == 200
        assert _settings_manager().get("last_seen_version") == "3.7.5"

    def test_dev_build_only_unparseable_releases_keeps_prior_baseline(self, client):
        """Pathological release feed (every entry non-semver) → no write.
        Same reasoning as the no-releases case — better to no-op than
        write garbage. Bug shape #8: every cell of the dev-build matrix
        is covered, including this degenerate path."""
        with (
            self._patch_version("dev@a2d0362", "dev_docker"),
            self._patch_releases([{"version": "dev@xyz"}, {"version": ""}, {"version": "not-semver"}]),
        ):
            resp = self._post(client)
        assert resp.status_code == 200
        assert _settings_manager().get("last_seen_version") == "3.7.5"

    def test_dev_build_mixed_parseable_picks_max(self, client):
        """Mix of parseable + unparseable: the unparseable entries must
        be skipped (not crash, not become the chosen value), and the
        max parseable wins. Order-independent.
        """
        releases = [
            {"version": "dev@something"},  # unparseable — skipped
            {"version": "3.5.0"},
            {"version": "PR-42"},  # unparseable — skipped
            {"version": "4.2.0"},  # max
            {"version": "3.7.0"},
        ]
        with self._patch_version("dev@a2d0362", "dev_docker"), self._patch_releases(releases):
            resp = self._post(client)
        assert resp.status_code == 200
        assert _settings_manager().get("last_seen_version") == "4.2.0"

    def test_empty_current_version_is_noop(self, client):
        """No version detected → keep prior baseline, don't crash."""
        with self._patch_version(""):
            resp = self._post(client)
        assert resp.status_code == 200
        assert _settings_manager().get("last_seen_version") == "3.7.5"

    def test_version_unknown_sentinel_does_not_pollute(self, client):
        """current_version="0.0.0" / "0.0.0.dev0" are version-unknown fallbacks —
        they parse cleanly but writing them would make every real release look
        "newer" forever on the next upgrade. Keep the prior baseline AND
        don't fall through to the release-max path (otherwise an unset
        version on a dev image with releases available would still get
        overwritten — wrong)."""
        for sentinel in ("0.0.0", "0.0.0.dev0"):
            # Reset baseline between iterations.
            _settings_manager().update({"last_seen_version": "3.7.5"})
            with self._patch_version(sentinel), self._patch_releases(self._RELEASES):
                resp = self._post(client)
            assert resp.status_code == 200
            assert _settings_manager().get("last_seen_version") == "3.7.5", (
                f"sentinel {sentinel!r} should not be persisted; release-max path also skipped"
            )

    def test_post_release_suffix_persists(self, client):
        """parse_version accepts e.g. "3.7.5.post14" → write back (real release artifact)."""
        with self._patch_version("3.7.5.post14", "source"):
            resp = self._post(client)
        assert resp.status_code == 200
        assert _settings_manager().get("last_seen_version") == "3.7.5.post14"

    def test_dev_build_dismiss_then_get_returns_has_new_false(self, client):
        """End-to-end loop verification: after a dev-build dismiss, the
        SAME conditions that previously made the modal re-render must
        now return has_new=False on the next GET. This is the actual
        user-visible behaviour issue #237 reports."""
        with self._patch_version("dev@a2d0362", "dev_docker"), self._patch_releases(self._RELEASES):
            self._post(client)
            resp = client.get(
                "/api/system/whats-new",
                headers={"Authorization": "Bearer test-token-12345678"},
            )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["has_new"] is False, (
            f"After dismiss on a dev build, the modal must not re-show — got entries: {body.get('entries')}"
        )
        assert body["entries"] == []


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
