"""Legacy import shim — forwards to :mod:`plex_generate_previews.jobs.dispatcher`.

New code should import from :mod:`plex_generate_previews.jobs` directly.
"""

from .jobs.dispatcher import (  # noqa: F401
    JobDispatcher,
    JobTracker,
    get_dispatcher,
    reset_dispatcher,
)
