"""Tests for the timezone detection and warning system."""

import os
import time
from unittest.mock import patch

from media_preview_generator.web.routes.api_system import _get_timezone_info


class TestGetTimezoneInfo:
    """Unit tests for _get_timezone_info()."""

    def test_no_tz_env_and_utc_system_shows_warning(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TZ", None)
            with patch.object(time, "tzname", ("UTC", "UTC")):
                result = _get_timezone_info()
                assert result["timezone"] == "UTC"
                assert result["tz_env_set"] is False
                assert "warning" in result
                assert "/etc/localtime" in result["warning"]
                assert "TZ=" in result["warning"]

    def test_tz_env_set_no_warning(self):
        with patch.dict(os.environ, {"TZ": "America/New_York"}):
            with patch.object(time, "tzname", ("EST", "EDT")):
                result = _get_timezone_info()
                assert result["timezone"] == "EST"
                assert result["tz_env_set"] is True
                assert "warning" not in result

    def test_tz_env_explicitly_utc_no_warning(self):
        with patch.dict(os.environ, {"TZ": "UTC"}):
            with patch.object(time, "tzname", ("UTC", "UTC")):
                result = _get_timezone_info()
                assert result["timezone"] == "UTC"
                assert result["tz_env_set"] is True
                assert "warning" not in result

    def test_no_tz_env_but_non_utc_system_no_warning(self):
        """If /etc/localtime is mounted, system tz != UTC even without TZ env."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TZ", None)
            with patch.object(time, "tzname", ("PST", "PDT")):
                result = _get_timezone_info()
                assert result["timezone"] == "PST"
                assert result["tz_env_set"] is False
                assert "warning" not in result
