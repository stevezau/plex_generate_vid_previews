"""Legacy import shim — forwards to :mod:`plex_generate_previews.jobs.orchestrator`.

New code should import from :mod:`plex_generate_previews.jobs` directly.
"""

from .jobs.orchestrator import run_processing  # noqa: F401
