"""WSGI entry point for gunicorn.

Usage (production — via wrapper.sh):
    gunicorn \\
        --bind 0.0.0.0:8080 \\
        --worker-class gthread \\
        --workers 1 \\
        "media_preview_generator.web.wsgi:app"

Usage (development):
    python -m media_preview_generator.web.app
"""

from .app import create_app

app = create_app()
