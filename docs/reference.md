# Configuration & API Reference

> [Back to Docs](README.md)

Complete reference for all configuration options and REST API endpoints.

> [!IMPORTANT]
> This page is the source of truth for configuration precedence, settings, and REST API behavior.
> For installation and setup flows, use [Getting Started](getting-started.md).
> For operations, webhooks, and troubleshooting, use [Guides & Troubleshooting](guides.md).

## Related Docs

- [Getting Started](getting-started.md)
- [Guides & Troubleshooting](guides.md)
- [Main README](../README.md)

---

## Configuration Priority

Settings are applied in this order (highest priority first):

1. **CLI arguments** — override everything
2. **Web UI / Settings page** — saved to `/config/settings.json`
3. **Environment variables** — fallback when not set in UI
4. **Default values** — used when nothing is configured

> [!TIP]
> Most settings can be configured via the web interface at `http://your-server:8080/settings`. No need to restart the container when changing settings!

---

## Plex Connection

Configured automatically via the **Setup Wizard** using Plex OAuth, but can also be set manually:

| Variable | CLI Argument | Web UI | Description |
|----------|--------------|--------|-------------|
| `PLEX_URL` | `--plex-url` | Yes | Plex server URL (e.g., `http://192.168.1.100:32400`) |
| `PLEX_TOKEN` | `--plex-token` | Yes | Plex authentication token (auto-set via OAuth) |
| `PLEX_CONFIG_FOLDER` | `--plex-config-folder` | Yes | Path to Plex config folder |

> [!TIP]
> Use the Setup Wizard to sign in with Plex OAuth. Your token is obtained securely without manually copying it.

---

## Processing Options

| Variable | CLI Argument | Web UI | Default | Description |
|----------|--------------|--------|---------|-------------|
| `GPU_THREADS` | `--gpu-threads` | Yes | `1` | Number of GPU worker threads (0–32) |
| `CPU_THREADS` | `--cpu-threads` | Yes | `1` | Number of CPU worker threads (0–32) |
| `GPU_SELECTION` | `--gpu-selection` | No | `all` | GPU selection: `all` or `0,1,2` |
| `THUMBNAIL_QUALITY` | `--thumbnail-quality` | Yes | `4` | Preview quality 1-10 (2=highest) |
| `PLEX_BIF_FRAME_INTERVAL` | `--plex-bif-frame-interval` | Yes | `5` | Interval between preview images (1–60 s) |
| `REGENERATE_THUMBNAILS` | `--regenerate-thumbnails` | No | `false` | Regenerate existing thumbnails |
| `PLEX_LIBRARIES` | `--plex-libraries` | Yes | All | Comma-separated library names or IDs |
| `SORT_BY` | `--sort-by` | No | `newest` | Sort order: `newest` or `oldest` |
| `NICE_LEVEL` | N/A | No | `15` | Process priority (0–19) |

---

## Web Interface Settings

The web server uses **gunicorn** with **gthread** workers in production (Docker).

| Variable | Default | Description |
|----------|---------|-------------|
| `WEB_PORT` | `8080` | Web server port |
| `WEB_AUTH_TOKEN` | Auto-generated | Fixed authentication token (overrides wizard-set token) |
| `FLASK_SECRET_KEY` | Auto-generated | Session secret (persisted to `/config/flask_secret.key`) |
| `CORS_ORIGINS` | `*` (all) | Allowed CORS origins (comma-separated) |
| `RATELIMIT_STORAGE_URL` | In-memory | Redis URL for rate limiting across restarts |

---

## Docker/Permissions

| Variable | Default | Description |
|----------|---------|-------------|
| `PUID` | `1000` | User ID (Unraid: `99`) |
| `PGID` | `1000` | Group ID (Unraid: `100`) |

---

## System Settings

| Variable | CLI Argument | Default | Description |
|----------|--------------|---------|-------------|
| `PLEX_TIMEOUT` | `--plex-timeout` | `60` | Plex API timeout in seconds |
| `TMP_FOLDER` | `--tmp-folder` | System temp | Temporary folder for processing |
| `LOG_LEVEL` | `--log-level` | `INFO` | Logging level: DEBUG, INFO, WARNING, ERROR |
| `DEBUG` | N/A | `false` | Enable debug mode |

---

## Webhook Settings

Settings for automatic preview generation when media is imported via Radarr or Sonarr.

| Setting | Default | Web UI | Description |
|---------|---------|--------|-------------|
| `webhook_enabled` | `true` | Yes | Master enable/disable for webhook processing |
| `webhook_delay` | `60` | Yes | Seconds to wait after import before triggering (10–300 s) |
| `webhook_secret` | *(empty)* | Yes | Dedicated secret for webhook auth (falls back to API token) |
| `webhook_radarr_library` | *(empty)* | Yes | Library to scan for Radarr imports (empty = all) |
| `webhook_sonarr_library` | *(empty)* | Yes | Library to scan for Sonarr imports (empty = all) |

