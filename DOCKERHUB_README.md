<!-- This file is the Docker Hub "Full Description" and is auto-synced by CI.
     When you update README.md significantly, update this file to match.
     Docker Hub does not render mermaid diagrams, GitHub admonitions, or relative images. -->

# Media Preview Generator

GPU-accelerated video preview thumbnail generation for **Plex, Emby, and Jellyfin**. **Web UI only** — no CLI.

> Previously named **Plex Generate Previews** at `stevezzau/plex_generate_vid_previews`. That image keeps mirroring updates until **2026-10-29**; after that, only this repo (`stevezzau/media_preview_generator`) is published. Update your `compose` to the new name when convenient — settings and volumes carry over unchanged.

**The Problem:** Built-in preview generation has gaps depending on which server you run:

- **Plex** generates thumbnails single-threaded on the CPU (no GPU support).
- **Emby** has no GPU support for thumbnail generation at all.
- **Jellyfin** does support hardware-accelerated trickplay, but it shares CPU/GPU with playback — and on a busy server those are resources you'd rather give to the player.

**The Solution:** This tool runs preview generation **off the media server** on a machine of your choosing, uses every GPU it finds, and processes files in parallel. When two or more servers contain the same file, FFmpeg runs only once — the result is then written out in each server's expected format.

## Features

| Feature | Description |
|---------|-------------|
| **Multi-Vendor** | Plex, Emby, and Jellyfin in any combination, any number of each |
| **One Pass, Many Servers** | When two or more servers contain the same file, FFmpeg runs only once — result is written in each server's expected format |
| **Universal Webhook URL** | One inbound URL handles Plex / Emby / Jellyfin / Sonarr / Radarr — vendor auto-detected |
| **Multi-Plex** | Multiple Plex servers configured side by side, routed automatically |
| **Plex OAuth** | One sign-in lists every Plex server your account can reach; tick which ones to add |
| **Jellyfin Quick Connect** | Friendliest auth — no password ever leaves your browser |
| **Jellyfin trickplay one-click fix** | Detects + auto-flips Jellyfin's library settings so the previews you publish actually appear in Jellyfin's web UI (the most common Jellyfin gotcha) |
| **Smart Caching** | If two servers contain the same file, the second one reuses the result of the first instead of running FFmpeg again. Cache size and timeout are configurable. |
| **Automatic Retry on Slow Indexing** | If your media server hasn't finished scanning a new file yet, the app retries automatically (30 s → 2 m → 5 m → 15 m → 60 m) instead of giving up |
| **Multi-Server Preview Viewer** | Inspect published previews for any file across any server from one viewer |
| **Multi-GPU** | NVIDIA, AMD, Intel — per-GPU enable/disable and worker count |
| **Parallel Processing** | Configurable GPU and CPU worker threads |
| **GPU to CPU Fallback** | If a file fails on the GPU, the same worker automatically retries it on the CPU — no extra config |
| **Hardware Acceleration** | CUDA, VAAPI, QuickSync, plus Vulkan support for Dolby Vision colour handling |
| **Per-Server Filtering** | Library toggles, path mappings, and exclude rules scoped per server |
| **Quality Control** | Adjustable thumbnail quality (1-10) and frame interval (1-60 s) |
| **Docker Ready** | Pre-built images with GPU support |
| **Web Dashboard** | Manage jobs, schedules, status, and recent-webhook history |
| **Scheduling** | Cron and interval-based automation |
| **Smart Skipping** | Skips files that already have a fresh preview, but detects when a file has been swapped (e.g. Sonarr/Radarr quality upgrade) and regenerates automatically |
| **Radarr/Sonarr Webhooks** | Auto-process new content on import |
| **Plex Direct Webhook** | Auto-trigger on `library.new` (requires Plex Pass) for media added outside Sonarr/Radarr |
| **Recently Added Scanner** | Polling fallback that catches manually-added items without needing Plex Pass |

## Quick Start

