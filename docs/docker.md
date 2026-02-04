# Docker Guide

> [Back to Docs](README.md)

Complete Docker deployment guide with examples for different GPU configurations.

---

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
  stevezzau/plex_generate_vid_previews:latest
```

Then open `http://YOUR_SERVER_IP:8080` and complete the **Setup Wizard**.

> 💡 **No environment variables required!** Sign in with Plex OAuth through the web UI.

---

## Docker Compose

Copy `docker-compose.example.yml` from the repository and modify for your setup.

### Intel GPU

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
      - PLEX_URL=http://192.168.1.100:32400
      - PLEX_TOKEN=your-token
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

## Volume Mounts

| Container Path | Purpose | Mode |
|----------------|---------|------|
| `/media` | Your media files | `ro` (read-only) |
| `/plex` | Plex application data | `rw` (read-write) |
| `/config` | App config, schedules, auth | `rw` (read-write) |

---

## Environment Variables

See [Configuration Reference](configuration.md) for all options.

Essential variables:

| Variable | Required | Example |
|----------|----------|---------|
| `PLEX_URL` | ✅ | `http://192.168.1.100:32400` |
| `PLEX_TOKEN` | ✅ | `your-plex-token` |
| `PUID` | Recommended | `1000` |
| `PGID` | Recommended | `1000` |

---

## Important Notes

### Don't Use `init: true`

This container uses s6-overlay. Adding `init: true` will break it:

```yaml
# ❌ WRONG - Don't do this
services:
  plex-previews:
    init: true  # Remove this!
```

### Use IP Address, Not localhost

The container can't reach `localhost` on your host:

```bash
# ❌ WRONG
PLEX_URL=http://localhost:32400

# ✅ CORRECT
PLEX_URL=http://192.168.1.100:32400
```

---

## CLI Mode

Run one-time processing instead of web server:

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

## View Logs

```bash
# All logs
docker logs plex-generate-previews

# Follow logs
docker logs -f plex-generate-previews

# Get auth token
docker logs plex-generate-previews | grep "Token:"
```

---

## Update

```bash
docker pull stevezzau/plex_generate_vid_previews:latest
docker stop plex-generate-previews
docker rm plex-generate-previews
# Re-run docker run command
```

---

[Back to Docs](README.md) | [Main README](../README.md)

---

## Networking

> How to configure Docker networking for the Plex Preview Generator.

### Quick Decision Tree

```
Where is your Plex server running?
│
├── On the same Docker host?
│   └── What network mode does Plex use?
│       ├── Host network → Use --network host
│       ├── Bridge (default) → Use same network or Plex IP:port
│       └── Custom (br1, macvlan) → Use same custom network
│
└── On a different machine?
    └── Use bridge network with Plex's IP address
```

### Network Modes

| Mode | Port Mapping | Container IP | Best For |
|------|--------------|--------------|----------|
| Bridge | Required | 172.17.x.x | Remote Plex, simple setups |
| Host | Not needed | Host's IP | Maximum compatibility |
| br1/macvlan | Not needed | LAN IP | Unraid, Synology, NAS |

### Common Scenarios

**Plex on Unraid with br1:**
```yaml
services:
  plex-previews:
    image: stevezzau/plex_generate_vid_previews:latest
    networks:
      - br1

networks:
  br1:
    external: true
```

**Plex on different machine:**
```bash
docker run -p 8080:8080 stevezzau/plex_generate_vid_previews
# Then enter http://PLEX_IP:32400 in setup wizard
```

### Troubleshooting Network Issues

**"Connection refused" or timeout:**
```bash
# Check networks match
docker inspect plex | grep -A 10 "Networks"
docker inspect plex-previews | grep -A 10 "Networks"

# Test from inside container
docker exec -it plex-previews curl -I http://PLEX_IP:32400
```

**Plex.direct URLs timing out:**
Use local IP address instead - the setup wizard prefers local connections automatically.

---

[Back to Docs](README.md) | [Main README](../README.md)
