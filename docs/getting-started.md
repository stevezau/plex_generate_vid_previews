# Getting Started

> [Back to Docs](README.md)

Get Plex preview thumbnails generating in minutes.

> [!IMPORTANT]
> This page is the source of truth for installation and first-time setup.
> For operations and troubleshooting, use [Guides & Troubleshooting](guides.md).
> For exact settings and API contracts, use [Configuration & API Reference](reference.md).

## Related Docs

- [Guides & Troubleshooting](guides.md)
- [Configuration & API Reference](reference.md)
- [Contributing & Development](../CONTRIBUTING.md)

---

## Prerequisites

1. Plex Media Server running and accessible
2. A Plex account (for OAuth sign-in)
3. Docker installed on your server

---

## Quick Start (Docker)

### Step 1: Run the Container

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
  stevezzau/plex_generate_vid_previews:latest
```

> [!NOTE]
> No environment variables are required for first-time setup.
> Advanced deployments can still use environment variables from the reference docs.

### Step 2: Get Your Access Token

Find your token using the [Authentication Token](#authentication-token) section below.

### Step 3: Complete the Setup Wizard

1. Open `http://YOUR_SERVER_IP:8080`
2. Enter the authentication token from the logs
3. Follow the wizard: **Sign in with Plex** → **Select Server** → **Configure Paths** → **Options** → **Security**

---

## Volume Mounts

| Container Path | Purpose | Mode |
|----------------|---------|------|
| `/media` | Your media files | `ro` (read-only) |
| `/plex` | Plex application data (where BIF files are stored) | `rw` |
| `/config` | App settings, schedules, job history | `rw` |

---

## Authentication Token

Use this section whenever documentation asks for your authentication token.

```bash
docker logs plex-generate-previews | grep "Token:"
```

You can also set a fixed token for predictable logins:

```bash
WEB_AUTH_TOKEN=your-password
```

---

## Docker Compose

See [docker-compose.example.yml](../docker-compose.example.yml) for ready-to-use configurations:

| Configuration | Use Case |
|---------------|----------|
| **GPU** | Single service covering Intel, AMD, and NVIDIA with inline comments |
| **CPU-Only** | Minimal service with `GPU_THREADS=0` |
| **Unraid** | Intel iGPU default with NVIDIA alternative, Unraid paths and permissions |

Copy the file, uncomment the section for your hardware, and adjust volume paths.

---

## GPU Acceleration

Hardware-accelerated video processing for faster thumbnail generation.

### Supported GPUs

| GPU Type | Platform | Acceleration | Docker Support |
|----------|----------|--------------|----------------|
| **NVIDIA** | Linux | CUDA/NVENC | NVIDIA Container Toolkit |
| **AMD** | Linux | VAAPI | Device passthrough |
| **Intel** | Linux | VAAPI/QuickSync | Device passthrough |
| **All GPUs** | Windows | D3D11VA | Native only |
| **Apple Silicon** | macOS | VideoToolbox | Native only |

### GPU Detection

Check which GPUs are detected:

```bash
# Docker
docker run --rm --device /dev/dri:/dev/dri stevezzau/plex_generate_vid_previews:latest --list-gpus

# Local install
plex-generate-previews --list-gpus
```

Example output:

```
✅ Found 2 GPU(s):
  [0] NVIDIA GeForce RTX 4090 (CUDA)
  [1] Intel UHD Graphics 770 (VAAPI - /dev/dri/renderD128)
```

### Intel iGPU (QuickSync)

Most common setup, especially on Unraid.

```bash
docker run -d \
  --device /dev/dri:/dev/dri \
  -e PUID=1000 \
  -e PGID=1000 \
  stevezzau/plex_generate_vid_previews:latest
```

Verify the device exists:

```bash
ls -la /dev/dri
# Should show: card0, renderD128
```

If permission denied:

```bash
# Find video group ID
getent group video
# Add to Docker: --group-add <gid>
```

### NVIDIA GPU

Prerequisites:

1. Install NVIDIA drivers
2. Install [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)

```bash
docker run -d \
  --gpus all \
  -e NVIDIA_VISIBLE_DEVICES=all \
  -e NVIDIA_DRIVER_CAPABILITIES=compute,video,utility \
  stevezzau/plex_generate_vid_previews:latest
```

Docker Compose:

```yaml
services:
  plex-previews:
    image: stevezzau/plex_generate_vid_previews:latest
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```

### AMD GPU

```bash
docker run -d \
  --device /dev/dri:/dev/dri \
  --group-add video \
  -e PUID=1000 \
  -e PGID=1000 \
  stevezzau/plex_generate_vid_previews:latest
```

AMD requires proper VAAPI drivers on the host system.

### Windows (Native)

Windows uses D3D11VA hardware acceleration automatically with any GPU.

```bash
plex-generate-previews --list-gpus
```

Requirements: latest GPU drivers (NVIDIA, AMD, or Intel) and FFmpeg with D3D11VA support.

### macOS (Native)

Apple Silicon and Intel Macs use VideoToolbox.

```bash
plex-generate-previews --list-gpus
```

### Multi-GPU Selection

