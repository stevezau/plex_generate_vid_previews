"""Legacy import shim — forwards to :mod:`plex_generate_previews.jobs.worker`.

New code should import from :mod:`plex_generate_previews.jobs` directly.
"""

from .jobs.worker import (  # noqa: F401
    Worker,
    WorkerPool,
    clear_job_threads,
    is_job_thread,
    register_job_thread,
    unregister_job_thread,
)
