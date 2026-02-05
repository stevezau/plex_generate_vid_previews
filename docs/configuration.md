# Configuration Reference

> [Back to Docs](README.md)

Complete reference for all configuration options. Settings can be configured via the **web-based Settings page**, environment variables, or CLI arguments.

---

## Configuration Priority

Settings are applied in this order (highest priority first):

1. **CLI arguments** - Override everything
2. **Web UI / Settings page** - Saved to `/config/settings.json`
3. **Environment variables** - Fallback when not set in UI
4. **Default values** - Used when nothing is configured

> **Tip:** Most settings can now be configured via the web interface at `http://your-server:8080/settings`. No need to restart the container when changing settings!

---

## Plex Connection Settings

These are configured automatically via the **Setup Wizard** using Plex OAuth, but can also be set manually:

| Variable | CLI Argument | Web UI | Description |
|----------|--------------|--------|-------------|
| `PLEX_URL` | `--plex-url` | ✅ | Plex server URL (e.g., `http://192.168.1.100:32400`) |
| `PLEX_TOKEN` | `--plex-token` | ✅ | Plex authentication token (auto-set via OAuth) |
| `PLEX_CONFIG_FOLDER` | `--plex-config-folder` | ✅ | Path to Plex config folder |

> **Tip:** Use the Setup Wizard to sign in with Plex OAuth. Your token is obtained securely without manually copying it.

---

## Processing Options

| Variable | CLI Argument | Web UI | Default | Description |
|----------|--------------|--------|---------|-------------|
| `GPU_THREADS` | `--gpu-threads` | ✅ | `4` | Number of GPU worker threads (0-32) |
| `CPU_THREADS` | `--cpu-threads` | ✅ | `4` | Number of CPU worker threads (0-32) |
| `GPU_SELECTION` | `--gpu-selection` | ❌ | `all` | GPU selection: `all` or `0,1,2` |
| `THUMBNAIL_QUALITY` | `--thumbnail-quality` | ✅ | `4` | Preview quality 1-10 (2=highest) |
| `PLEX_BIF_FRAME_INTERVAL` | `--plex-bif-frame-interval` | ✅ | `5` | Interval between preview images (1-60 sec) |
| `REGENERATE_THUMBNAILS` | `--regenerate-thumbnails` | ❌ | `false` | Regenerate existing thumbnails |
| `PLEX_LIBRARIES` | `--plex-libraries` | ✅ | All | Comma-separated library names or IDs |
| `SORT_BY` | `--sort-by` | ❌ | `newest` | Sort order: `newest` or `oldest` |
| `NICE_LEVEL` | N/A | ❌ | `15` | Process priority (0-19) |

---

## Web Interface

| Variable | Default | Description |
|----------|---------|-------------|
| `WEB_PORT` | `8080` | Web server port |
| `WEB_AUTH_TOKEN` | Auto-generated | Fixed auth token (check logs if not set) |
| `WEB_HIDE_TOKEN` | `false` | Set `true` to hide token from logs |
| `FLASK_SECRET_KEY` | Auto-generated | Session secret (persisted to `/config/flask_secret.key`) |
| `CORS_ORIGINS` | `*` | Allowed CORS origins (comma-separated) |
| `RATELIMIT_STORAGE_URL` | In-memory | Redis URL for multi-worker rate limiting |

---

## Docker/Permissions

| Variable | Default | Description |
|----------|---------|-------------|
| `PUID` | `1000` | User ID (Unraid: `99`) |
| `PGID` | `1000` | Group ID (Unraid: `100`) |

---

## System

| Variable | CLI Argument | Default | Description |
|----------|--------------|---------|-------------|
| `PLEX_TIMEOUT` | `--plex-timeout` | `60` | Plex API timeout in seconds |
| `TMP_FOLDER` | `--tmp-folder` | System temp | Temporary folder for processing |
| `LOG_LEVEL` | `--log-level` | `INFO` | Logging level: DEBUG, INFO, WARNING, ERROR |
| `DEBUG` | N/A | `false` | Enable debug mode |

---

## Special Commands

| Command | Description |
|---------|-------------|
| `--list-gpus` | List detected GPUs and exit |
| `--help` | Show help message and exit |
| `--cli` | Run in CLI mode (instead of web server) |

---

## Example: Minimal Docker Setup

With the web-based setup wizard, you only need to provide volume mounts:

```bash
docker run -d \
  --name plex-generate-previews \
  -p 8080:8080 \
  --device /dev/dri:/dev/dri \
  -v /path/to/media:/media:ro \
  -v /path/to/plex/config:/plex:rw \
  -v /path/to/app/config:/config:rw \
  stevezzau/plex_generate_vid_previews:latest
```

Then complete configuration in the web UI.

---

## Example: Environment File (Advanced)

For users who prefer environment variables or need headless setup:

```bash
PLEX_URL=http://192.168.1.100:32400
PLEX_TOKEN=your-token-here
PLEX_CONFIG_FOLDER=/plex/Library/Application Support/Plex Media Server
GPU_THREADS=4
CPU_THREADS=4
THUMBNAIL_QUALITY=4
PUID=1000
PGID=1000
```

---

## Path Mappings

> Essential for Docker deployments where Plex sees files at different paths.

### Why Path Mappings?

| Component | Sees Files At |
|-----------|---------------|
| Plex Container | `/data/media/Movies/film.mkv` |
| This Container | `/media/Movies/film.mkv` |

Without mapping, you'll see "Skipping as file not found" errors.

### Configuration

| Variable | CLI Argument | Description |
|----------|--------------|-------------|
| `PLEX_VIDEOS_PATH_MAPPING` | `--plex-videos-path-mapping` | Path as Plex sees it |
| `PLEX_LOCAL_VIDEOS_PATH_MAPPING` | `--plex-local-videos-path-mapping` | Path as container sees it |

### Common Examples

| Setup | PLEX_VIDEOS_PATH_MAPPING | PLEX_LOCAL_VIDEOS_PATH_MAPPING |
|-------|--------------------------|--------------------------------|
| linuxserver/plex | `/data/media` | `/media` |
| Unraid share | `/mnt/user/media` | `/media` |
| Windows share | `\\\\server\\media` | `/media` |

### How to Find Your Paths

1. **Plex path**: Plex Web → Settings → Libraries → Edit → Folders
2. **Container path**: Check your `-v` volume mount

### No Mapping Needed

If both Plex and this container see files at the same path (e.g., both use `/media`), skip this configuration.

---

[Back to Docs](README.md) | [Main README](../README.md)