```bash
# Use only GPU 0 and 2
--gpu-selection "0,2"

# Use all GPUs (default)
--gpu-selection "all"
```

### CPU-Only Mode

Disable GPU acceleration:

```bash
--gpu-threads 0 --cpu-threads 8
```

### Performance Tuning

| Threads | Recommendation |
|---------|----------------|
| GPU: 1, CPU: 1 | Default (safe for all hardware) |
| GPU: 4, CPU: 2 | Balanced (mid-range systems) |
| GPU: 8, CPU: 4 | High-end systems |
| GPU: 0, CPU: 8 | CPU-only |

> [!TIP]
> Start with the defaults and increase GPU/CPU threads gradually. Monitor system load to find the optimal balance for your hardware.

---

## Unraid

Setup guide for Unraid with Community Applications template and manual Docker options.

### Quick Start

1. **Run the container** (see options below)
2. **Open the Web UI** at `http://YOUR_UNRAID_IP:8080`
3. **Get the authentication token** from container logs or set `WEB_AUTH_TOKEN`
4. **Complete the Setup Wizard** — sign in with Plex, configure settings

> [!TIP]
> The setup wizard guides you through Plex OAuth authentication. No need to manually find your Plex token!

### Community Applications (Easiest)

Search for "plex-generate-previews" in Community Applications and install the template.

### Manual Docker Run — Intel iGPU (Most Common)

```bash
docker run -d \
  --name plex-generate-previews \
  --restart unless-stopped \
  -p 8080:8080 \
  -l net.unraid.docker.webui="http://[IP]:[PORT:8080]/" \
  --device /dev/dri:/dev/dri \
  -e WEB_AUTH_TOKEN=my-secret-password \
  -e PUID=99 \
  -e PGID=100 \
  -v /mnt/user/data/plex:/data/plex:ro \
  -v "/mnt/cache/appdata/plex/Library/Application Support/Plex Media Server":/plex:rw \
  -v /mnt/user/appdata/plex-generate-previews:/config:rw \
  stevezzau/plex_generate_vid_previews:latest
```

| Setting | Value | Description |
|---------|-------|-------------|
| `-l net.unraid.docker.webui` | Web UI label | Adds a Web UI button in Unraid |
| `--device /dev/dri` | GPU passthrough | Intel VAAPI/QuickSync |
| `WEB_AUTH_TOKEN` | `my-secret-password` | **Your login password** (set your own!) |
| `PUID/PGID` | `99/100` | Unraid `nobody:users` account |
| `/data/plex` | Media path | Same as Plex container = no path mapping |
| `/config` | App config | Settings persist here |

### Intel iGPU with Custom Network (br0/br1)

If you use a custom Docker network with fixed IPs (common on Unraid):

```bash
docker run -d \
  --name plex-generate-previews \
  --restart unless-stopped \
  --network=br0 \
  --ip=192.168.1.50 \
  --device /dev/dri:/dev/dri \
  -e WEB_AUTH_TOKEN=my-secret-password \
  -e PUID=99 \
  -e PGID=100 \
  -v /mnt/user/data/plex:/data/plex:ro \
  -v "/mnt/cache/appdata/plex/Library/Application Support/Plex Media Server":/plex:rw \
  -v /mnt/user/appdata/plex-generate-previews:/config:rw \
  stevezzau/plex_generate_vid_previews:latest
```

> [!NOTE]
> When using `--network` with a fixed IP, you don't need `-p 8080:8080` — access the web UI directly at `http://192.168.1.50:8080`

### NVIDIA GPU on Unraid

Requires: Nvidia-Driver plugin from Community Applications.

```bash
docker run -d \
  --name plex-generate-previews \
  --restart unless-stopped \
  --runtime=nvidia \
  -p 8080:8080 \
  -e NVIDIA_VISIBLE_DEVICES=all \
  -e NVIDIA_DRIVER_CAPABILITIES=compute,video,utility \
  -e WEB_AUTH_TOKEN=my-secret-password \
  -e PUID=99 \
  -e PGID=100 \
  -v /mnt/user/data/plex:/data/plex:ro \
  -v "/mnt/cache/appdata/plex/Library/Application Support/Plex Media Server":/plex:rw \
  -v /mnt/user/appdata/plex-generate-previews:/config:rw \
  stevezzau/plex_generate_vid_previews:latest
```

### Important Unraid Notes

**PUID/PGID Values** — Unraid uses `nobody:users` by default:

| Variable | Value | Description |
|----------|-------|-------------|
| `PUID` | `99` | `nobody` user |
| `PGID` | `100` | `users` group |

