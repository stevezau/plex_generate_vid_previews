"""Job lifecycle — orchestration, dispatch, and per-item workers.

Sub-modules:

* :mod:`.orchestrator` — :func:`run_processing` kicks off a library or
  webhook scan against any configured server (Plex, Emby, Jellyfin),
  resolves items, and hands them to the dispatcher.
* :mod:`.dispatcher`   — :class:`JobDispatcher` + :class:`JobTracker`
  drive the persistent dispatch loop and priority scheduling across
  multiple concurrent jobs.
* :mod:`.worker`       — :class:`Worker` / :class:`WorkerPool` own
  per-GPU / per-CPU threads and dispatch each :class:`ProcessableItem`
  to :func:`processing.multi_server.process_canonical_path`.
"""

from .dispatcher import (
    JobDispatcher,
    JobTracker,
    get_dispatcher,
    reset_dispatcher,
)
from .orchestrator import run_processing
from .worker import (
    Worker,
    WorkerPool,
    clear_job_threads,
    is_job_thread,
    register_job_thread,
    unregister_job_thread,
)

__all__ = [
    "JobDispatcher",
    "JobTracker",
    "Worker",
    "WorkerPool",
    "clear_job_threads",
    "get_dispatcher",
    "is_job_thread",
    "register_job_thread",
    "reset_dispatcher",
    "run_processing",
    "unregister_job_thread",
]
