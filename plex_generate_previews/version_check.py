"""Version check module.

Behavior:
- Dev Docker image (GIT_BRANCH and GIT_SHA set): compare baked commit to GitHub
  branch head and warn if behind.
- Git checkout (running from source): compare current commit to GitHub branch head.
- Otherwise (installed package, zip, release Docker): compare package SemVer to GitHub latest release.
"""

import os
import re
import subprocess

import requests
from loguru import logger

from .utils import is_docker_environment


def get_current_version() -> str:
    """Get the current version from package metadata.

    Priority order:
    1. Local _version.py (when running from source)
    2. Installed package metadata (when installed via pip)
    3. Fallback to "0.0.0"

    Returns:
        str: Current version string (e.g., "2.1.2")

    """
    try:
        from . import __version__

        return __version__
    except (ImportError, AttributeError):
        pass

    try:
        import importlib.metadata

        return importlib.metadata.version("plex-generate-previews")
    except Exception:
        logger.debug("importlib.metadata version lookup failed", exc_info=True)

    logger.debug("Could not determine current version, using fallback")
    return "0.0.0"


def parse_version(version_str: str) -> tuple[int, int, int]:
    """Parse a semantic version string into comparable tuple.

    Args:
        version_str: Version string like "2.0.0", "1.5.3", "2.1.1.post14"

    Returns:
        Tuple of (major, minor, patch) integers

    Raises:
        ValueError: If version string format is invalid

    """
    # Remove any 'v' prefix and extract version parts
    clean_version = version_str.lstrip("v")

    # Match semantic version pattern (major.minor.patch) with optional suffixes
    # Supports: 2.0.0, v2.1.2, 2.1.1.post14, 2.3.1.dev5, 0.0.0+unknown, 2.3.1.dev5+g1234abc
    match = re.match(
        r"^(\d+)\.(\d+)\.(\d+)(?:\.(?:post|dev)\d+)?(?:-[a-zA-Z0-9.-]+)?(?:\+[a-zA-Z0-9.-]+)?$",
        clean_version,
    )

    if not match:
        raise ValueError(f"Invalid version format: {version_str}")

    major, minor, patch = match.groups()[:3]
    return (int(major), int(minor), int(patch))


def get_latest_github_release() -> str | None:
    """Query GitHub API for the latest release version.

    Returns:
        str: Latest release version string, or None if failed

    """
    try:
        # GitHub API endpoint for latest release
        url = "https://api.github.com/repos/stevezau/plex_generate_vid_previews/releases/latest"

        # Set timeout and user agent
        headers = {"User-Agent": "plex-generate-previews-version-check"}

        response = requests.get(url, headers=headers, timeout=3)
        response.raise_for_status()

        data = response.json()
        latest_version = data.get("tag_name", "")

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
            logger.debug("GitHub API error: {}", e.response.status_code)
        return None
    except requests.exceptions.RequestException as e:
        logger.debug("Version check request failed: {}", e)
        return None
    except (KeyError, ValueError) as e:
        logger.debug("Invalid response from GitHub API: {}", e)
        return None
    except Exception as e:
        logger.debug("Unexpected error during version check: {}", e)
        return None


def get_git_commit_sha() -> str | None:
    """Get current Git commit SHA if running from a git checkout.

    Checks in this order:
    1. Current working directory (if user ran from git checkout)
    2. Module directory (if running from source, not installed)

    Returns:
        str: Full 40-char SHA of current commit, or None if not in git repo

    """
    # Try current working directory first (user might have cd'd into git repo)
    for check_dir in [os.getcwd(), os.path.dirname(__file__)]:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=2,
                cwd=check_dir,
            )
            if result.returncode == 0:
                sha = result.stdout.strip()
                logger.debug("Found git repo in: {}", check_dir)
                return sha
        except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
            continue

    logger.debug("Not running from a git repository")
    return None


def get_git_branch() -> str | None:
    """Get current Git branch if running from a git checkout.

    Checks in this order:
    1. Current working directory (if user ran from git checkout)
    2. Module directory (if running from source, not installed)

    Returns:
        str: Branch name (e.g., "main", "dev"), or None if not in git repo

    """
    # Try current working directory first (user might have cd'd into git repo)
    for check_dir in [os.getcwd(), os.path.dirname(__file__)]:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=2,
                cwd=check_dir,
            )
            if result.returncode == 0:
                branch = result.stdout.strip()
                # Ignore detached HEAD state
                if branch and branch != "HEAD":
                    return branch
        except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
            continue

    return None


