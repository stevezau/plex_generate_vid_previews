# Release Notes — v3.5.0

## What's New

### BIF Viewer

You can now preview the actual thumbnail images the app generated without ever leaving the web UI. Go to the new **BIF Viewer** page, search for any movie or show, and scrub through the thumbnails frame by frame. Great for checking whether HDR tone mapping looks right or if the quality setting needs adjusting.

### Log Viewer

A full-featured **Log Viewer** is now built into the web UI. It shows live logs as they happen, keeps history so you can scroll back, lets you filter by log level, and has a copy button for sharing logs when reporting issues. The viewer follows the server's log level setting — change it in Settings and the viewer updates automatically.

### Job Priority

You can now assign a priority (High, Normal, or Low) when starting a job. If multiple jobs are queued, higher-priority jobs run first. You can also change the priority of a running or queued job on the fly from the dashboard.

### External Authentication

If you already secure access to your apps through a reverse proxy (Authelia, Authentik, nginx auth, Caddy Security) or a VPN (Tailscale, WireGuard), you can now skip the built-in login screen entirely. Set `AUTH_METHOD=external` and the app trusts your network-level authentication. Webhook authentication for Radarr/Sonarr is not affected.

### Per-GPU Configuration

Each GPU detected by the app can now be configured independently. Enable or disable individual GPUs, and set the number of workers and FFmpeg threads per GPU — all from the Settings page.

### Schedule Editing

You can now edit existing schedules directly from the web UI instead of having to delete and recreate them.

---

## What's Improved

### Better HDR and Dolby Vision Thumbnails

This was a big focus area. If your HDR thumbnails looked too dark or your Dolby Vision content produced green/purple artifacts, this release should fix it:

- **Dolby Vision Profile 5** (the tricky one without an HDR10 fallback layer) is handled by libplacebo with proper Vulkan tone mapping.
- **Dolby Vision Profile 7/8** (with an HDR10 fallback layer) now correctly uses the HDR10 base layer for tone mapping — simpler and more reliable.
- **HDR10 / HLG** content uses a corrected filter chain with proper brightness levels. The previous settings crushed highlights and made thumbnails look dark.
- The tone mapping algorithm is now configurable in Settings (default: Hable). If your thumbnails still look dark, try Reinhard.

### NVIDIA on Windows

NVIDIA GPUs on Windows now use CUDA acceleration instead of the generic D3D11VA path. This is faster and more reliable. D3D11VA is still used as a fallback for AMD and Intel GPUs on Windows.

### Better Container Compatibility

GPU detection now works in containers that don't have `/sys/class/drm` (common on TrueNAS Scale and Kubernetes). Previously, the app couldn't find GPUs in these environments.

### Faster Settings and Webhooks Pages

Both pages were slow to load due to unnecessary processing on every page view. This is now fixed — they load instantly.

### Smoother Job Cancellation

When you cancel a job, the FFmpeg processes are now killed immediately instead of waiting for the current file to finish.

### Live GPU Configuration

If you change GPU settings (enable/disable a GPU, change workers) while a job is running, the worker pool adjusts on the fly — no restart needed.

---

## What's Removed

### CLI Mode

The command-line interface (`--cli` flag) has been removed. The web UI is now the only way to use the app. All features that were available in CLI mode are available in the web UI, including one-time processing, scheduling, and webhook automation.

---

## Upgrading

This is a drop-in upgrade. Pull the latest Docker image and restart:

```bash
docker pull stevezzau/plex_generate_vid_previews:latest
docker stop plex-generate-previews
docker rm plex-generate-previews
# Re-run your docker run command
```

Your settings, schedules, and job history in `/config` are preserved. The app automatically migrates your settings to the new format on first start.

**If you were using `--cli` mode**, you'll need to switch to the web UI. Start a container with port 8080 exposed, open the web UI, and use scheduling or webhooks to automate jobs.

**If you had dark HDR thumbnails**, they should look better now. You can use the new BIF Viewer to check, and adjust the tone mapping algorithm in Settings if needed.
