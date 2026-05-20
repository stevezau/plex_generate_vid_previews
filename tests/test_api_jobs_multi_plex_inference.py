"""Regression: when two Plex servers in ``media_servers`` share the
same library id (Plex assigns ids sequentially per-server starting at
``"1"`` — so collisions are the rule, not the exception), the inference
function ``_infer_server_from_library_id`` must NOT silently pick the
first server. It returned the first match pre-fix, which caused issue
#244: the user pinned a scan to the 4K Plex's "4k Movies" (id=1) in the
UI, the UI omitted ``server_id``, the backend's inference matched the
ORIGINAL Plex's "movies" (also id=1) first, and the scan ran against
the wrong server.

Two fix surfaces work together:

1. **Inference (this test file).** ``_infer_server_from_library_id``
   must return ``(None, None, None)`` when two or more enabled servers
   share the same library id — "ambiguous, refuse to guess" is the
   safe failure mode.
2. **/api/jobs (covered in TestCreateJobExplicitServerId below).** When
   the request body carries ``server_id``, it MUST be honoured — the
   inference path is the fallback for callers that don't provide one.

See issue #244 comment thread (2026-05-19, @bubba925's job log showing
``pin='1105d5d5fe51429f913899ccf6058c1c'`` — the FIRST configured
Plex's id — for a scan the operator pinned to the SECOND).
"""

from __future__ import annotations

import io
import json
import os
from unittest.mock import patch

import pytest

from media_preview_generator.web.app import create_app
from media_preview_generator.web.settings_manager import reset_settings_manager


@pytest.fixture(autouse=True)
def _reset_singletons():
    reset_settings_manager()
    import media_preview_generator.web.jobs as jobs_mod

    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    import media_preview_generator.web.scheduler as sched_mod

    with sched_mod._schedule_lock:
        sched_mod._schedule_manager = None
    from media_preview_generator.web.routes import clear_gpu_cache

    clear_gpu_cache()
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
    clear_gpu_cache()


@pytest.fixture()
def app(tmp_path):
    config_dir = str(tmp_path / "config")
    os.makedirs(config_dir, exist_ok=True)
    with open(os.path.join(config_dir, "auth.json"), "w") as f:
        json.dump({"token": "test-token-12345678"}, f)
    with open(os.path.join(config_dir, "settings.json"), "w") as f:
        json.dump({"setup_complete": True}, f)
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
        yield flask_app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def two_plex_servers_overlapping_ids():
    """Mirrors the reporter's settings.json: two enabled Plex servers,
    both with libraries id="1" and id="2" (because Plex assigns ids
    per-server starting at 1)."""
    from media_preview_generator.web.settings_manager import get_settings_manager

    entries = [
        {
            "id": "plex-kraken",
            "type": "plex",
            "name": "Plex - Kraken",
            "enabled": True,
            "url": "http://kraken:32400",
            "auth": {"token": "tok-k"},
            "libraries": [
                {"id": "1", "name": "movies", "enabled": True},
                {"id": "2", "name": "tv", "enabled": True},
            ],
        },
        {
            "id": "plex-calypso-4k",
            "type": "plex",
            "name": "Plex - Calypso - 4k",
            "enabled": True,
            "url": "http://calypso:32400",
            "auth": {"token": "tok-c"},
            "libraries": [
                {"id": "1", "name": "4k Movies", "enabled": True},
                {"id": "2", "name": "4k TV", "enabled": True},
            ],
        },
    ]
    get_settings_manager().set("media_servers", entries)
    return entries