```bash
docker run -d \
  --name media-preview-generator \
  --restart unless-stopped \
  -p 8080:8080 \
  --device /dev/dri:/dev/dri \
  -e PUID=1000 \
  -e PGID=1000 \
  -v /path/to/media:/media:ro \
  -v /path/to/plex/config:/plex:rw \
  -v /path/to/app/config:/config:rw \
  -v /etc/localtime:/etc/localtime:ro \
  stevezzau/media_preview_generator:latest
```

Replace `/path/to/media`, `/path/to/plex/config`, and `/path/to/app/config` with your actual paths.

> **Timezone:** The `/etc/localtime` mount ensures log timestamps and scheduled jobs use your local time. Alternatively, use `-e TZ=America/New_York` (replace with your [timezone](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)).

Then open `http://YOUR_IP:8080`, retrieve the authentication token from container logs, and complete the setup wizard. All settings (Plex connection, GPU config, processing options) are configured in the web UI Settings page.

## Image Tags

| Tag | Source | Use for |
|---|---|---|
| `:latest` | Latest release (e.g. `3.7.2`) | **Recommended.** Stable. |
| `:3.7.2` (version) | A specific release | Pinning to a known-good version |
| `:dev` | Every push to `dev` | Bleeding edge — may break |

## Volume Mounts

| Container Path | Purpose | Mode |
|----------------|---------|------|
| `/media` | Your media files | `ro` (read-only) |
| `/plex` | Plex application data (where BIF files are stored) | `rw` |
| `/config` | App settings, schedules, job history | `rw` |

## Docker Compose

### GPU (Intel / AMD / NVIDIA)

```yaml
services:
  plex-previews:
    image: stevezzau/media_preview_generator:latest
    container_name: media-preview-generator
    restart: unless-stopped
    ports:
      - "8080:8080"
    # Intel / AMD GPU (VAAPI)
    devices:
      - /dev/dri:/dev/dri
    # NVIDIA: remove 'devices' above, uncomment below
    # deploy:
    #   resources:
    #     reservations:
    #       devices:
    #         - driver: nvidia
    #           count: all
    #           capabilities: [gpu]
    environment:
      # NVIDIA only (uncomment if using NVIDIA):
      # - NVIDIA_VISIBLE_DEVICES=all
      # Use 'all' so the NVIDIA Vulkan driver is injected; 'graphics' is
      # required for Dolby Vision Profile 5 libplacebo tone-mapping.
      # - NVIDIA_DRIVER_CAPABILITIES=all
      - PUID=1000
      - PGID=1000
    volumes:
      - /path/to/your/media:/media:ro
      - /path/to/plex/config:/plex:rw
      - /path/to/app/config:/config:rw
      - /etc/localtime:/etc/localtime:ro
```

### CPU-Only

Set GPU Workers to 0 and CPU Workers as needed in the web UI Settings.

```yaml
services:
  plex-previews:
    image: stevezzau/media_preview_generator:latest
    container_name: media-preview-generator
    restart: unless-stopped
    ports:
      - "8080:8080"
    environment:
      - PUID=1000
      - PGID=1000
    volumes:
      - /path/to/your/media:/media:ro
      - /path/to/plex/config:/plex:rw
      - /path/to/app/config:/config:rw
      - /etc/localtime:/etc/localtime:ro
```

## GPU Support

| GPU Type | Platform | Acceleration | Docker Flag |
|----------|----------|--------------|-------------|
| **NVIDIA** | Linux | CUDA/NVENC | `--gpus all` |
| **AMD** | Linux | VAAPI | `--device /dev/dri` |
| **Intel** | Linux | QuickSync/VAAPI | `--device /dev/dri` |
| **NVIDIA** | Windows | CUDA | Native only |
| **AMD/Intel** | Windows | D3D11VA | Native only |
| **Apple Silicon** | macOS | VideoToolbox | Native only |

> **"Native only"** means GPU acceleration requires running the app from source on that platform. Docker on Windows (WSL2) and macOS runs a Linux VM — D3D11VA and VideoToolbox are not available inside Docker. Docker on these platforms will use CPU-only processing. Apple Silicon users benefit from the native ARM64 Docker image (no Rosetta overhead).

### NVIDIA GPU

Prerequisites: NVIDIA drivers + [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html).

