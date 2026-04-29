"""
Pytest configuration and shared fixtures for test suite.

Provides common test fixtures including mock configs, Plex responses,
temporary directories, and helper functions for mocking FFmpeg and Plex.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Session-wide: swap APScheduler's SQLAlchemyJobStore for MemoryJobStore.
#
# ScheduleManager.__init__ instantiates SQLAlchemyJobStore(url=...) which
# creates a sqlite DB per test (hot path in the Flask-app suites that
# re-create the app per test). MemoryJobStore is a drop-in for our tests
# because no test exercises cross-restart jobstore persistence — schedule
# metadata is persisted via schedules.json, not the APScheduler jobstore.
#
# We replace the class reference at import time (before any test runs) so
# the swap is in effect for every subsequent import of web.scheduler.
# A small wrapper absorbs the ``url=...`` kwarg that MemoryJobStore
# doesn't accept.
# ---------------------------------------------------------------------------
try:
    from apscheduler.jobstores.memory import MemoryJobStore as _MemoryJobStore

    import media_preview_generator.web.scheduler as _sched_mod

    class _TestMemoryJobStore(_MemoryJobStore):
        def __init__(self, *args, **kwargs):  # noqa: D401
            super().__init__()

    _sched_mod.SQLAlchemyJobStore = _TestMemoryJobStore
except ImportError:  # pragma: no cover
    pass


@pytest.fixture
def fixtures_dir():
    """Return path to fixtures directory."""
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def media_fixture():
    """Return a resolver that maps a content-type key to a real on-disk
    test clip under ``tests/fixtures/media/``.

    Usage:
        def test_hdr10_probe(media_fixture):
            path = media_fixture("hdr10")
            info = MediaInfo.parse(str(path))
            ...

    Available keys (see ``tests/fixtures/media/generate.sh``):
      - ``"sdr"``     — H.264 BT.709 SDR
      - ``"hdr10"``   — HEVC Main10 with HDR10 metadata (BT.2020 + PQ)
      - ``"dv8"``     — DV Profile 8.1 (HDR10 base layer)
      - ``"dv_p5_hdr_format"`` — NOT a file: the ``hdr_format`` string
        that pymediainfo returns for a DV Profile 5 clip, suitable for
        mocking in unit tests that don't need real bytes.

    Fail-loud if the requested key is missing — tests should call
    ``pytest.importorskip`` or mark themselves skipped when the fixture
    file isn't present, rather than silently pass.
    """
    base = Path(__file__).parent / "fixtures" / "media"

    paths = {
        "sdr": base / "sdr_tiny.mkv",
        "hdr10": base / "hdr10_tiny.mkv",
        "dv8": base / "dv_profile8_tiny.mkv",
    }
    sentinels = {
        "dv_p5_hdr_format": "Dolby Vision, Version 1.0, dvhe.05.06, BL+EL+RPU",
    }

    def resolve(key: str):
        if key in sentinels:
            return sentinels[key]
        path = paths.get(key)
        if path is None:
            raise KeyError(f"Unknown media_fixture key: {key!r}. Known: {sorted(list(paths) + list(sentinels))}")
        if not path.exists():
            pytest.skip(f"Media fixture {path} missing — run tests/fixtures/media/generate.sh to rebuild it.")
        return path

    return resolve


@pytest.fixture
def mock_config():
    """Create a mock Config object with sensible defaults."""
    config = MagicMock()
    config.plex_url = "http://localhost:32400"
    config.plex_token = "test_token_12345"
    config.plex_timeout = 60
    config.plex_libraries = []
    config.plex_config_folder = "/config/plex"
    config.plex_local_videos_path_mapping = ""
    config.plex_videos_path_mapping = ""
    config.path_mappings = []
    config.plex_bif_frame_interval = 5
    config.thumbnail_quality = 4
    config.regenerate_thumbnails = False
    config.gpu_threads = 1
    config.cpu_threads = 1
    config.gpu_config = []
    config.tmp_folder = "/tmp/media_preview_generator"
    config.tmp_folder_created_by_us = False
    config.ffmpeg_path = "/usr/bin/ffmpeg"
    config.ffmpeg_threads = 2
    config.tonemap_algorithm = "hable"
    config.log_level = "INFO"
    config.worker_pool_timeout = 30
    # None so get_library_sections filters by plex_libraries (titles), not by ID
    config.plex_library_ids = None
    return config


@pytest.fixture
def temp_dir():
    """Create a temporary directory for tests that is cleaned up afterwards."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def mock_plex_server():
    """Create a mock Plex server with common methods."""
    plex = MagicMock()
    plex.library = MagicMock()
    plex.query = MagicMock()
    return plex


