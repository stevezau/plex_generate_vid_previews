<!-- This file is the Docker Hub "Full Description" and is auto-synced by CI.
     When you update README.md significantly, update this file to match.
     Docker Hub does not render mermaid diagrams, GitHub admonitions, or relative images. -->

# Plex Generate Previews

GPU-accelerated video preview thumbnail generation for Plex Media Server.

**The Problem:** Plex's built-in preview generation is painfully slow.

**The Solution:** This tool uses GPU acceleration and parallel processing to generate previews **5-10x faster**.

## Features

| Feature | Description |
|---------|-------------|
| **Multi-GPU** | NVIDIA, AMD, Intel, and Windows GPUs |
| **Parallel Processing** | Configurable GPU and CPU worker threads |
| **GPU to CPU Fallback** | Optional fallback-only CPU workers for GPU decode failures |
| **Hardware Acceleration** | CUDA, VAAPI, D3D11VA, VideoToolbox |
| **Library Filtering** | Process specific Plex libraries |
| **Quality Control** | Adjustable thumbnail quality (1-10) |
| **Docker Ready** | Pre-built images with GPU support |
| **Web Dashboard** | Manage jobs, schedules, and status |
| **Scheduling** | Cron and interval-based automation |
| **Smart Skipping** | Automatically skips files that already have thumbnails |
| **Radarr/Sonarr** | Webhook integration for auto-processing on import |

## Quick Start

```bash
docker run -d \
  --name plex-generate-previews \
  --restart unless-stopped \
  -p 8080:8080 \
  --device /dev/dri:/dev/dri \
  -e PUID=1000 \
  -e PGID=1000 \
  -v /path/to/media:/media:ro \
  -v /path/to/plex/config:/plex:rw \
  -v /path/to/app/config:/config:rw \
  -v /etc/localtime:/etc/localtime:ro \
  stevezzau/plex_generate_vid_previews:latest
```

Replace `/path/to/media`, `/path/to/plex/config`, and `/path/to/app/config` with your actual paths.

> **Timezone:** The `/etc/localtime` mount ensures log timestamps and scheduled jobs use your local time. Alternatively, use `-e TZ=America/New_York` (replace with your [timezone](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)).

Then open `http://YOUR_IP:8080`, retrieve the authentication token from container logs, and complete the setup wizard.

### Get Your Authentication Token

```bash
docker logs plex-generate-previews | grep "Token:"
```

Or set a fixed token with `-e WEB_AUTH_TOKEN=your-password`.

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
    image: stevezzau/plex_generate_vid_previews:latest
    container_name: plex-generate-previews
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
      # - NVIDIA_DRIVER_CAPABILITIES=compute,video,utility
      - PUID=1000
      - PGID=1000
    volumes:
      - /path/to/your/media:/media:ro
      - /path/to/plex/config:/plex:rw
      - /path/to/app/config:/config:rw
      - /etc/localtime:/etc/localtime:ro
```

### CPU-Only

```yaml
services:
  plex-previews:
    image: stevezzau/plex_generate_vid_previews:latest
    container_name: plex-generate-previews
    restart: unless-stopped
    ports:
      - "8080:8080"
    environment:
      - GPU_THREADS=0
      - CPU_THREADS=8
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
| **All** | Windows | D3D11VA | Native only |
| **Apple Silicon** | macOS | VideoToolbox | Native only |

> **"Native only"** means GPU acceleration requires running the app from source on that platform. Docker on Windows (WSL2) and macOS runs a Linux VM — D3D11VA and VideoToolbox are not available inside Docker. Docker on these platforms will use CPU-only processing. Apple Silicon users benefit from the native ARM64 Docker image (no Rosetta overhead).

### NVIDIA GPU

Prerequisites: NVIDIA drivers + [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html).

```bash
docker run -d \
  --gpus all \
  -e NVIDIA_VISIBLE_DEVICES=all \
  -e NVIDIA_DRIVER_CAPABILITIES=compute,video,utility \
  -e PUID=1000 \
  -e PGID=1000 \
  -p 8080:8080 \
  -v /path/to/media:/media:ro \
  -v /path/to/plex/config:/plex:rw \
  -v /path/to/app/config:/config:rw \
  -v /etc/localtime:/etc/localtime:ro \
  stevezzau/plex_generate_vid_previews:latest
```

### GPU + CPU Fallback Mode

