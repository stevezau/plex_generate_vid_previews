"""Routes package for the web interface.

Splits route handlers into domain-specific modules while maintaining a
single public API. All previously-importable names remain accessible
from ``plex_generate_previews.web.routes``.
"""

from flask import Blueprint

# Create blueprints (must be defined before sub-modules import them)
main = Blueprint("main", __name__)
api = Blueprint("api", __name__, url_prefix="/api")

# Import sub-modules to register their route decorators with the blueprints.
# Order doesn't matter; each module imports `main` or `api` from this package.
from . import api_bif  # noqa: E402, F401
from . import api_detection  # noqa: E402, F401
from . import api_jobs  # noqa: E402, F401
from . import api_plex  # noqa: E402, F401
from . import api_schedules  # noqa: E402, F401
from . import api_settings  # noqa: E402, F401
from . import api_system  # noqa: E402, F401
from . import pages  # noqa: E402, F401

# Re-export names used by other modules (app.py, webhooks.py, tests)
from ._helpers import (  # noqa: E402, F401
    MEDIA_ROOT,
    PLEX_DATA_ROOT,
    _is_within_base,
    _param_to_bool,
    _safe_resolve_within,
    clear_gpu_cache,
    limiter,
)
from .api_system import _fetch_libraries_via_http, clear_library_cache  # noqa: E402, F401
from .job_runner import _start_job_async  # noqa: E402, F401
from .socketio_handlers import register_socketio_handlers  # noqa: E402, F401
