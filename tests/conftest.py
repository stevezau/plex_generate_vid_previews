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
    import plex_generate_previews.web.scheduler as _sched_mod
    from apscheduler.jobstores.memory import MemoryJobStore as _MemoryJobStore

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
    config.tmp_folder = "/tmp/plex_generate_previews"
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
    with open(xml_path, "r", encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def plex_xml_episode_tree(fixtures_dir):
    """Load episode tree XML fixture."""
    xml_path = fixtures_dir / "plex_responses" / "episode_tree.xml"
    with open(xml_path, "r", encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def plex_xml_movie_tree(fixtures_dir):
    """Load movie tree XML fixture."""
    xml_path = fixtures_dir / "plex_responses" / "movie_tree.xml"
    with open(xml_path, "r", encoding="utf-8") as f:
        return f.read()


def create_mock_ffmpeg_process(
    returncode=0, duration=60.0, create_images=True, output_dir=None
):
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
    video_track.maximum_content_light_level = (
        None  # MaxCLL — override in tests as needed
    )
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
    return create_mock_mediainfo(
        hdr_format_override="Dolby Vision, Version 1.0, dvhe.05.06, BL+EL+RPU"
    )


@pytest.fixture
def mock_mediainfo_dv_with_hdr10():
    """Create a mock MediaInfo for Dolby Vision Profile 8 with HDR10 compat."""
    return create_mock_mediainfo(
        hdr_format_override=(
            "Dolby Vision, Version 1.0, dvhe.08.06, BL+RPU, "
            "HDR10 compatible / SMPTE ST 2086"
        )
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
        import plex_generate_previews.web.app as app_mod
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
        import plex_generate_previews.logging_config as lc_mod
        import plex_generate_previews.web.app as app_mod
    except ImportError:
        return
    noop = lambda *a, **kw: None  # noqa: E731
    monkeypatch.setattr(lc_mod, "setup_logging", noop)
    monkeypatch.setattr(app_mod, "setup_logging", noop, raising=False)


# Export helper functions so tests can use them
__all__ = [
    "create_mock_ffmpeg_process",
    "create_mock_mediainfo",
]