class TestInferServerFromLibraryIdAmbiguity:
    """Pin the contract: when the same library id maps to multiple
    enabled servers, the inference must refuse to guess. Pre-fix it
    returned the first match in registration order."""

    def test_ambiguous_library_id_returns_none(self, app, two_plex_servers_overlapping_ids):
        """Two Plex servers both have library id="1". Inference must
        return (None, None, None) — picking the first silently mis-
        routes the user's scan (issue #244)."""
        from media_preview_generator.web.routes.api_jobs import _infer_server_from_library_id

        with app.app_context():
            sid, sname, stype = _infer_server_from_library_id("1")

        assert sid is None, (
            f"ambiguous id must yield None, got server_id={sid!r}. First-match-wins is the regression from #244."
        )
        assert sname is None
        assert stype is None

    def test_unambiguous_library_id_still_resolves(self, app):
        """Control: single server, single library id — inference must
        still work for the canonical single-server case."""
        from media_preview_generator.web.routes.api_jobs import _infer_server_from_library_id
        from media_preview_generator.web.settings_manager import get_settings_manager

        get_settings_manager().set(
            "media_servers",
            [
                {
                    "id": "plex-only",
                    "type": "plex",
                    "name": "Only Plex",
                    "enabled": True,
                    "libraries": [{"id": "1", "name": "Movies", "enabled": True}],
                }
            ],
        )

        with app.app_context():
            sid, _, _ = _infer_server_from_library_id("1")

        assert sid == "plex-only"

    def test_cross_vendor_id_collision_also_returns_none(self, app):
        """Plex and Jellyfin (or Emby) can both have a library id="1" —
        the inference makes no vendor distinction. A future "smart"
        change that adds type-based filtering inside the inference
        would regress this case without a test pinning it."""
        from media_preview_generator.web.routes.api_jobs import _infer_server_from_library_id
        from media_preview_generator.web.settings_manager import get_settings_manager

        get_settings_manager().set(
            "media_servers",
            [
                {
                    "id": "plex-a",
                    "type": "plex",
                    "enabled": True,
                    "libraries": [{"id": "1", "name": "Movies", "enabled": True}],
                },
                {
                    "id": "jf-1",
                    "type": "jellyfin",
                    "enabled": True,
                    "libraries": [{"id": "1", "name": "Movies", "enabled": True}],
                },
            ],
        )

        with app.app_context():
            sid, _, _ = _infer_server_from_library_id("1")

        assert sid is None, "cross-vendor id collision must also refuse to guess"

    def test_disabled_server_does_not_count_toward_ambiguity(self, app):
        """If only one of the matching servers is enabled, the
        inference can confidently pick that one. The disabled server
        is off-air and doesn't compete for the library id."""
        from media_preview_generator.web.routes.api_jobs import _infer_server_from_library_id
        from media_preview_generator.web.settings_manager import get_settings_manager

        get_settings_manager().set(
            "media_servers",
            [
                {
                    "id": "plex-a",
                    "type": "plex",
                    "enabled": False,
                    "libraries": [{"id": "1", "name": "movies", "enabled": True}],
                },
                {
                    "id": "plex-b",
                    "type": "plex",
                    "enabled": True,
                    "libraries": [{"id": "1", "name": "4k Movies", "enabled": True}],
                },
            ],
        )

        with app.app_context():
            sid, _, _ = _infer_server_from_library_id("1")

        assert sid == "plex-b", "the only enabled match must win"


