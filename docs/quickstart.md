# Quick Start & Docker Guide

> [Back to Docs](README.md)

Get Plex preview thumbnails generating in minutes.

---

## Prerequisites

1. ✅ Plex Media Server running and accessible
2. ✅ A Plex account (for OAuth sign-in)
3. ✅ Docker installed on your server

---

## Quick Start (5 minutes)

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

> **Note:** No environment variables needed! The setup wizard handles configuration.

### Step 2: Get Your Access Token

```bash
docker logs plex-generate-previews | grep "Token:"
```

### Step 3: Complete the Setup Wizard

1. Open `http://YOUR_SERVER_IP:8080`
2. Enter the authentication token from the logs
3. Follow the wizard: **Sign in with Plex** → **Select Server** → **Configure Paths** → **Start Processing**

---

## Volume Mounts

| Container Path | Purpose | Mode |
|----------------|---------|------|
| `/media` | Your media files | `ro` (read-only) |
| `/plex` | Plex application data (where BIF files are stored) | `rw` |
| `/config` | App settings, schedules, job history | `rw` |

---

## Docker Compose

### Intel GPU (QuickSync)

```yaml
services:
  plex-previews:
    image: stevezzau/plex_generate_vid_previews:latest
    container_name: plex-generate-previews
    restart: unless-stopped
    ports:
      - "8080:8080"
    devices:
      - /dev/dri:/dev/dri
    environment:
      - PUID=1000
      - PGID=1000
      # Optional: Set a fixed auth token (otherwise check container logs)
      # - WEB_AUTH_TOKEN=your-password
    volumes:
      - /path/to/media:/media:ro
      - /path/to/plex/config:/plex:rw
      - /path/to/app/config:/config:rw
```

### NVIDIA GPU

```yaml
services:
  plex-previews:
    image: stevezzau/plex_generate_vid_previews:latest
    container_name: plex-generate-previews
    restart: unless-stopped
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
    ports:
      - "8080:8080"
    environment:
      - NVIDIA_VISIBLE_DEVICES=all
      - NVIDIA_DRIVER_CAPABILITIES=compute,video,utility
      - PUID=1000
      - PGID=1000
    volumes:
      - /path/to/media:/media:ro
      - /path/to/plex/config:/plex:rw
      - /path/to/app/config:/config:rw
```

### CPU Only

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
      - /path/to/media:/media:ro
      - /path/to/plex/config:/plex:rw
      - /path/to/app/config:/config:rw
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

> **Note:** Use Unraid's PUID=99, PGID=100 (nobody:users).

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

## Pip Install (Alternative)

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
docker logs plex-generate-previews | grep "Token:"  # Get auth token
```

### Update

```bash
docker pull stevezzau/plex_generate_vid_previews:latest
docker stop plex-generate-previews
docker rm plex-generate-previews
# Re-run your docker run command
```

---

## Important Notes

**Don't Use `init: true`**

This container uses s6-overlay. Adding `init: true` in docker-compose will break it.

**Use IP Address, Not localhost**

The container can't reach `localhost` on your host:

```bash
# ❌ WRONG: PLEX_URL=http://localhost:32400
# ✅ CORRECT: PLEX_URL=http://192.168.1.100:32400
```

---

## What's Next?

| Goal | Guide |
|------|-------|
| All configuration options | [Configuration](configuration.md) |
| GPU acceleration details | [GPU Support](gpu-support.md) |
| Scheduling & dashboard | [Web Interface](web-interface.md) |
| Unraid-specific setup | [Unraid Guide](unraid.md) |
| FAQ & troubleshooting | [FAQ](faq.md) |

---

[Back to Docs](README.md) | [Main README](../README.md)