@pytest.fixture
def mock_plex_section_movie():
    """Create a mock Plex movie library section."""
    section = MagicMock()
    section.title = "Movies"
    section.METADATA_TYPE = "movie"
    section.type = "movie"
    return section


@pytest.fixture
def mock_plex_section_episode():
    """Create a mock Plex TV show library section."""
    section = MagicMock()
    section.title = "TV Shows"
    section.METADATA_TYPE = "episode"
    section.type = "show"
    return section


@pytest.fixture
def mock_plex_movie():
    """Create a mock Plex movie item."""
    movie = MagicMock()
    movie.key = "/library/metadata/54321"
    movie.title = "Test Movie"
    movie.locations = ["/data/movies/Test Movie (2024)/Test Movie (2024).mkv"]
    return movie


@pytest.fixture
def mock_plex_episode():
    """Create a mock Plex episode item."""
    episode = MagicMock()
    episode.key = "/library/metadata/12345"
    episode.title = "Pilot"
    episode.grandparentTitle = "Test Show"
    episode.seasonEpisode = "s01e01"
    episode.locations = ["/data/tv/Test Show/Season 01/Test Show - S01E01 - Pilot.mkv"]
    return episode


@pytest.fixture
def sample_jpeg(fixtures_dir):
    """Return path to sample JPEG fixture."""
    return str(fixtures_dir / "sample.jpg")


@pytest.fixture
def reference_bif(fixtures_dir):
    """Return path to reference BIF fixture."""
    return str(fixtures_dir / "reference.bif")