def get_branch_head_sha(branch: str) -> str | None:
    """Query GitHub API for the latest commit SHA on a branch.

    Args:
        branch: Branch name (e.g., "dev")

    Returns:
        str: Full 40-char SHA of branch head, or None if failed

    """
    try:
        url = f"https://api.github.com/repos/stevezau/plex_generate_vid_previews/branches/{branch}"
        headers = {"User-Agent": "plex-generate-previews-version-check"}
        response = requests.get(url, headers=headers, timeout=3)
        response.raise_for_status()
        data = response.json()
        commit = data.get("commit", {})
        sha = commit.get("sha", "")
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
        logger.debug("GitHub branch API error: {}", getattr(e.response, "status_code", "unknown"))
        return None
    except requests.exceptions.RequestException as e:
        logger.debug("Branch head request failed: {}", e)
        return None
    except Exception as e:
        logger.debug("Unexpected error during branch head check: {}", e)
        return None


def check_for_updates() -> None:
    """Check for available updates and log appropriate messages.

    Detection logic:
    1. Dev Docker: Check GIT_BRANCH + GIT_SHA env vars, compare commits
    2. Git checkout: Check for .git directory, compare commits on current branch
    3. Release/Pip/Zip: Compare package version with latest GitHub release
    """
    try:
        # Path 1: Dev Docker image - commit-aware check when metadata present
        git_branch = (os.environ.get("GIT_BRANCH") or "").strip()
        git_sha = (os.environ.get("GIT_SHA") or "").strip()

        if git_branch and git_sha:
            logger.debug("Dev Docker detected: branch={}, commit={}", git_branch, git_sha[:7])
            head_sha = get_branch_head_sha(git_branch)
            if head_sha:
                # Compare allowing short SHAs inside full SHA
                current_short = git_sha[:7]
                head_short = head_sha[:7]
                if not head_sha.startswith(git_sha):
                    logger.warning(
                        "⚠️  Newer dev commit on {}: {} (you have: {})", git_branch, head_short, current_short
                    )
                    logger.warning("🐳 Update dev image: docker pull stevezzau/plex_generate_vid_previews:dev")
                    return
                else:
                    logger.info("✅ Dev build up to date with {} branch ({})", git_branch, head_short)
                    return
            else:
                logger.debug("Could not check remote {} branch (API call failed)", git_branch)

        # Path 2: Git checkout - running from source repository
        local_commit = get_git_commit_sha()
        local_branch = get_git_branch()

        logger.debug(
            "Git detection: commit={}, branch={}", local_commit[:7] if local_commit else "None", local_branch or "None"
        )

        if local_commit and local_branch:
            logger.debug("Detected git checkout on branch '{}' at commit {}", local_branch, local_commit[:7])
            head_sha = get_branch_head_sha(local_branch)
            if head_sha:
                current_short = local_commit[:7]
                head_short = head_sha[:7]
                if not head_sha.startswith(local_commit):
                    logger.warning("⚠️  Newer commit on {}: {} (you have: {})", local_branch, head_short, current_short)
                    logger.warning("🔄 Update: git pull origin {}", local_branch)
                    return
                else:
                    logger.info("✅ Git checkout up to date with {} branch ({})", local_branch, head_short)
                    return
            else:
                logger.debug("Could not fetch remote {} branch head (API call failed)", local_branch)
        else:
            logger.debug("Git checkout detection skipped - commit or branch not found")

        # Path 3: Release/Pip/Zip install - version-based check
        current_version = get_current_version()
        logger.debug("Current version: {}", current_version)

        # Get latest version from GitHub
        latest_version = get_latest_github_release()
        if not latest_version:
            logger.debug("Could not determine latest version")
            return

        logger.debug("Latest version: {}", latest_version)

        # Parse versions for comparison
        try:
            current_tuple = parse_version(current_version)
            latest_tuple = parse_version(latest_version)
        except ValueError as e:
            logger.debug("Version parsing error: {}", e)
            return

        # Compare versions
        if latest_tuple > current_tuple:
            # Check if running from dev snapshot (zip download without git)
            if current_version.startswith("0.0.0"):
                logger.warning("ℹ️  Running from development snapshot (not an official release)")
                logger.warning("ℹ️  Latest stable release: {}", latest_version)
                logger.warning("🐳 Install stable: docker pull stevezzau/plex_generate_vid_previews:latest")
                logger.warning(
                    "🔗 Or from source: pip install git+https://github.com/stevezau/plex_generate_vid_previews.git"
                )
            else:
                # Normal version update available
                logger.warning("⚠️  A newer version is available: {} (you have: {})", latest_version, current_version)

                # Provide appropriate update instructions based on environment
                if is_docker_environment():
                    logger.warning("🐳 Update: docker pull stevezzau/plex_generate_vid_previews:latest")
                else:
                    logger.warning(
                        "📦 Update: pip install --upgrade git+https://github.com/stevezau/plex_generate_vid_previews.git"
                    )

            logger.warning("🔗 Release notes: https://github.com/stevezau/plex_generate_vid_previews/releases/latest")
        else:
            logger.debug("Version is up to date")

    except Exception as e:
        # Catch any unexpected errors and log at debug level
        logger.debug("Version check failed unexpectedly: {}", e)
