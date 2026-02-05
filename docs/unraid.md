# Unraid Guide

> [Back to Docs](README.md)

Setup guide for Unraid with Community Applications template and manual Docker options.

---

## Quick Start

1. **Run the container** (see options below)
2. **Open the Web UI** at `http://YOUR_UNRAID_IP:8080`
3. **Get the auth token** from container logs or set `WEB_AUTH_TOKEN`
4. **Complete the Setup Wizard** - Sign in with Plex, configure settings

> **Tip:** The setup wizard guides you through Plex OAuth authentication. No need to manually find your Plex token!

---

## Option 1: Community Applications (Easiest)

Search for "plex-generate-previews" in Community Applications and install the template.

---

## Option 2: Manual Docker Run

### Intel iGPU (QuickSync) - Most Common

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

**Configuration:**
| Setting | Value | Description |
|---------|-------|-------------|
| `-l net.unraid.docker.webui` | WebUI label | Adds WebUI button in Unraid |
| `--device /dev/dri` | GPU passthrough | Intel VAAPI/QuickSync |
| `WEB_AUTH_TOKEN` | `my-secret-password` | **Your login password** (set your own!) |
| `PUID/PGID` | `99/100` | Unraid nobody:users |
| `/data/plex` | Media path | Same as Plex container = no path mapping |
| `/config` | App config | Settings persist here |

> **Note:** `PLEX_URL` and `PLEX_TOKEN` are no longer required! The Setup Wizard will help you sign in securely via Plex OAuth.

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

**Network Settings:**
| Setting | Example | Description |
|---------|---------|-------------|
| `--network` | `br0` or `br1` | Your custom bridge network name |
| `--ip` | `192.168.1.50` | Unused IP in your subnet |

> **Note:** When using `--network` with a fixed IP, you don't need `-p 8080:8080` — access the web UI directly at `http://192.168.1.50:8080`

### Intel iGPU - Complete Example (Docker Compose)

Save as `docker-compose.yml` in `/mnt/user/appdata/plex-generate-previews/`:

```yaml
services:
  plex-generate-previews:
    image: stevezzau/plex_generate_vid_previews:latest
    container_name: plex-generate-previews
    restart: unless-stopped
    ports:
      - "8080:8080"
    devices:
      # Intel GPU passthrough for VAAPI/QuickSync acceleration
      - /dev/dri:/dev/dri
    environment:
      # Web UI authentication - set your own password!
      - WEB_AUTH_TOKEN=my-secret-password
      # Unraid permissions
      - PUID=99
      - PGID=100
    volumes:
      # Your media library (same path as Plex = no mapping needed)
      - /mnt/user/data/plex:/data/plex:ro
      # Plex config folder (for writing BIF files)
      - /mnt/cache/appdata/plex/Library/Application Support/Plex Media Server:/plex:rw
      # App settings persistence
      - /mnt/user/appdata/plex-generate-previews:/config:rw
```

Start with:
```bash
cd /mnt/user/appdata/plex-generate-previews
docker-compose up -d
```

### NVIDIA GPU

Requires: Nvidia-Driver plugin from Community Applications

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

> **Note:** Complete Plex authentication via the Setup Wizard after starting the container.

### CPU-Only

```bash
docker run -d \
  --name plex-generate-previews \
  --restart unless-stopped \
  -p 8080:8080 \
  -e WEB_AUTH_TOKEN=my-secret-password \
  -e PUID=99 \
  -e PGID=100 \
  -e GPU_THREADS=0 \
  -e CPU_THREADS=8 \
  -v /mnt/user/data/plex:/data/plex:ro \
  -v "/mnt/cache/appdata/plex/Library/Application Support/Plex Media Server":/plex:rw \
  -v /mnt/user/appdata/plex-generate-previews:/config:rw \
  stevezzau/plex_generate_vid_previews:latest
```

> **Note:** Complete Plex authentication via the Setup Wizard after starting the container.

---

## Important Unraid Notes

### Network Considerations

When completing the Setup Wizard, select your Plex server from the dropdown. If using a local server, make sure the container can reach it (not `localhost` from Unraid's perspective).

### PUID/PGID Values

Unraid uses `nobody:users` by default:

| Variable | Value | Description |
|----------|-------|-------------|
| `PUID` | `99` | nobody user |
| `PGID` | `100` | users group |

### Check Intel GPU Exists

```bash
ls -la /dev/dri
# Should show: card0, renderD128
```

---

## Path Mapping for Plex

Path mapping is only needed when this container mounts media to a **different path** than Plex uses.

### Option 1: No Mapping Needed (Recommended)

Mount media to the **same container path** as Plex:

```
Plex:           /mnt/user/data/plex → /data/plex
This container: /mnt/user/data/plex → /data/plex  ← Same path!
```

Both see `/data/plex/Movies/film.mkv` — no mapping required.

### Option 2: With Path Mapping

If you prefer mounting to `/media` instead:

```
Plex:           /mnt/user/data/plex → /data/plex
This container: /mnt/user/data/plex → /media      ← Different path
```

Plex sees: `/data/plex/Movies/film.mkv`  
This container sees: `/media/Movies/film.mkv`

Add path mapping:
```bash
-e PLEX_VIDEOS_PATH_MAPPING=/data/plex \
-e PLEX_LOCAL_VIDEOS_PATH_MAPPING=/media \
```

See [Path Mappings Guide](configuration.md#path-mappings) for more examples.

---

## Access Web Interface

After starting the container:

1. Open `http://YOUR_UNRAID_IP:8080`
2. Login with your `WEB_AUTH_TOKEN` (or check logs if not set):
   ```bash
   docker logs plex-generate-previews | grep "Token:"
   ```
3. **First-time users:** Complete the Setup Wizard to sign in with Plex
4. **Returning users:** Access the dashboard directly

---

## TRaSH Guide Folder Structure

For users following [TRaSH Guides](https://trash-guides.info/):

### Configure Plex Container

Add a second container path:
- Container path: `/server/media/plex/`
- Host path: `/mnt/user/media/plex/`

### Update Plex Libraries

Update library paths to use the new mapping:
- Format: `//server/media/plex/<media-folder>`
- Example: `//server/media/plex/tv`

### Set Permissions

```bash
chmod -R 777 /mnt/cache/appdata/plex/Library/Application\ Support/Plex\ Media\ Server/Media/
```

---

## Troubleshooting

### "File not found" errors

Check your volume mounts and path mappings. See [Path Mappings Guide](configuration.md#path-mappings).

### GPU not detected

Verify `/dev/dri` exists and is passed through:
```bash
ls -la /dev/dri
docker exec plex-generate-previews ls -la /dev/dri
```

### Permission errors

Ensure PUID/PGID match your Unraid user:
```bash
id  # Shows your UID/GID
```

---

[Back to Docs](README.md) | [Main README](../README.md)
