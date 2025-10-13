"""
Version check module for checking if a newer version is available.

Queries GitHub Releases API to compare current version with latest release.
Handles network failures gracefully without interrupting application startup.
"""

import re
import requests
from typing import Optional, Tuple
from loguru import logger
from .utils import is_docker_environment


def get_current_version() -> str:
    """
    Get the current version from package metadata.
    
    Returns:
        str: Current version string (e.g., "2.1.2")
    """
    try:
        # Try to get version from installed package metadata first (pip install)
        import importlib.metadata
        return importlib.metadata.version("plex-generate-previews")
    except Exception:
        # Fallback to reading directly from _version.py (running from source)
        try:
            from . import __version__
            return __version__
        except Exception:
            pass
            
    # Final fallback - return a default version
    logger.debug("Could not determine current version, using fallback")
    return "0.0.0"


def parse_version(version_str: str) -> Tuple[int, int, int]:
    """
    Parse a semantic version string into comparable tuple.
    
    Args:
        version_str: Version string like "2.0.0", "1.5.3", "2.1.1.post14"
        
    Returns:
        Tuple of (major, minor, patch) integers
        
    Raises:
        ValueError: If version string format is invalid
    """
    # Remove any 'v' prefix and extract version parts
    clean_version = version_str.lstrip('v')
    
    # Match semantic version pattern (major.minor.patch) with optional suffixes
    # Supports: 2.0.0, v2.1.2, 2.1.1.post14, 2.0.0-beta, etc.
    match = re.match(r'^(\d+)\.(\d+)\.(\d+)(?:\.post\d+)?(?:-[a-zA-Z0-9.-]+)?(?:\+[a-zA-Z0-9.-]+)?$', clean_version)
    
    if not match:
        raise ValueError(f"Invalid version format: {version_str}")
    
    major, minor, patch = match.groups()
    return (int(major), int(minor), int(patch))


def get_latest_github_release() -> Optional[str]:
    """
    Query GitHub API for the latest release version.
    
    Returns:
        str: Latest release version string, or None if failed
    """
    try:
        # GitHub API endpoint for latest release
        url = "https://api.github.com/repos/stevezau/plex_generate_vid_previews/releases/latest"
        
        # Set timeout and user agent
        headers = {
            'User-Agent': 'plex-generate-previews-version-check'
        }
        
        response = requests.get(url, headers=headers, timeout=3)
        response.raise_for_status()
        
        data = response.json()
        latest_version = data.get('tag_name', '')
        
        if not latest_version:
            logger.debug("GitHub API returned empty tag_name")
            return None
            
        return latest_version
        
    except requests.exceptions.Timeout:
        logger.debug("Version check timed out - no internet connection or slow response")
        return None
    except requests.exceptions.ConnectionError:
        logger.debug("Version check failed - no internet connection")
        return None
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            logger.debug("Repository or releases not found on GitHub")
        elif e.response.status_code == 429:
            logger.debug("GitHub API rate limit exceeded")
        else:
            logger.debug(f"GitHub API error: {e.response.status_code}")
        return None
    except requests.exceptions.RequestException as e:
        logger.debug(f"Version check request failed: {e}")
        return None
    except (KeyError, ValueError) as e:
        logger.debug(f"Invalid response from GitHub API: {e}")
        return None
    except Exception as e:
        logger.debug(f"Unexpected error during version check: {e}")
        return None


def check_for_updates(skip_check: bool = False) -> None:
    """
    Check for available updates and log appropriate messages.
    
    Args:
        skip_check: If True, skip the version check entirely
    """
    if skip_check:
        logger.debug("Version check skipped by user request")
        return
    
    try:
        # Get current version
        current_version = get_current_version()
        logger.debug(f"Current version: {current_version}")
        
        # Get latest version from GitHub
        latest_version = get_latest_github_release()
        if not latest_version:
            logger.debug("Could not determine latest version")
            return
            
        logger.debug(f"Latest version: {latest_version}")
        
        # Parse versions for comparison
        try:
            current_tuple = parse_version(current_version)
            latest_tuple = parse_version(latest_version)
        except ValueError as e:
            logger.debug(f"Version parsing error: {e}")
            return
        
        # Compare versions
        if latest_tuple > current_tuple:
            # Newer version available - show warning
            logger.warning(f"⚠️  A newer version is available: {latest_version} (you have: {current_version})")
            
            # Provide appropriate update instructions based on environment
            if is_docker_environment():
                logger.warning("🐳 Update: docker pull stevezzau/plex_generate_vid_previews:latest")
            else:
                logger.warning("📦 Update: pip install --upgrade git+https://github.com/stevezau/plex_generate_vid_previews.git")
            
            logger.warning("🔗 Release notes: https://github.com/stevezau/plex_generate_vid_previews/releases/latest")
        else:
            logger.debug("Version is up to date")
            
    except Exception as e:
        # Catch any unexpected errors and log at debug level
        logger.debug(f"Version check failed unexpectedly: {e}")