**Network Considerations** — When completing the Setup Wizard, select your Plex server from the dropdown. If using a local server, make sure the container can reach it (not `localhost` from Unraid's perspective).

**Check Intel GPU Exists:**

```bash
ls -la /dev/dri
# Should show: card0, renderD128
```

### Path Mapping for Plex

Path mapping is only needed when this container mounts media to a **different path** than Plex uses.

**No Mapping Needed (Recommended)** — mount media to the same container path as Plex:

```
Plex:           /mnt/user/data/plex → /data/plex
This container: /mnt/user/data/plex → /data/plex  ← Same path!
```

**With Path Mapping** — if you prefer mounting to `/media`:

```
Plex:           /mnt/user/data/plex → /data/plex
This container: /mnt/user/data/plex → /media      ← Different path
```

Add path mapping:

```bash
-e PLEX_VIDEOS_PATH_MAPPING=/data/plex \
-e PLEX_LOCAL_VIDEOS_PATH_MAPPING=/media \
```

See [Path Mappings](reference.md#path-mappings) for more examples.

### TRaSH Guide Folder Structure

For users following [TRaSH Guides](https://trash-guides.info/):

1. **Configure Plex Container** — add a second container path:
   - Container path: `/server/media/plex/`
   - Host path: `/mnt/user/media/plex/`

2. **Update Plex Libraries** — use the new mapping:
   - Format: `//server/media/plex/<media-folder>`
   - Example: `//server/media/plex/tv`

3. **Set Permissions:**
   ```bash
   chmod -R 777 /mnt/cache/appdata/plex/Library/Application\ Support/Plex\ Media\ Server/Media/
   ```

---

## Networking

### Quick Decision Tree

```
Where is your Plex server?
│
├── Same Docker host?
│   ├── Plex uses host network → Use --network host
│   ├── Plex uses bridge (default) → Use same network or Plex IP:port
│   └── Plex uses custom (br1, macvlan) → Use same custom network
│
└── Different machine?
    └── Use bridge network with Plex's IP address
```

### Custom Network Example (Unraid)

```bash
docker run -d \
  --name plex-generate-previews \
  --restart unless-stopped \
  --network=br1 \
  --ip=192.168.1.51 \
  -l net.unraid.docker.webui="http://[IP]:[PORT:8080]/" \
  --device /dev/dri:/dev/dri \
  -e WEB_AUTH_TOKEN=your-password \
  -e PUID=99 \
  -e PGID=100 \
  -v /mnt/user/data/plex:/data/plex:ro \
  -v "/mnt/cache/appdata/plex/Library/Application Support/Plex Media Server":/plex:rw \
  -v /mnt/user/appdata/plex-generate-previews:/config:rw \
  stevezzau/plex_generate_vid_previews:latest
```

> [!NOTE]
> Use Unraid's PUID=99, PGID=100 (nobody:users).

---

## CLI Mode

Run one-time processing without the web server:

```bash
docker run --rm \
  --device /dev/dri:/dev/dri \
  -e PLEX_URL=http://192.168.1.100:32400 \
  -e PLEX_TOKEN=your-token \
  -v /path/to/media:/media:ro \
  -v /path/to/plex/config:/plex:rw \
  stevezzau/plex_generate_vid_previews:latest --cli
```

---

## Pip Install

```bash
# Install
pip install git+https://github.com/stevezau/plex_generate_vid_previews.git

# Check GPUs
plex-generate-previews --list-gpus

# Run
plex-generate-previews \
  --plex-url http://localhost:32400 \
  --plex-token your-plex-token \
  --plex-config-folder "/path/to/Plex Media Server"
```

---

## Common Operations

### View Logs

```bash
docker logs plex-generate-previews          # All logs
docker logs -f plex-generate-previews       # Follow logs
```

### Update

```bash
docker pull stevezzau/plex_generate_vid_previews:latest
docker stop plex-generate-previews
docker rm plex-generate-previews
# Re-run your docker run command
```

---

## Development Environment

The project includes a [devcontainer](https://containers.dev/) configuration for a consistent development environment.

### What It Provides

- Python 3.12 with FFmpeg and mediainfo
- Docker-in-Docker for container builds
- Pre-commit hooks (ruff check + format)
- Playwright with Chromium for e2e testing

### How to Use

- **VS Code**: Reopen in Container (Ctrl+Shift+P → "Dev Containers: Reopen in Container")
- **GitHub Codespaces**: Open the repository in a Codespace

### Installed Extensions

Python, Debugpy, Ruff, Pylance, TOML, Copilot, Playwright, Coverage Gutters, Docker, REST Client

### Port Forwarding

| Port | Service |
|------|---------|
| 8080 | Web UI |
| 8089 | Locust (load testing) |

### Post-Create Setup

The devcontainer automatically:

1. Installs the package with dev dependencies (`pip install -e ".[dev]"`)
2. Installs Playwright Chromium browser
3. Sets up pre-commit hooks

See `.devcontainer/` for the full configuration.

---

## Important Notes

**Don't Use `init: true`**

This container uses s6-overlay. Adding `init: true` in docker-compose will break it.

**Use IP Address, Not localhost**

The container can't reach `localhost` on your host:

```bash
# WRONG:  PLEX_URL=http://localhost:32400
# CORRECT: PLEX_URL=http://192.168.1.100:32400
```

---

## Next Steps

- Run and monitor jobs from the [Web Interface Guide](guides.md#web-interface)
- Configure webhooks in [Webhook Integration](guides.md#webhook-integration)
- Review all tunables in [Configuration & API Reference](reference.md)

---

[Back to Docs](README.md) | [Main README](../README.md)
