"""
Version check module.

Behavior:
- Dev Docker image (GIT_BRANCH and GIT_SHA set): compare baked commit to GitHub
  branch head and warn if behind.
- Otherwise (pip, zip, release Docker): compare package SemVer to GitHub latest release.
"""

import os
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


def get_branch_head_sha(branch: str) -> Optional[str]:
    """
    Query GitHub API for the latest commit SHA on a branch.
    
    Args:
        branch: Branch name (e.g., "dev")
        
    Returns:
        str: Full 40-char SHA of branch head, or None if failed
    """
    try:
        url = f"https://api.github.com/repos/stevezau/plex_generate_vid_previews/branches/{branch}"
        headers = {
            'User-Agent': 'plex-generate-previews-version-check'
        }
        response = requests.get(url, headers=headers, timeout=3)
        response.raise_for_status()
        data = response.json()
        commit = data.get('commit', {})
        sha = commit.get('sha', '')
        if not sha:
            logger.debug("GitHub API returned empty branch sha")
            return None
        return sha
    except requests.exceptions.Timeout:
        logger.debug("Branch head check timed out - no internet connection or slow response")
        return None
    except requests.exceptions.ConnectionError:
        logger.debug("Branch head check failed - no internet connection")
        return None
    except requests.exceptions.HTTPError as e:
        logger.debug(f"GitHub branch API error: {getattr(e.response, 'status_code', 'unknown')}")
        return None
    except requests.exceptions.RequestException as e:
        logger.debug(f"Branch head request failed: {e}")
        return None
    except Exception as e:
        logger.debug(f"Unexpected error during branch head check: {e}")
        return None


def check_for_updates() -> None:
    """
    Check for available updates and log appropriate messages.
    
    Args:
        None
    """
    
    try:
        # Dev Docker image path: commit-aware check when metadata present
        git_branch = (os.environ.get("GIT_BRANCH") or "").strip()
        git_sha = (os.environ.get("GIT_SHA") or "").strip()

        if git_branch and git_sha:
            head_sha = get_branch_head_sha(git_branch)
            if head_sha:
                # Compare allowing short SHAs inside full SHA
                current_short = git_sha[:7]
                head_short = head_sha[:7]
                if not head_sha.startswith(git_sha):
                    logger.warning(f"⚠️  Newer dev commit on {git_branch}: {head_short} (you have: {current_short})")
                    logger.warning("🐳 Update dev image: docker pull stevezzau/plex_generate_vid_previews:dev")
                    return
                else:
                    logger.debug(f"Dev build up to date with {git_branch} ({head_short})")
                    return

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
