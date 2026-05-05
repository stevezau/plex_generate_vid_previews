"""Backend-real E2E: BIF viewer renders REAL JPEG frames from a REAL BIF file.

The audit called this out: every existing ``test_preview_inspector.*`` test
stubs ``/api/bif/info`` to return ``{"frames": []}`` so no frame is ever
rendered. The whole *value* of the inspector is rendering frames; until
this file existed, no e2e test verified a single byte of preview output.

Strategy:
    * Generate a tiny real BIF on disk inside the seeded ``plex_config_folder``
      so the backend's allow-list accepts it (``_validate_bif_path`` requires
      paths under ``plex_config_folder`` OR a server's media root).
    * Hit /bif-viewer with the path tab, paste the BIF path, click Load.
    * Assert <img id="previewFrame"> actually loads JPEG bytes
      (naturalWidth > 0 from a successful image decode).
    * Drag the slider to a different frame index, assert the src changes.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

# A 1x1 pixel valid JPEG — smallest possible bytes that a browser will
# successfully decode (naturalWidth > 0 after load).
_TINY_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb0043000806060706"
    "0508070708090a0d160e0d0c0c0d1b13140f161d281f201c1c1c1c20242a"
    "2d2924262d27353138322f342e2c2f2c40455a4c40495445322c505f5345"
    "576e575c536c544f4c4848484e72504a4c50525548484848484854484848"
    "48484848484848ffc0000b080001000101011100ffc4001f000001050101"
    "01010101000000000000000001020304050607080910111200ffc400b510"
    "0002010303020403050504040000017d010203000411051221314106135161"
    "07227114328191a1082342b1c11552d1f02433627282090a161718191a25"
    "262728292a3435363738393a434445464748494a535455565758595a6364"
    "65666768696a737475767778797a838485868788898a92939495969798"
    "999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9ca"
    "d2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9fa"
    "ffda0008010100003f00fbd0ffd9"
)


def _build_real_bif(bif_path: Path, frame_count: int = 5, interval_ms: int = 1000) -> None:
    """Write a real, valid BIF file with N copies of the same JPEG.

    Mirrors the on-disk layout that bif_reader.read_bif_metadata expects.
    """
    bif_path.parent.mkdir(parents=True, exist_ok=True)
    magic = bytes([0x89, 0x42, 0x49, 0x46, 0x0D, 0x0A, 0x1A, 0x0A])
    version = 0
    bif_table_size = 8 + (8 * frame_count)
    image_index = 64 + bif_table_size
    image_size = len(_TINY_JPEG)

    with open(bif_path, "wb") as f:
        f.write(magic)
        f.write(struct.pack("<I", version))
        f.write(struct.pack("<I", frame_count))
        f.write(struct.pack("<I", interval_ms))
        f.write(b"\x00" * (64 - 20))

        # Per-frame index entries.
        offset = image_index
        for i in range(frame_count):
            f.write(struct.pack("<I", i))  # timestamp
            f.write(struct.pack("<I", offset))
            offset += image_size

        # Sentinel + final offset.
        f.write(struct.pack("<I", 0xFFFFFFFF))
        f.write(struct.pack("<I", offset))

        # Image bodies.
        for _ in range(frame_count):
            f.write(_TINY_JPEG)


@pytest.fixture
def real_bif_setup(tmp_path_factory):
    """Seed a config_dir with a real BIF file under plex_config_folder.

    Returns (settings_overrides, bif_absolute_path) — the overrides
    point ``plex_config_folder`` at our temp dir so ``_validate_bif_path``
    accepts the BIF.
    """
    plex_root = tmp_path_factory.mktemp("plex_root")
    bif_path = plex_root / "Media" / "localhost" / "a" / "bcdef.bundle" / "Contents" / "Indexes" / "index-sd.bif"
    _build_real_bif(bif_path, frame_count=5, interval_ms=1000)

    return {"plex_config_folder": str(plex_root)}, str(bif_path)


@pytest.fixture
def backend_real_app_with_bif(tmp_path_factory, real_bif_setup):
    """Variant of backend_real_app that seeds settings to point at our BIF."""
    import subprocess as sp
    import sys

    overrides, bif_path = real_bif_setup

    # Build config_dir + seed settings.json.
    config_dir = tmp_path_factory.mktemp("backend_real_with_bif")
    from .conftest import _build_fake_ffmpeg_path, _seed_settings_complete, get_free_port, wait_for_port

    _seed_settings_complete(str(config_dir), overrides)

    fake_bin = _build_fake_ffmpeg_path(str(config_dir))
    port = get_free_port()
    env = {
        **os.environ,
        "WEB_PORT": str(port),
        "CONFIG_DIR": str(config_dir),
        "WEB_AUTH_TOKEN": "e2e-test-token",
        "PATH": fake_bin + os.pathsep + os.environ.get("PATH", ""),
    }
    proc = sp.Popen(
        [
            sys.executable,
            "-c",
            f"from media_preview_generator.web.app import run_server; run_server(host='0.0.0.0', port={port})",
        ],
        env=env,
        stdout=sp.PIPE,
        stderr=sp.PIPE,
    )
    if not wait_for_port(port, timeout=20):
        stdout, stderr = proc.communicate(timeout=5)
        proc.kill()
        raise RuntimeError(f"App failed to start: {stderr.decode()[:1000]}")
    try:
        yield (f"http://localhost:{port}", str(config_dir), bif_path)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except sp.TimeoutExpired:
            proc.kill()


@pytest.mark.e2e
class TestBifViewerWithRealFrames:
    def test_load_path_renders_real_frame_then_scrub_loads_different_frame(
        self,
        page: Page,
        context,
        backend_real_app_with_bif: tuple[str, str, str],
    ) -> None:
        from .conftest import _capture_session_cookie

        app_url, _, bif_path = backend_real_app_with_bif
        cookie = _capture_session_cookie(app_url)
        context.add_cookies([cookie])

        # Pre-flight: the backend's /api/bif/info endpoint must accept this
        # BIF path through the allow-list. If this fails, the rest of the
        # test would be a wild goose chase chasing JS bugs that don't exist.
        info_resp = page.request.get(
            f"{app_url}/api/bif/info?path={bif_path}",
            headers={"X-Auth-Token": "e2e-test-token"},
        )
        assert info_resp.ok, (
            f"Backend's /api/bif/info rejected the seeded BIF at {bif_path}: "
            f"status={info_resp.status} body={info_resp.text()}"
        )
        info = info_resp.json()
        assert info["frame_count"] == 5, f"BIF metadata wrong: {info}"

        # Now drive the UI.
        page.goto(f"{app_url}/bif-viewer")
        page.wait_for_load_state("domcontentloaded")

        # Switch to the path tab.
        page.locator('button[data-bs-target="#tabPath"]').click()
        page.locator("#pathInput").fill(bif_path)
        page.locator("#loadPathBtn").click()

        # Viewer panel should reveal — JS does this in showViewer() once
        # /api/bif/info returns successfully.
        expect(page.locator("#viewerPanel")).to_be_visible(timeout=5000)
        expect(page.locator("#totalFrames")).to_have_text("4", timeout=3000)

        # The first frame's <img> src should point at the real /api/bif/frame
        # endpoint with index 0.
        preview = page.locator("#previewFrame")
        first_src = preview.get_attribute("src")
        assert first_src and "/api/bif/frame" in first_src and "index=0" in first_src, (
            f"#previewFrame src not pointing at backend frame endpoint: {first_src!r}"
        )

        # The browser must actually decode the JPEG (not error). naturalWidth>0
        # is the canonical "image loaded" signal — without this the test would
        # pass even if the backend served an empty body or a broken JPEG.
        page.wait_for_function(
            "() => { const img = document.getElementById('previewFrame');"
            "        return img && img.complete && img.naturalWidth > 0; }",
            timeout=5000,
        )

        # Drag the slider to frame index 3 and assert the src changes.
        # The change handler updates src to ?index=3.
        page.evaluate(
            """() => {
                const slider = document.getElementById('frameSlider');
                slider.value = 3;
                slider.dispatchEvent(new Event('input', { bubbles: true }));
            }"""
        )
        # The setFrame() handler updates the src synchronously.
        page.wait_for_function(
            "() => { const img = document.getElementById('previewFrame');"
            "        return img && img.src.includes('index=3'); }",
            timeout=2000,
        )
        new_src = preview.get_attribute("src")
        assert new_src != first_src, (
            f"Scrubber drag did not change preview src — still {first_src!r}. "
            "The frame slider's input handler may be broken."
        )

        # And the new frame must also load successfully (caches were primed
        # by the adjacent-frame preloader, so this should be near-instant).
        page.wait_for_function(
            "() => { const img = document.getElementById('previewFrame');"
            "        return img && img.complete && img.naturalWidth > 0; }",
            timeout=3000,
        )

    def test_real_backend_serves_jpeg_bytes_for_each_frame(
        self,
        page: Page,
        context,
        backend_real_app_with_bif: tuple[str, str, str],
    ) -> None:
        """Backend /api/bif/frame must return real JPEG bytes for every frame.

        Catches the regression class where the BIF index table is read OK
        but the per-frame slicing returns truncated/empty JPEGs.
        """
        from .conftest import _capture_session_cookie

        app_url, _, bif_path = backend_real_app_with_bif
        cookie = _capture_session_cookie(app_url)
        context.add_cookies([cookie])

        for i in range(5):
            r = page.request.get(
                f"{app_url}/api/bif/frame?path={bif_path}&index={i}",
                headers={"X-Auth-Token": "e2e-test-token"},
            )
            assert r.ok, f"Frame {i}: status={r.status} body={r.text()[:200]}"
            assert r.headers.get("content-type", "").startswith("image/jpeg"), (
                f"Frame {i}: wrong content-type {r.headers.get('content-type')!r}"
            )
            body = r.body()
            # Real JPEGs start with FF D8 FF.
            assert body.startswith(b"\xff\xd8\xff"), (
                f"Frame {i}: backend returned non-JPEG bytes (first 4 = {body[:4]!r})"
            )
