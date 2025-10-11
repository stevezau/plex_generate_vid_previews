"""
Entry point for python -m plex_generate_previews

Allows running the package as a module:
    python -m plex_generate_previews
"""

from .cli import main

if __name__ == '__main__':
    main()