Set **CPU Workers** to `0` and **CPU Fallback Workers** to `1` (or higher) to keep main processing on GPU while allowing CPU retry for unsupported codecs.

## Environment Variables

All settings can be configured in the web UI (Settings page). Environment variables are optional overrides.

| Variable | Default | Description |
|----------|---------|-------------|
| `PLEX_URL` | *(wizard)* | Plex server URL (e.g. `http://192.168.1.100:32400`) |
| `PLEX_TOKEN` | *(wizard)* | Plex authentication token (auto-set via OAuth wizard) |
| `PLEX_CONFIG_FOLDER` | *(wizard)* | Path to Plex config folder |
| `GPU_THREADS` | `1` | Number of GPU worker threads (0-32) |
| `CPU_THREADS` | `1` | Number of CPU worker threads (0-32) |
| `FALLBACK_CPU_THREADS` | `0` | CPU fallback workers for GPU failures (0-32) |
| `THUMBNAIL_QUALITY` | `4` | Preview quality 1-10 (2=highest) |
| `PLEX_BIF_FRAME_INTERVAL` | `5` | Seconds between preview images (1-60) |
| `PLEX_LIBRARIES` | All | Comma-separated library names or IDs |
| `PUID` | `1000` | User ID (Unraid: `99`) |
| `PGID` | `1000` | Group ID (Unraid: `100`) |
| `TZ` | Host | Timezone (e.g. `America/New_York`). Alternative to mounting `/etc/localtime` |
| `WEB_PORT` | `8080` | Web server port |
| `WEB_AUTH_TOKEN` | Auto | Fixed authentication token |
| `LOG_LEVEL` | `INFO` | Logging level: DEBUG, INFO, WARNING, ERROR |
| `TMP_FOLDER` | System | Temporary folder for processing |
| `PLEX_TIMEOUT` | `60` | Plex API timeout in seconds |

## Unraid

Search for "plex-generate-previews" in Community Applications, or run manually:

```bash
docker run -d \
  --name plex-generate-previews \
  --restart unless-stopped \
  -p 8080:8080 \
  --device /dev/dri:/dev/dri \
  -e WEB_AUTH_TOKEN=my-secret-password \
  -e PUID=99 \
  -e PGID=100 \
  -v /mnt/user/data/plex:/data/plex:ro \
  -v "/mnt/cache/appdata/plex/Library/Application Support/Plex Media Server":/plex:rw \
  -v /mnt/user/appdata/plex-generate-previews:/config:rw \
  -v /etc/localtime:/etc/localtime:ro \
  stevezzau/plex_generate_vid_previews:latest
```

## Performance Tuning

| GPU Threads | CPU Threads | Use Case |
|-------------|-------------|----------|
| 1 | 1 | Default (safe for all hardware) |
| 4 | 2 | Balanced (mid-range systems) |
| 8 | 4 | High-end systems |
| 0 | 8 | CPU-only |

Configure in the web UI under **Settings**.

## Important Notes

- **Don't use `init: true`** in docker-compose -- this container uses s6-overlay and `init: true` will break it.
- **Use your host IP for Plex** -- the container cannot reach `localhost` on your host. Use `http://192.168.1.100:32400`, not `http://localhost:32400`.
- **Recommended Plex setting** -- set "Generate video preview thumbnails" to **Never** in Plex settings. This tool replaces that with GPU-accelerated processing.

## Documentation

Full documentation is available on GitHub:

- [Getting Started](https://github.com/stevezau/plex_generate_vid_previews/blob/main/docs/getting-started.md) -- Docker, GPU, Unraid, devcontainer
- [Configuration & API Reference](https://github.com/stevezau/plex_generate_vid_previews/blob/main/docs/reference.md) -- All settings and REST API
- [Guides & Troubleshooting](https://github.com/stevezau/plex_generate_vid_previews/blob/main/docs/guides.md) -- Web interface, webhooks, FAQ

## Support

- [Report a Bug](https://github.com/stevezau/plex_generate_vid_previews/issues/new?labels=bug)
- [Request a Feature](https://github.com/stevezau/plex_generate_vid_previews/issues/new?labels=enhancement)
- [GitHub Repository](https://github.com/stevezau/plex_generate_vid_previews)

## License

MIT License. See [LICENSE](https://github.com/stevezau/plex_generate_vid_previews/blob/main/LICENSE) for details.
