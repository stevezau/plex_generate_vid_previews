"""
WSGI entry point for gunicorn.

Usage (production — via wrapper.sh):
    gunicorn \\
        --bind 0.0.0.0:8080 \\
        --worker-class gthread \\
        --threads 4 \\
        --workers 1 \\
        "plex_generate_previews.web.wsgi:app"

Usage (development):
    python -m plex_generate_previews.web.app
"""

from .app import create_app

app = create_app()
