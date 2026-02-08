"""
Web interface package for Plex Preview Generator.

Provides a Flask-based web GUI with scheduling and token authentication.
"""

from .app import create_app, socketio

__all__ = ["create_app", "socketio"]