@pytest.fixture
def plex_xml_library_sections(fixtures_dir):
    """Load library sections XML fixture."""
    xml_path = fixtures_dir / "plex_responses" / "library_sections.xml"
    with open(xml_path, encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def plex_xml_episode_tree(fixtures_dir):
    """Load episode tree XML fixture."""
    xml_path = fixtures_dir / "plex_responses" / "episode_tree.xml"
    with open(xml_path, encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def plex_xml_movie_tree(fixtures_dir):
    """Load movie tree XML fixture."""
    xml_path = fixtures_dir / "plex_responses" / "movie_tree.xml"
    with open(xml_path, encoding="utf-8") as f:
        return f.read()


def create_mock_ffmpeg_process(returncode=0, duration=60.0, create_images=True, output_dir=None):
    """
    Helper function to create a mock FFmpeg process for testing.

    Args:
        returncode: Exit code for the process (0 = success)
        duration: Video duration in seconds for progress simulation
        create_images: Whether to create actual image files in output_dir
        output_dir: Directory to create test images in

    Returns:
        MagicMock: Mocked subprocess.Popen object
    """
    mock_proc = MagicMock()
    mock_proc.returncode = returncode

    # Simulate progress by returning None (running) then returncode (done)
    poll_count = [0]

    def mock_poll():
        poll_count[0] += 1
        if poll_count[0] > 2:  # Return done after 2 polls
            return returncode
        return None

    mock_proc.poll = mock_poll

    # If requested, create test images
    if create_images and output_dir and returncode == 0:
        os.makedirs(output_dir, exist_ok=True)
        # Create some test images
        for i in range(1, 4):
            img_path = os.path.join(output_dir, f"img-{i:06d}.jpg")
            with open(img_path, "wb") as f:
                f.write(b"\xff\xd8\xff")  # Minimal JPEG

    return mock_proc


def create_mock_mediainfo(has_hdr=False, hdr_format_override=None, duration=60.0):
    """
    Helper function to create a mock MediaInfo object.

    Args:
        has_hdr: Whether video has HDR format (sets ``"HDR10"`` if True)
        hdr_format_override: Explicit ``hdr_format`` string.  When provided,
            this value is used verbatim and *has_hdr* is ignored.  Useful for
            testing Dolby Vision variants, e.g.
            ``"Dolby Vision, Version 1.0, dvhe.05.06, BL+EL+RPU"``.
        duration: Video duration in seconds

    Returns:
        MagicMock: Mocked MediaInfo object
    """
    mock_info = MagicMock()

    # Video track
    video_track = MagicMock()
    if hdr_format_override is not None:
        video_track.hdr_format = hdr_format_override
    else:
        video_track.hdr_format = "HDR10" if has_hdr else None
    video_track.maximum_content_light_level = None  # MaxCLL — override in tests as needed
    video_track.duration = duration * 1000  # milliseconds
    video_track.width = 1920
    video_track.height = 1080
    video_track.frame_rate = 24.0

    mock_info.video_tracks = [video_track]
    mock_info.audio_tracks = []
    mock_info.general_tracks = []

    return mock_info


@pytest.fixture
def mock_ffmpeg_success():
    """Create a successful FFmpeg process mock."""
    return create_mock_ffmpeg_process(returncode=0)


@pytest.fixture
def mock_ffmpeg_failure():
    """Create a failed FFmpeg process mock."""
    return create_mock_ffmpeg_process(returncode=1)


@pytest.fixture
def mock_mediainfo_standard():
    """Create a mock MediaInfo for standard video."""
    return create_mock_mediainfo(has_hdr=False)


@pytest.fixture
def mock_mediainfo_hdr():
    """Create a mock MediaInfo for HDR video."""
    return create_mock_mediainfo(has_hdr=True)


@pytest.fixture
def mock_mediainfo_dv_profile5():
    """Create a mock MediaInfo for Dolby Vision Profile 5 (no backward compat)."""
    return create_mock_mediainfo(hdr_format_override="Dolby Vision, Version 1.0, dvhe.05.06, BL+EL+RPU")


@pytest.fixture
def mock_mediainfo_dv_with_hdr10():
    """Create a mock MediaInfo for Dolby Vision Profile 8 with HDR10 compat."""
    return create_mock_mediainfo(
        hdr_format_override=("Dolby Vision, Version 1.0, dvhe.08.06, BL+RPU, HDR10 compatible / SMPTE ST 2086")
    )


@pytest.fixture(autouse=True)
def _neutralize_prewarm_caches(request, monkeypatch):
    """Replace ``_prewarm_caches`` with a no-op for every test by default.

    The real function spawns two daemon threads on every ``create_app()``
    call — benign in production, but a major slowdown in the Flask test
    suites that re-create the app per test. Opt back into the real
    implementation by marking a test ``@pytest.mark.real_prewarm``.
    """
    if request.node.get_closest_marker("real_prewarm"):
        return
    try:
        import media_preview_generator.web.app as app_mod
    except ImportError:
        return
    monkeypatch.setattr(app_mod, "_prewarm_caches", lambda: None)


@pytest.fixture(autouse=True)
def _neutralize_setup_logging(request, monkeypatch):
    """Replace ``setup_logging`` with a no-op for every test by default.

    Every ``create_app()`` call runs ``setup_logging()`` which adds three
    loguru handlers (each with ``enqueue=True``, each spawning a background
    writer thread). In a Flask suite that re-creates the app per test
    this accumulates ~800 handler add/removes and flushes at teardown
    racing with pytest's stdout capture. Tests that specifically verify
    logging behaviour can opt back in with ``@pytest.mark.real_logging``.
    """
    if request.node.get_closest_marker("real_logging"):
        return
    try:
        import media_preview_generator.logging_config as lc_mod
        import media_preview_generator.web.app as app_mod
    except ImportError:
        return
    noop = lambda *a, **kw: None  # noqa: E731
    monkeypatch.setattr(lc_mod, "setup_logging", noop)
    monkeypatch.setattr(app_mod, "setup_logging", noop, raising=False)


@pytest.fixture(autouse=True)
def _neutralize_real_world_calls(request, monkeypatch):
    """Stub out the network / hardware calls that ``run_job`` would
    otherwise make on a developer's laptop or on an unsandboxed CI runner.

    With the sync shim below, ``POST /api/jobs`` route handlers run
    ``run_job`` inline. That path calls ``plex_server(config)`` and
    ``_ensure_gpu_cache()`` *before* the mocked ``run_processing``. On a
    developer box with ``PLEX_URL`` set this would connect to the user's
    real Plex library (we observed ``Retrieved 9949 media files from
    library 'Movies'`` in a 34 s test run). On CI it would hang on the
    default ``requests`` timeout until ``pytest-timeout`` killed the
    worker — taking the worker's coverage data with it.

    Tests that want the real call (GPU detection integration, for
    example) can opt out by marking themselves ``real_gpu_detection`` or
    ``real_plex_server`` — neither marker is wired up yet because no
    existing test needs it.
    """
    if request.node.get_closest_marker("real_plex_server") is None:
        try:
            import media_preview_generator.plex_client as pc_mod

            mock_server = MagicMock()
            mock_server.library.sections.return_value = []
            monkeypatch.setattr(pc_mod, "plex_server", lambda *a, **kw: mock_server, raising=True)
        except ImportError:
            pass

    if request.node.get_closest_marker("real_gpu_detection") is None:
        # Replace ``detect_all_gpus`` at its source so every lazy import
        # site (``_ensure_gpu_cache``, ``gpu_detection_extended`` tests)
        # picks up the stub. Patching the cache isn't enough: module-
        # scoped reset fixtures (e.g. ``test_routes.py::_reset_singletons``
        # → ``clear_gpu_cache``) wipe the cache before our value can be
        # read, which sends ``_ensure_gpu_cache`` back to the real ffmpeg
        # subprocess probes.
        try:
            import media_preview_generator.gpu.detect as gd_mod

            monkeypatch.setattr(gd_mod, "detect_all_gpus", lambda *a, **kw: [], raising=True)
        except ImportError:
            pass


@pytest.fixture(autouse=True)
def _sync_start_job_async(request, monkeypatch):
    """Run ``_start_job_async`` synchronously (no daemon thread) by default.

    Route handlers (``POST /api/jobs``, webhook endpoints, settings resume, …)
    normally spawn a daemon ``run_job`` thread via
    ``media_preview_generator.web.routes.job_runner._start_job_async``. That
    thread escapes the test's ``with patch(...)`` scope and keeps running
    after teardown — enumerating the real Plex library (if ``PLEX_URL`` is
    set), probing real GPUs, and flooding stderr with ``I/O operation on
    closed file`` once the per-job loguru handler is removed. On CI the
    surviving threads pile up in a single xdist worker and stall progress
    for ~10 minutes before the last batch drains.

    Replacing ``threading.Thread`` (only inside the ``job_runner`` module)
    with a synchronous shim means ``run_job`` runs to completion *before*
    the route handler returns. Patches on ``run_processing`` /
    ``load_config`` / etc. are still in effect, coverage of ``run_job`` /
    ``worker.py`` is preserved, and no daemon thread survives teardown.

    Tests that genuinely need asynchronous dispatch (e.g.
    ``test_routes.py::TestJobConfigPathMappings`` which asserts that
    ``run_processing`` is invoked on a separate thread within 2 s, or
    ``test_job_dispatcher.py::TestInflightJobGuard`` which spies on
    ``threading.Thread`` directly) opt out with
    ``@pytest.mark.real_job_async``.
    """
    if request.node.get_closest_marker("real_job_async"):
        return
    try:
        import media_preview_generator.web.routes.job_runner as jr_mod
    except ImportError:
        return

    import threading as _real_threading

    class _SyncThread:
        """Drop-in for ``threading.Thread`` that runs ``target`` inline."""

        def __init__(self, *args, target=None, **kwargs):
            self._target = target
            self._args = kwargs.get("args", ())
            self._kwargs = kwargs.get("kwargs", {})
            self.daemon = kwargs.get("daemon", False)
            self.name = kwargs.get("name", "SyncThread")

        def start(self):
            if self._target is not None:
                self._target(*self._args, **self._kwargs)

        def is_alive(self):
            return False

        def join(self, timeout=None):
            return

    # Shim: only the ``Thread`` attribute is overridden; everything else
    # (Lock, Event, current_thread, …) delegates to the real threading
    # module so worker-pool code keeps working.
    class _ThreadingShim:
        Thread = _SyncThread

        def __getattr__(self, name):
            return getattr(_real_threading, name)

    monkeypatch.setattr(jr_mod, "threading", _ThreadingShim(), raising=True)


# Export helper functions so tests can use them
__all__ = [
    "create_mock_ffmpeg_process",
    "create_mock_mediainfo",
]