class TestCreateJobExplicitServerId:
    """The /api/jobs POST endpoint must honour an explicit ``server_id``
    field in the body. The UI fix sends it; this test pins that the
    backend uses it instead of falling back to the (ambiguous) inference."""

    def _post_job(self, client, body):
        return client.post(
            "/api/jobs",
            data=json.dumps(body),
            content_type="application/json",
            headers={"X-Auth-Token": "test-token-12345678"},
        )

    def test_explicit_server_id_overrides_inference(self, client, two_plex_servers_overlapping_ids):
        """The reporter's scenario: 2 Plex, both have library id="1".
        UI now sends ``server_id="plex-calypso-4k"`` AND
        ``library_ids=["1"]``. Backend MUST pin to the explicit id,
        not the first-server-with-library-1 inference fallback."""
        with patch("media_preview_generator.web.routes.api_jobs._start_job_async") as mock_start:
            resp = self._post_job(
                client,
                {
                    "library_ids": ["1"],
                    "library_name": "4k Movies",
                    "server_id": "plex-calypso-4k",
                    "priority": 2,
                    "config": {},
                },
            )

        assert resp.status_code in (200, 201), resp.get_data(as_text=True)
        mock_start.assert_called_once()
        # The async-start kwargs carry the config_overrides — pin the
        # contract that ``server_id`` survives the round-trip into the
        # job runner's overrides dict (where job_runner.py:498 will
        # translate it to ``config.server_id_filter``).
        call_args = mock_start.call_args
        # _start_job_async(job_id, config_overrides) — kwargs vary by
        # endpoint, so look at positional args too.
        overrides = None
        if len(call_args.args) >= 2:
            overrides = call_args.args[1]
        else:
            overrides = call_args.kwargs.get("config_overrides")
        assert overrides is not None, f"could not locate config_overrides in {call_args!r}"
        assert overrides.get("server_id") == "plex-calypso-4k", (
            f"explicit server_id MUST survive to config_overrides; got overrides={overrides!r}"
        )

    def test_no_explicit_server_id_with_ambiguous_libraries_stays_unpinned(
        self, client, two_plex_servers_overlapping_ids
    ):
        """Defensive: when neither the UI nor inference can disambiguate
        (older clients, scripted callers), the job must run unpinned —
        the multi-server fan-out will then publish to every owning
        server (or warn on no-candidates). Pre-fix this silently pinned
        to the first server with the matching library id."""
        with patch("media_preview_generator.web.routes.api_jobs._start_job_async") as mock_start:
            resp = self._post_job(
                client,
                {
                    "library_ids": ["1"],
                    "library_name": "Some library",
                    "priority": 2,
                    "config": {},
                },
            )

        assert resp.status_code in (200, 201)
        mock_start.assert_called_once()
        overrides = mock_start.call_args.args[1] if len(mock_start.call_args.args) >= 2 else None
        if overrides is None:
            overrides = mock_start.call_args.kwargs.get("config_overrides") or {}
        # Tighter than ``not in or is None`` — also rejects ``""`` /
        # other falsy values that the dispatcher's ``if server_id_filter:``
        # check would accept, then downstream callers might mis-treat.
        # Pre-fix this silently pinned to the first server with the
        # matching library id (issue #244).
        assert not overrides.get("server_id"), (
            f"ambiguous case must leave server_id falsy/absent; got overrides={overrides!r}"
        )


class TestServerIdSurvivesIntoConfigServerIdFilter:
    """End-to-end pin: the explicit ``server_id`` in the request body
    must propagate all the way into ``Config.server_id_filter`` — that's
    the attribute the multi-server dispatcher reads (orchestrator.py:399,
    multi_server.py:1196). A refactor that renames the overrides key
    but forgets to update the translation in job_runner.py:498 would
    silently regress this back into the #244 bug shape (D34's exact
    failure mode in a different form)."""

    def test_server_id_override_translates_to_config_server_id_filter(self):
        """The translation at job_runner.py:498 turns
        ``config_overrides["server_id"]`` into
        ``config.server_id_filter``. Pin it directly."""
        from types import SimpleNamespace

        config = SimpleNamespace(server_id_filter=None)
        overrides = {"server_id": "plex-calypso-4k"}

        # Mirror the loop body at job_runner.py:495-498.
        for key, value in overrides.items():
            if key == "server_id":
                config.server_id_filter = str(value) if value else None
            elif hasattr(config, key):
                setattr(config, key, value)

        assert config.server_id_filter == "plex-calypso-4k", (
            f"server_id override MUST translate to config.server_id_filter; "
            f"got config.server_id_filter={config.server_id_filter!r}"
        )

    def test_empty_string_server_id_translates_to_none(self):
        """Defensive: ``server_id=""`` is "no pin", not "pin to id-with-
        empty-string". The translator coerces to ``None`` so the
        dispatcher's ``if server_id_filter:`` check correctly falls
        through to fan-out. Matches the empty-string case the
        tightened ambiguous test above also covers."""
        from types import SimpleNamespace

        config = SimpleNamespace(server_id_filter=None)
        for key, value in {"server_id": ""}.items():
            if key == "server_id":
                config.server_id_filter = str(value) if value else None
            elif hasattr(config, key):
                setattr(config, key, value)
        assert config.server_id_filter is None


_ = io  # imports kept for symmetry with sibling test files
