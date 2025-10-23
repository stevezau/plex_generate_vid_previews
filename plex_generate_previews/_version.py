"""Version file - managed by setuptools-scm.

This file serves as a placeholder for development and is overwritten during builds.

How versioning works:
- During package build (pip install, python -m build), setuptools-scm reads Git tags
  and overwrites this file with the actual version
- For Docker builds, the version is passed via SETUPTOOLS_SCM_PRETEND_VERSION build arg
- The hardcoded values below are fallbacks for development when running from source

Version derivation:
- Tagged commit (e.g., v2.3.0) → version is "2.3.0"
- Commits after tag → dev version like "2.3.1.dev5+g1234abc"
- No tags → fallback_version from pyproject.toml
"""

from typing import Tuple

# Placeholder values - overwritten by setuptools-scm during package build
# Matches fallback_version in pyproject.toml
__version__ = "0.0.0+unknown"
__version_tuple__: Tuple[int, int, int] = (0, 0, 0)
