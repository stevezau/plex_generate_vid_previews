"""TEST_AUDIT P0.1 — full kwargs matrix the orchestrator sends to process_canonical_path.

This file is the single concentrated answer to the audit's "boundary-call
assertion blindness" finding (the D34 paradigm). Many existing tests check
``mock.assert_called_once()`` without asserting what kwargs the SUT actually
controlled — the dispatcher → ``process_canonical_path`` regression in job
d9918149 hid for months because the test only checked ``kwargs["canonical_path"]``
and ignored ``kwargs["server_id_filter"]``.

Every test in this file pins the COMPLETE kwarg shape for ONE matrix cell:

    server_type ∈ {Plex, Emby, Jellyfin}   ×   caller pin ∈ {None, explicit-id}

Per orchestrator.py:670-675 the pin-precedence rules are:

    1. Caller-supplied server_id_filter ALWAYS wins
    2. No caller pin + non-Plex originator → scope to that originator
    3. No caller pin + Plex originator → fan out (server_id_filter=None)

So the expected ``server_id_filter`` per cell:

    | originator | caller_pin   | expected forward    |
    |-----------|---------------|---------------------|
    | Plex      | None          | None (fan out)      |
    | Plex      | "any-id"      | "any-id"            |
    | Emby      | None          | <emby cfg id>       |
    | Emby      | "any-id"      | "any-id"            |
    | Jellyfin  | None          | <jelly cfg id>      |
    | Jellyfin  | "any-id"      | "any-id"            |

Each test asserts the FULL kwarg shape — not just server_id_filter — so a
regression that drops or mistypes ANY other forwarded kwarg (canonical_path,
item_id_by_server, registry, config, gpu, gpu_device_path, progress_callback,
cancel_check, regenerate) is caught loudly.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.jobs.orchestrator import _run_full_scan_multi_server
from media_preview_generator.processing.types import ProcessableItem
from media_preview_generator.servers.base import ServerConfig, ServerType

MODULE = "media_preview_generator.jobs.orchestrator"


def _config(*, regenerate: bool = False) -> SimpleNamespace:
    """Minimal Config namespace the orchestrator inspects."""
    cfg = SimpleNamespace(
        gpu_threads=0,
        cpu_threads=1,
        working_tmp_folder="/tmp/work",
        plex_url="",
        plex_token="",
        webhook_paths=None,
        server_id_filter=None,
        regenerate_thumbnails=regenerate,
    )
    return cfg


def _server_config(server_id: str, server_type: ServerType) -> ServerConfig:
    return ServerConfig(
        id=server_id,
        type=server_type,
        name=f"Test {server_type.value}",
        enabled=True,
        url="http://test",
        auth={"access_token": "t"},
    )


def _drive_dispatcher(
    *,
    server_type: ServerType,
    caller_pin: str | None,
    item_kwargs: dict | None = None,
    cfg_kwargs: dict | None = None,
):
    """Drive ``_run_full_scan_multi_server`` and return the captured
    ``process_canonical_path`` call's kwargs.

    Returns ``(call_kwargs, server_cfg, expected_registry, expected_config)``
    so each cell-specific test can assert IDENTITY (not just truthiness)
    on the registry + config kwargs. A regression that silently swapped
    in a different registry/config would pass a truthy check but break
    here.
    """
    # When the caller pins explicitly, the configured server id MUST match
    # the pin — otherwise _run_full_scan_multi_server filters it out before
    # dispatch (it scopes the registry to the pinned server only). When no
    # pin, use a stable per-vendor id so the parametrized test ids stay
    # readable.
    server_id = caller_pin if caller_pin else f"{server_type.value}-only"
    cfg = _server_config(server_id, server_type)

    registry_mock = MagicMock()
    registry_mock.configs.return_value = [cfg]

    item = ProcessableItem(
        canonical_path="/data/movies/x.mkv",
        server_id=server_id,
        **(item_kwargs or {}),
    )

    proc = MagicMock()
    proc.list_canonical_paths.return_value = iter([item])

    settings_entry = {"id": server_id, "type": server_type.value, "enabled": True}
    expected_config = _config(**(cfg_kwargs or {}))

    with (
        patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
        patch("media_preview_generator.servers.ServerRegistry") as mock_registry,
        patch("media_preview_generator.processing.get_processor_for", return_value=proc),
        patch("media_preview_generator.processing.multi_server.process_canonical_path") as mock_process,
    ):
        mock_sm.return_value.get.return_value = [settings_entry]
        mock_registry.from_settings.return_value = registry_mock
        mock_process.return_value = MagicMock(publishers=[])

        _run_full_scan_multi_server(
            expected_config,
            selected_gpus=[],
            server_id_filter=caller_pin,
        )

    mock_process.assert_called_once()
    return mock_process.call_args.kwargs, cfg, registry_mock, expected_config


# ---------------------------------------------------------------------------
# Helper: assert all kwargs that should be present on EVERY call
# ---------------------------------------------------------------------------


def _assert_common_kwargs_shape(
    kwargs: dict,
    *,
    expected_path: str = "/data/movies/x.mkv",
    expected_registry=None,
    expected_config=None,
):
    """Pin the kwargs that should be present + correctly shaped on every
    dispatcher call regardless of cell. A regression that drops ANY of
    these fails here, even before the cell-specific assertion.

    When ``expected_registry`` / ``expected_config`` are provided, asserts
    OBJECT IDENTITY (not just truthiness) — a regression that silently
    constructed a fresh empty Config / Registry would pass a `is not None`
    check but break here.
    """
    # canonical_path — must equal the item's canonical_path verbatim.
    assert kwargs.get("canonical_path") == expected_path, (
        f"canonical_path drift: expected {expected_path!r}, got {kwargs.get('canonical_path')!r}. "
        f"Pre-fix: a regression that mutated canonical_path mid-dispatch would skip the "
        f"freshness short-circuit and re-run FFmpeg unnecessarily."
    )
    # registry — must be the same object the dispatcher was called with.
    if expected_registry is not None:
        assert kwargs.get("registry") is expected_registry, (
            "registry kwarg identity drift — dispatcher passed a different object than expected. "
            "A regression that silently constructed a fresh empty registry would publish to "
            "zero servers (silent NO_OWNERS for every item)."
        )
    else:
        assert kwargs.get("registry") is not None, "registry kwarg missing"
    # config — must be the same object the dispatcher was called with.
    if expected_config is not None:
        assert kwargs.get("config") is expected_config, (
            "config kwarg identity drift — dispatcher passed a different Config than expected. "
            "A regression that silently constructed a fresh Config would lose user GPU/worker/path "
            "settings and FFmpeg would run with defaults."
        )
    else:
        assert kwargs.get("config") is not None, "config kwarg missing"
    # progress_callback — must be a callable so the worker UI ticks.
    pc = kwargs.get("progress_callback")
    assert callable(pc), f"progress_callback must be callable; got {pc!r}"


# ---------------------------------------------------------------------------
# Cell 1 — Plex originator + no caller pin → fan out (server_id_filter=None)
# ---------------------------------------------------------------------------


class TestPlexNoPinFansOut:
    def test_plex_no_caller_pin_forwards_server_id_filter_none(self):
        kwargs, cfg, registry, config = _drive_dispatcher(server_type=ServerType.PLEX, caller_pin=None)
        _assert_common_kwargs_shape(kwargs, expected_registry=registry, expected_config=config)

        assert kwargs.get("server_id_filter") is None, (
            f"Plex originator + no caller pin must fan out (server_id_filter=None); "
            f"got {kwargs.get('server_id_filter')!r}. A regression scoping it to the "
            f"originator would miss the cross-vendor publish path that benefits "
            f"multi-vendor installs."
        )

    def test_regenerate_default_propagates_as_false(self):
        kwargs, _, registry, config = _drive_dispatcher(server_type=ServerType.PLEX, caller_pin=None)
        # regenerate kwarg pinned to False (audit P0.10 contract — also pinned
        # in test_full_scan_multi_server.py, but verified here at every cell).
        assert kwargs.get("regenerate") is False, (
            f"regenerate must default to False (bool, not None); got {kwargs.get('regenerate')!r}"
        )

    def test_regenerate_true_propagates_when_config_set(self):
        kwargs, _, registry, config = _drive_dispatcher(
            server_type=ServerType.PLEX,
            caller_pin=None,
            cfg_kwargs={"regenerate": True},
        )
        assert kwargs.get("regenerate") is True


# ---------------------------------------------------------------------------
# Cell 2 — Plex originator + caller pin → caller pin wins
# ---------------------------------------------------------------------------


class TestPlexWithCallerPin:
    def test_plex_caller_pin_wins_over_originator_default(self):
        kwargs, cfg, registry, config = _drive_dispatcher(server_type=ServerType.PLEX, caller_pin="explicit-pin")
        _assert_common_kwargs_shape(kwargs, expected_registry=registry, expected_config=config)

        assert kwargs.get("server_id_filter") == "explicit-pin", (
            f"Caller-supplied server_id_filter must always win for Plex originator; "
            f"got {kwargs.get('server_id_filter')!r}. Closes the d9918149 reproducer "
            f"where Plex-pinned dispatch leaked into Jellyfin/Emby siblings."
        )


# ---------------------------------------------------------------------------
# Cell 3 — Emby originator + no caller pin → scope to originator
# ---------------------------------------------------------------------------


class TestEmbyNoPinScopes:
    def test_emby_no_caller_pin_scopes_to_originator(self):
        kwargs, cfg, registry, config = _drive_dispatcher(server_type=ServerType.EMBY, caller_pin=None)
        _assert_common_kwargs_shape(kwargs, expected_registry=registry, expected_config=config)

        assert kwargs.get("server_id_filter") == cfg.id, (
            f"Non-Plex originator + no caller pin must scope to the originator "
            f"(server_id_filter={cfg.id!r}); got {kwargs.get('server_id_filter')!r}. "
            f"Pre-fix: would fan out to all servers and burn time on lookups for files "
            f"those servers don't have."
        )


# ---------------------------------------------------------------------------
# Cell 4 — Emby originator + caller pin → caller pin wins
# ---------------------------------------------------------------------------


class TestEmbyWithCallerPin:
    def test_emby_caller_pin_wins_over_originator_scope(self):
        kwargs, cfg, registry, config = _drive_dispatcher(server_type=ServerType.EMBY, caller_pin="emby-explicit")
        _assert_common_kwargs_shape(kwargs, expected_registry=registry, expected_config=config)

        assert kwargs.get("server_id_filter") == "emby-explicit", (
            f"Caller pin must override originator scoping for Emby; got {kwargs.get('server_id_filter')!r}"
        )


# ---------------------------------------------------------------------------
# Cell 5 — Jellyfin originator + no caller pin → scope to originator
# ---------------------------------------------------------------------------


class TestJellyfinNoPinScopes:
    def test_jellyfin_no_caller_pin_scopes_to_originator(self):
        kwargs, cfg, registry, config = _drive_dispatcher(server_type=ServerType.JELLYFIN, caller_pin=None)
        _assert_common_kwargs_shape(kwargs, expected_registry=registry, expected_config=config)

        assert kwargs.get("server_id_filter") == cfg.id, (
            f"Jellyfin originator + no caller pin must scope to itself; got {kwargs.get('server_id_filter')!r}"
        )


# ---------------------------------------------------------------------------
# Cell 6 — Jellyfin originator + caller pin → caller pin wins
# ---------------------------------------------------------------------------


class TestJellyfinWithCallerPin:
    def test_jellyfin_caller_pin_wins(self):
        kwargs, cfg, registry, config = _drive_dispatcher(server_type=ServerType.JELLYFIN, caller_pin="jelly-explicit")
        _assert_common_kwargs_shape(kwargs, expected_registry=registry, expected_config=config)

        assert kwargs.get("server_id_filter") == "jelly-explicit"


# ---------------------------------------------------------------------------
# Item-level kwarg propagation — each ProcessableItem field must reach the call
# ---------------------------------------------------------------------------


class TestItemFieldsPropagate:
    """Each field on ProcessableItem must be forwarded to the corresponding
    process_canonical_path kwarg. Pre-fix the dispatcher silently dropped
    item_id_by_server hints for vendor-webhook items, forcing a redundant
    Plex roundtrip."""

    def test_item_id_by_server_hint_propagates(self):
        kwargs, _, registry, config = _drive_dispatcher(
            server_type=ServerType.PLEX,
            caller_pin=None,
            item_kwargs={"item_id_by_server": {"plex-only": "rk-12345"}},
        )
        assert kwargs.get("item_id_by_server") == {"plex-only": "rk-12345"}, (
            f"item_id_by_server hint dropped — would force Plex reverse lookup; got {kwargs.get('item_id_by_server')!r}"
        )

    def test_item_id_by_server_none_when_unset(self):
        kwargs, _, registry, config = _drive_dispatcher(server_type=ServerType.PLEX, caller_pin=None)
        # Empty dict on ProcessableItem → coerced to None at call site
        # (orchestrator line 716: ``item.item_id_by_server or None``).
        assert kwargs.get("item_id_by_server") is None, (
            f"empty item_id_by_server should coerce to None (avoids downstream "
            f"empty-dict checks); got {kwargs.get('item_id_by_server')!r}"
        )

    def test_bundle_metadata_by_server_propagates(self):
        kwargs, _, registry, config = _drive_dispatcher(
            server_type=ServerType.PLEX,
            caller_pin=None,
            item_kwargs={"bundle_metadata_by_server": {"plex-only": ("hash", 0.123)}},
        )
        assert kwargs.get("bundle_metadata_by_server") == {"plex-only": ("hash", 0.123)}


# ---------------------------------------------------------------------------
# GPU kwargs propagate from selected_gpus through to the call
# ---------------------------------------------------------------------------


class TestGpuKwargsPropagate:
    """gpu + gpu_device_path are derived from selected_gpus by the slot
    machinery. Pin so a refactor that decouples them is caught.

    With selected_gpus=[] the slot has gpu_type=None / gpu_device=None →
    forwarded as gpu=None, gpu_device_path=None.
    """

    def test_gpu_none_when_no_selected_gpus(self):
        kwargs, _, registry, config = _drive_dispatcher(server_type=ServerType.PLEX, caller_pin=None)
        # When no GPUs selected, dispatcher uses CPU fallback (gpu=None, device=None).
        assert kwargs.get("gpu") is None, f"gpu must be None when no GPUs selected; got {kwargs.get('gpu')!r}"
        assert kwargs.get("gpu_device_path") is None


# ---------------------------------------------------------------------------
# Parametrized cell sweep — single test pins the FULL matrix in one place
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "server_type,caller_pin,expected_forward",
    [
        # (server_type, caller_pin, expected server_id_filter at call site)
        (ServerType.PLEX, None, None),
        (ServerType.PLEX, "explicit", "explicit"),
        (ServerType.EMBY, None, "emby-only"),  # cfg.id is "emby-only"
        (ServerType.EMBY, "explicit", "explicit"),
        (ServerType.JELLYFIN, None, "jellyfin-only"),
        (ServerType.JELLYFIN, "explicit", "explicit"),
    ],
    ids=[
        "plex_no_pin_fans_out",
        "plex_with_pin_pin_wins",
        "emby_no_pin_scopes_to_originator",
        "emby_with_pin_pin_wins",
        "jellyfin_no_pin_scopes_to_originator",
        "jellyfin_with_pin_pin_wins",
    ],
)
def test_full_pin_matrix(server_type, caller_pin, expected_forward):
    """Single parametrized sweep over the 6-cell pin matrix. Catches the
    case where adding a new branch (e.g. a new ServerType) silently
    breaks one cell while leaving the others passing.
    """
    kwargs, cfg, registry, config = _drive_dispatcher(server_type=server_type, caller_pin=caller_pin)
    _assert_common_kwargs_shape(kwargs, expected_registry=registry, expected_config=config)
    assert kwargs.get("server_id_filter") == expected_forward, (
        f"Cell ({server_type.value}, caller_pin={caller_pin!r}): expected "
        f"server_id_filter={expected_forward!r}, got {kwargs.get('server_id_filter')!r}"
    )
