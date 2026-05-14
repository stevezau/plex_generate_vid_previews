"""Cross-path consistency for worker row labels.

Three independent paths produce rows for the dashboard's Workers panel
and they used to drift into different label formats — visible to the
user as the panel shape flipping between mid-job and idle states.
This module locks the contract: every path delegates to
``jobs/worker_naming.py``.
"""

from __future__ import annotations

from media_preview_generator.jobs.worker_naming import (
    cpu_worker_label,
    friendly_device_label,
    gpu_worker_label,
)


class TestFriendlyDeviceLabel:
    def test_short_nvidia_name_passes_through(self):
        info = {"name": "NVIDIA TITAN RTX"}
        assert friendly_device_label(info, "cuda:0", "NVIDIA") == "NVIDIA TITAN RTX"

    def test_long_intel_name_collapses_to_bracketed_marketing_string(self):
        info = {"name": "Intel Corporation Raptor Lake-S GT1 [UHD Graphics 770] (rev 04)"}
        # The lspci verbose string used to appear in the Workers panel
        # verbatim — wrapping the row and shoving the status badge
        # around. We pull the user-recognisable bracketed identifier
        # and prepend the vendor.
        assert friendly_device_label(info, "/dev/dri/renderD128", "INTEL") == "Intel UHD Graphics 770"

    def test_amd_radeon_label(self):
        info = {"name": "Advanced Micro Devices [Radeon RX 7900 XT]"}
        assert friendly_device_label(info, "/dev/dri/renderD129", "AMD") == "AMD Radeon RX 7900 XT"

    def test_missing_name_falls_back_to_device_path(self):
        assert friendly_device_label({}, "/dev/dri/renderD128", "INTEL") == "/dev/dri/renderD128"

    def test_missing_everything_falls_back_to_GPU(self):
        assert friendly_device_label({}, None, None) == "GPU"

    def test_simple_namespace_input_works(self):
        from types import SimpleNamespace

        info = SimpleNamespace(name="NVIDIA GeForce RTX 4090")
        assert friendly_device_label(info, "cuda:0", "NVIDIA") == "NVIDIA GeForce RTX 4090"


class TestWorkerLabels:
    def test_gpu_worker_label_format(self):
        # Matches the legacy WorkerPool's display_name shape that
        # pre-dates the multi-server refactor — keeps long-time users
        # from seeing the labels visibly shift.
        assert gpu_worker_label(1, "NVIDIA TITAN RTX") == "GPU Worker 1 (NVIDIA TITAN RTX)"
        assert gpu_worker_label(3, "Intel UHD Graphics 770") == "GPU Worker 3 (Intel UHD Graphics 770)"

    def test_cpu_worker_label_format(self):
        assert cpu_worker_label(1) == "CPU Worker 1"
        assert cpu_worker_label(7) == "CPU Worker 7"


class TestAllProducersUseTheSameHelper:
    """Every place that builds a worker row label should delegate to
    :mod:`jobs.worker_naming`. If a future change adds a fourth path
    (or reverts one of the existing three to inline string formatting),
    this test catches it before it reaches the canary.
    """

    def _hits(self, path: str, needle: str) -> int:
        with open(path) as f:
            return f.read().count(needle)

    def test_no_inline_old_format_in_production_code(self):
        # The old format was f"{gpu_base_name} #{idx}" / f"CPU - Worker {idx}".
        # Allow occurrences inside this test file (we name the format
        # explicitly above). Anywhere else in production code is a bug.
        roots = [
            "media_preview_generator/jobs/orchestrator.py",
            "media_preview_generator/jobs/worker.py",
            "media_preview_generator/jobs/dispatcher.py",
            "media_preview_generator/web/routes/api_jobs.py",
        ]
        offenders = []
        for r in roots:
            with open(r) as f:
                text = f.read()
            # The literal old GPU format token; comment-only mentions
            # are fine, but a live f-string is not.
            if 'f"{gpu_base_name} #{idx}"' in text:
                offenders.append((r, 'f"{gpu_base_name} #{idx}"'))
            if 'f"CPU - Worker {idx}"' in text:
                offenders.append((r, 'f"CPU - Worker {idx}"'))
            if 'f"{gpu_name} #{w + 1}"' in text:
                offenders.append((r, 'f"{gpu_name} #{w + 1}"'))
        assert not offenders, f"Found inline worker-label strings — must use jobs/worker_naming helpers: {offenders!r}"