> [!TIP]
> Configure webhooks via the **Webhooks** page in the web UI. See [Webhook Integration](guides.md#webhook-integration) for setup instructions.

---

## Special Commands

| Command | Description |
|---------|-------------|
| `--list-gpus` | List detected GPUs and exit |
| `--help` | Show help message and exit |
| `--cli` | Run in CLI mode (instead of web server) |

---

## Path Mappings

> [!IMPORTANT]
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

## REST API

All API endpoints (except `/api/health` and `/api/setup/status`) require authentication.

### Authentication

Include the authentication token in requests using one of these methods:

```bash
# X-Auth-Token header
curl -H "X-Auth-Token: YOUR_TOKEN" http://localhost:8080/api/jobs

# Authorization Bearer header
curl -H "Authorization: Bearer YOUR_TOKEN" http://localhost:8080/api/jobs
```

Get your token from [Authentication Token](getting-started.md#authentication-token), or set a fixed token with `WEB_AUTH_TOKEN`.

## Setup & Settings Endpoints

### GET /api/setup/status

Check if setup is complete. **No authentication required.**

```json
{
  "configured": true,
  "setup_complete": true,
  "current_step": 0,
  "plex_authenticated": true
}
```

### GET /api/setup/state

Get current setup wizard state.

```json
{
  "step": 2,
  "data": {
    "server_name": "My Plex Server"
  }
}
```

### POST /api/setup/state

Save setup wizard progress.

**Request:**

```json
{
  "step": 2,
  "data": {
    "server_name": "My Plex Server"
  }
}
```

### POST /api/setup/complete

Mark setup as complete. Returns `{"success": true, "redirect": "/"}`.

### GET /api/setup/token-info

Get information about the current authentication token (used by Step 5 of the setup wizard).

```json
{
  "env_controlled": false,
  "token": "abc123xyz...",
  "token_length": 43,
  "source": "config"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `env_controlled` | boolean | Whether token is set via `WEB_AUTH_TOKEN` env var |
| `token` | string | The current authentication token |
| `token_length` | number | Length of the token |
| `source` | string | Either `"environment"` or `"config"` |

### POST /api/setup/set-token

Set a custom authentication token during setup.

**Request:**

```json
{
  "token": "my-custom-password",
  "confirm_token": "my-custom-password"
}
```

Returns `{"success": true}` on success, or `{"success": false, "error": "..."}` with details:

- `"Tokens do not match."`
- `"Token must be at least 8 characters long."`
- `"Token is controlled by WEB_AUTH_TOKEN environment variable and cannot be changed."`

### GET /api/settings

Get current settings.

```json
{
  "plex_url": "http://192.168.1.100:32400",
  "plex_token": "****",
  "plex_name": "My Server",
  "plex_config_folder": "/plex",
  "selected_libraries": ["1", "2"],
  "media_path": "/media",
  "plex_videos_path_mapping": "",
  "plex_local_videos_path_mapping": "",
  "gpu_threads": 4,
  "cpu_threads": 2,
  "thumbnail_interval": 5,
  "thumbnail_quality": 4
}
```

### POST /api/settings

Update settings. Send only the fields to change.

```json
{
  "gpu_threads": 4,
  "thumbnail_interval": 5,
  "plex_url": "http://192.168.1.100:32400"
}
```

## Plex OAuth Endpoints

### POST /api/plex/auth/pin

Create a new Plex OAuth PIN.

```json
{
  "id": 12345,
  "code": "ABCD1234",
  "auth_url": "https://app.plex.tv/auth#?clientID=...&code=ABCD1234"
}
```

### GET /api/plex/auth/pin/{id}

Check if PIN has been authenticated. Returns `{"authenticated": true, "auth_token": "..."}` or `{"authenticated": false, "auth_token": null}`.

### GET /api/plex/servers

Get list of user's Plex servers.

```json
{
  "servers": [
    {
      "name": "My Server",
      "machine_id": "abc123",
      "host": "192.168.1.100",
      "port": 32400,
      "ssl": false,
      "owned": true,
      "local": true
    }
  ]
}
```

### GET /api/plex/libraries

Get libraries from connected Plex server. Optional query parameters: `url`, `token`.

```json
{
  "libraries": [
    { "id": "1", "name": "Movies", "type": "movie" },
    { "id": "2", "name": "TV Shows", "type": "show" }
  ]
}
```

### POST /api/plex/test

Test Plex connection. Request: `{"url": "...", "token": "..."}`. Returns `{"success": true, "server_name": "...", "version": "..."}`.

## Jobs Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/jobs` | List all jobs |
| POST | `/api/jobs` | Create new job |
| GET | `/api/jobs/{id}` | Get job details |
| POST | `/api/jobs/{id}/cancel` | Cancel job |
| DELETE | `/api/jobs/{id}` | Delete job |

### GET /api/jobs

```json
{
  "jobs": [
    {
      "id": "job-123",
      "status": "running",
      "library_id": "1",
      "library_name": "Movies",
      "progress": 45,
      "total_items": 100,
      "completed_items": 45,
      "created_at": "2024-01-15T10:30:00Z",
      "started_at": "2024-01-15T10:30:05Z"
    }
  ]
}
```

### POST /api/jobs

**Request:** `{"library_id": "1", "library_name": "Movies"}`

**Response:** `{"id": "job-123", "status": "pending", "message": "Job created successfully"}`

### GET /api/jobs/{id}

```json
{
  "id": "job-123",
  "status": "running",
  "library_id": "1",
  "library_name": "Movies",
  "progress": 45,
  "total_items": 100,
  "completed_items": 45,
  "failed_items": 0,
  "created_at": "2024-01-15T10:30:00Z",
  "started_at": "2024-01-15T10:30:05Z",
  "workers": [
    {
      "id": 0,
      "type": "gpu",
      "status": "working",
      "current_item": "Movie Title"
    }
  ]
}
```

## Schedules Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/schedules` | List schedules |
| POST | `/api/schedules` | Create schedule |
| PUT | `/api/schedules/{id}` | Update schedule |
| DELETE | `/api/schedules/{id}` | Delete schedule |
| POST | `/api/schedules/{id}/run` | Run now |

### POST /api/schedules

**Cron request:**

```json
{
  "name": "Nightly Movies",
  "library_id": "1",
  "schedule_type": "cron",
  "cron_expression": "0 2 * * *"
}
```

**Interval request:**

```json
{
  "name": "Every 4 Hours",
  "library_id": "1",
  "schedule_type": "interval",
  "interval_minutes": 240
}
```

## System Endpoints

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/health` | No | Health check |
| GET | `/api/system/status` | Yes | System status (GPUs, workers, job counts) |
| GET | `/api/system/config` | Yes | Current configuration |
| GET | `/api/libraries` | Yes | Plex libraries |

## Webhook Endpoints

Inbound webhook endpoints for Radarr/Sonarr integration. Webhook endpoints accept `X-Auth-Token`, `Authorization: Bearer`, or a configured `webhook_secret`.

### POST /api/webhooks/radarr

Receive a Radarr webhook payload.

**Download event request:**

```json
{
  "eventType": "Download",
  "movie": {
    "title": "Inception",
    "folderPath": "/movies/Inception (2010)"
  }
}
```

**Response (202):** `{"success": true, "message": "Processing queued for 'Inception'"}`

**Test event:** `{"eventType": "Test"}` → **Response (200):** `{"success": true, "message": "Radarr webhook configured successfully"}`

### POST /api/webhooks/sonarr

Same authentication and response patterns as Radarr.

**Download event request:**

```json
{
  "eventType": "Download",
  "series": { "title": "Breaking Bad" },
  "episodeFile": { "relativePath": "Season 01/S01E01.mkv" }
}
```

### GET /api/webhooks/history

Get recent webhook events (newest first, max 100).

```json
{
  "events": [
    {
      "timestamp": "2026-02-12T10:30:00+00:00",
      "source": "radarr",
      "event_type": "Download",
      "title": "Inception",
      "status": "triggered"
    }
  ]
}
```

### DELETE /api/webhooks/history

Clear all webhook history. Returns `{"success": true}`.

## Error Responses

All errors follow this format:

```json
{
  "error": "Error message",
  "code": "ERROR_CODE"
}
```

| Code | HTTP Status | Description |
|------|-------------|-------------|
| `UNAUTHORIZED` | 401 | Missing or invalid authentication token |
| `NOT_FOUND` | 404 | Resource not found |
| `VALIDATION_ERROR` | 400 | Invalid request data |
| `SERVER_ERROR` | 500 | Internal server error |

---

## WebSocket Events

The dashboard uses WebSocket (via Flask-SocketIO + simple-websocket) for real-time updates. The client connects to the `/jobs` namespace.

```javascript
const socket = io('/jobs', {
    transports: ['websocket', 'polling'],
    reconnection: true
});
```

| Event | Description |
|-------|-------------|
| `job_progress` | Job progress update |
| `job_complete` | Job finished |
| `job_error` | Job failed |
| `worker_update` | Worker status change |

Example payload:

```json
{
  "event": "job_progress",
  "data": {
    "job_id": "job-123",
    "progress": 50,
    "completed": 50,
    "total": 100,
    "current_item": "Movie Title"
  }
}
```

---

## Rate Limiting

| Endpoint | Limit |
|----------|-------|
| `POST /login` | 5 per minute |
| `POST /api/auth/login` | 10 per minute |
| Default | 200 per day, 50 per hour |

Rate limit headers are included in responses:

- `X-RateLimit-Limit`
- `X-RateLimit-Remaining`
- `X-RateLimit-Reset`

---

## Next Steps

- Complete install and setup in [Getting Started](getting-started.md)
- Use operational workflows in [Guides & Troubleshooting](guides.md)

---

[Back to Docs](README.md) | [Main README](../README.md)