```bash
docker run -d \
  --gpus all \
  -e NVIDIA_VISIBLE_DEVICES=all \
  -e NVIDIA_DRIVER_CAPABILITIES=all \
  -e PUID=1000 \
  -e PGID=1000 \
  -p 8080:8080 \
  -v /path/to/media:/media:ro \
  -v /path/to/plex/config:/plex:rw \
  -v /path/to/app/config:/config:rw \
  -v /etc/localtime:/etc/localtime:ro \
  stevezzau/media_preview_generator:latest
```

### GPU + CPU Fallback

CPU fallback is automatic. If a file fails on the GPU (unsupported codec, driver crash, etc.), the same worker automatically retries it on the CPU and the dashboard shows a yellow "CPU fallback" badge so you know it happened. No separate worker pool to configure — increase **CPU Workers** above `0` only if you have a lot of content that never decodes on the GPU and you want those files to route straight to dedicated CPU workers.

## Environment Variables

All application settings (Plex, GPU, processing) are configured in the web UI Settings page. `settings.json` in `/config` is the single source of truth. The only infrastructure env vars that remain active:

| Variable | Default | Description |
|----------|---------|-------------|
| `CONFIG_DIR` | `/config` | Path to config directory |
| `WEB_PORT` | `8080` | Web server port |
| `PUID` | `1000` | User ID (Unraid: `99`) |
| `PGID` | `1000` | Group ID (Unraid: `100`) |
| `TZ` | Host | Timezone (e.g. `America/New_York`) |
| `CORS_ORIGINS` | `*` | CORS allowed origins |
| `HTTPS` | `false` | Enable HTTPS |
| `DEV_RELOAD` | `false` | Enable dev reload |

Application-level env vars (PLEX_URL, PLEX_TOKEN, CPU_THREADS, etc.) act as one-time seed values on first startup. They are migrated into settings.json. After that, settings.json is the source of truth.

## Unraid

Search for "media-preview-generator" in Community Applications, or run manually:

```bash
docker run -d \
  --name media-preview-generator \
  --restart unless-stopped \
  -p 8080:8080 \
  --device /dev/dri:/dev/dri \
  -e PUID=99 \
  -e PGID=100 \
  -v /mnt/user/data/plex:/data/plex:ro \
  -v "/mnt/cache/appdata/plex/Library/Application Support/Plex Media Server":/plex:rw \
  -v /mnt/user/appdata/media-preview-generator:/config:rw \
  -v /etc/localtime:/etc/localtime:ro \
  stevezzau/media_preview_generator:latest
```

## Performance Tuning

Configure GPU and CPU workers per-GPU in the web UI under **Settings**.

## Important Notes

- **Don't add `init: true`** to your docker-compose file — this container manages its own processes internally, and `init: true` conflicts with that.
- **Use your host IP for Plex** -- the container cannot reach `localhost` on your host. Use `http://192.168.1.100:32400`, not `http://localhost:32400`.
- **Recommended Plex setting** -- set "Generate video preview thumbnails" to **Never** in Plex settings. This tool replaces that with GPU-accelerated processing.

## Documentation

Full documentation is available on GitHub:

- [Getting Started](https://github.com/stevezau/media_preview_generator/blob/main/docs/getting-started.md) — Docker, GPU, Unraid, networking
- [Guides & Troubleshooting](https://github.com/stevezau/media_preview_generator/blob/main/docs/guides.md) — Web UI, schedules, webhooks, HDR, troubleshooting
- [Configuration & API Reference](https://github.com/stevezau/media_preview_generator/blob/main/docs/reference.md) — All settings, env vars, and REST API
- [FAQ](https://github.com/stevezau/media_preview_generator/blob/main/docs/faq.md) — Common questions about setup, performance, and compatibility

## Support

- [Report a Bug](https://github.com/stevezau/media_preview_generator/issues/new?labels=bug)
- [Request a Feature](https://github.com/stevezau/media_preview_generator/issues/new?labels=enhancement)
- [GitHub Repository](https://github.com/stevezau/media_preview_generator)

## License

MIT License. See [LICENSE](https://github.com/stevezau/media_preview_generator/blob/main/LICENSE) for details.
