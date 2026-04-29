"""E2E tests for wizard step 4: per-GPU panel + steppers + CPU workers.

Regressions covered:

* Per-GPU panel renders one card per detected GPU.
* `+`/`−` stepper buttons mutate the input value and respect min/max.
* Disabling a GPU greys out its workers/threads cells.
* Re-scan GPUs button POSTs `/api/system/rescan-gpus`.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from ._mocks import (
    capture_settings_save,
    mock_plex_libraries,
    mock_settings_get,
    mock_setup_status,
    mock_system_rescan_gpus,
    mock_system_status,
    mock_validate_plex_config_folder,
)


def _drive_to_step4(page: Page, app_url: str) -> None:
    page.goto(f"{app_url}/setup")
    page.wait_for_load_state("domcontentloaded")
    page.locator('.wizard-vendor-btn[data-vendor="plex"]').click()
    page.evaluate("document.getElementById('manualConnectDetails').open = true")
    page.locator("#manualPlexUrl").fill("http://plex.local:32400")
    page.locator("#manualPlexToken").fill("tok")
    page.locator("#manualPlexTestBtn").click()
    expect(page.locator("#manualPlexResult")).to_contain_text("Connected", timeout=5000)
    page.locator("#step1Next").click()
    page.locator(".library-card").first.click()
    page.locator("#step2Next").click()
    expect(page.locator('div.setup-step[data-step="3"]')).to_have_class("setup-step active")
    page.locator("#wizardPlexConfigFolder").fill("/plex")
    page.locator("#step3Next").click()
    expect(page.locator('div.setup-step[data-step="4"]')).to_have_class("setup-step active")


@pytest.mark.e2e
class TestPerGpuPanel:
    def test_renders_card_per_detected_gpu(self, wizard_page: Page, app_url_wizard: str) -> None:
        mock_plex_libraries(wizard_page)
        capture_settings_save(wizard_page)
        mock_setup_status(wizard_page, complete=False)
        mock_validate_plex_config_folder(wizard_page, valid=True)
        mock_settings_get(wizard_page)
        mock_system_status(
            wizard_page,
            gpus=[
                {"type": "nvidia", "device": "/dev/nvidia0", "name": "GPU 0", "status": "ok"},
                {"type": "vaapi", "device": "/dev/dri/renderD128", "name": "iGPU", "status": "ok"},
            ],
        )
        _drive_to_step4(wizard_page, app_url_wizard)
        # Wait for the "Detecting GPUs..." spinner to be hidden.
        expect(wizard_page.locator("#gpuDetecting")).to_be_hidden()
        cards = wizard_page.locator("#gpuConfigList .card")
        expect(cards).to_have_count(2)

    def test_workers_stepper_increments_value(self, wizard_page: Page, app_url_wizard: str) -> None:
        mock_plex_libraries(wizard_page)
        capture_settings_save(wizard_page)
        mock_setup_status(wizard_page, complete=False)
        mock_validate_plex_config_folder(wizard_page, valid=True)
        mock_settings_get(wizard_page)
        mock_system_status(wizard_page)  # default: 2 GPUs
        _drive_to_step4(wizard_page, app_url_wizard)
        expect(wizard_page.locator("#gpuDetecting")).to_be_hidden()

        first_workers = wizard_page.locator("#gpuConfigList .gpu-workers").first
        expect(first_workers).to_have_value("1")
        # The stepper's + button is the next sibling button.
        plus = first_workers.locator(".. >> .stepper-plus").first
        plus.click()
        expect(first_workers).to_have_value("2")
        plus.click()
        expect(first_workers).to_have_value("3")

    def test_workers_stepper_minus_clamps_at_one(self, wizard_page: Page, app_url_wizard: str) -> None:
        mock_plex_libraries(wizard_page)
        capture_settings_save(wizard_page)
        mock_setup_status(wizard_page, complete=False)
        mock_validate_plex_config_folder(wizard_page, valid=True)
        mock_settings_get(wizard_page)
        mock_system_status(wizard_page)
        _drive_to_step4(wizard_page, app_url_wizard)
        expect(wizard_page.locator("#gpuDetecting")).to_be_hidden()

        workers = wizard_page.locator("#gpuConfigList .gpu-workers").first
        minus = workers.locator(".. >> .stepper-minus").first
        # Default value is 1 = min; minus button should be disabled.
        expect(minus).to_be_disabled()

    def test_disabling_gpu_greys_out_workers(self, wizard_page: Page, app_url_wizard: str) -> None:
        mock_plex_libraries(wizard_page)
        capture_settings_save(wizard_page)
        mock_setup_status(wizard_page, complete=False)
        mock_validate_plex_config_folder(wizard_page, valid=True)
        mock_settings_get(wizard_page)
        mock_system_status(wizard_page)
        _drive_to_step4(wizard_page, app_url_wizard)
        expect(wizard_page.locator("#gpuDetecting")).to_be_hidden()

        toggle = wizard_page.locator(".gpu-enable-toggle").first
        device_id = toggle.get_attribute("data-device") or ""
        # Sanitised id: replace non-alphanumeric with underscore (matches
        # the panel JS).
        safe_id = "".join(c if c.isalnum() else "_" for c in device_id)
        # Untick — should grey out .gpu-settings-{safe_id} cells.
        toggle.uncheck()
        # Settings cells get inline styles opacity:0.5 + pointer-events:none.
        first_settings = wizard_page.locator(f".gpu-settings-{safe_id}").first
        opacity = first_settings.evaluate("el => el.style.opacity")
        assert opacity == "0.5"

    def test_rescan_gpus_button_calls_endpoint(self, wizard_page: Page, app_url_wizard: str) -> None:
        mock_plex_libraries(wizard_page)
        capture_settings_save(wizard_page)
        mock_setup_status(wizard_page, complete=False)
        mock_validate_plex_config_folder(wizard_page, valid=True)
        mock_settings_get(wizard_page)
        mock_system_status(wizard_page)
        called = mock_system_rescan_gpus(wizard_page)
        _drive_to_step4(wizard_page, app_url_wizard)
        expect(wizard_page.locator("#gpuDetecting")).to_be_hidden()

        wizard_page.locator("#gpuRescanBtn").click()
        # Give the JS a tick to fire the fetch.
        wizard_page.wait_for_timeout(300)
        assert called, "POST /api/system/rescan-gpus was never called"


@pytest.mark.e2e
class TestCpuWorkersStepper:
    def test_cpu_workers_stepper_present_and_increments(self, wizard_page: Page, app_url_wizard: str) -> None:
        mock_plex_libraries(wizard_page)
        capture_settings_save(wizard_page)
        mock_setup_status(wizard_page, complete=False)
        mock_validate_plex_config_folder(wizard_page, valid=True)
        mock_settings_get(wizard_page)
        mock_system_status(wizard_page)
        _drive_to_step4(wizard_page, app_url_wizard)

        cpu = wizard_page.locator("#cpuThreads")
        expect(cpu).to_have_value("1")
        plus = cpu.locator(".. >> .stepper-plus").first
        plus.click()
        expect(cpu).to_have_value("2")
