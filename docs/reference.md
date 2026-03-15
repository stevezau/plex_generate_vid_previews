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

1. **CLI arguments** — override everything (used when running with `--cli`)
2. **Web UI / Settings page** — saved to `/config/settings.json`
3. **Environment variables** — fallback when not set in UI
4. **Default values** — used when nothing is configured

For normal Docker use, configure everything in the **Setup Wizard** and **Settings**; no environment variables are required. Use env vars only when you need to override UI settings or run in CLI mode without an existing `/config`.

---

## Plex Connection

Configured via the **Setup Wizard** (Plex OAuth) or the **Settings** page. Env vars are only needed if you skip the UI (e.g. CLI-only runs).

| Variable | CLI Argument | Web UI | Description |
|----------|--------------|--------|-------------|
| `PLEX_URL` | `--plex-url` | Yes | Plex server URL (e.g., `http://192.168.1.100:32400`) |
| `PLEX_TOKEN` | `--plex-token` | Yes | Plex authentication token (auto-set via OAuth) |
| `PLEX_CONFIG_FOLDER` | `--plex-config-folder` | Yes | Path to Plex config folder |

> [!TIP]
> Use the Setup Wizard to sign in with Plex OAuth. Your token is obtained securely without manually copying it.

---

## Processing Options
<a id="cpu-fallback-workers"></a>

| Variable | CLI Argument | Web UI | Default | Description |
|----------|--------------|--------|---------|-------------|
| `GPU_THREADS` | `--gpu-threads` | Yes | `1` | Number of GPU worker threads (0–32) |
| `CPU_THREADS` | `--cpu-threads` | Yes | `1` | Number of CPU worker threads (0–32) |
| `FALLBACK_CPU_THREADS` | `--fallback-cpu-threads` | Yes | `0` | CPU fallback workers for GPU failures (0–32, used when `CPU_THREADS=0`) |
| `FFMPEG_THREADS` | `--ffmpeg-threads` | Yes | `2` | Limits CPU usage per GPU job (0–32, 0 = no limit). Recommended: 2 |
| `GPU_SELECTION` | `--gpu-selection` | No | `all` | GPU selection: `all` or `0,1,2` |
| `THUMBNAIL_QUALITY` | `--thumbnail-quality` | Yes | `4` | Preview quality 1-10 (2=highest) |
| `PLEX_BIF_FRAME_INTERVAL` | `--plex-bif-frame-interval` | Yes | `5` | Interval between preview images (1–60 s) |
| `REGENERATE_THUMBNAILS` | `--regenerate-thumbnails` | No | `false` | Regenerate existing thumbnails |
| `PLEX_LIBRARIES` | `--plex-libraries` | Yes | All | Comma-separated library names or IDs |
| `SORT_BY` | `--sort-by` | No | `newest` | Sort order: `newest` or `oldest` |
| `NICE_LEVEL` | N/A | No | `15` | Process priority (0–19) |

> [!TIP]
> For GPU-first processing with CPU safety net:
> set `CPU_THREADS=0` and `FALLBACK_CPU_THREADS>0`.
> This prevents regular CPU main-queue work while still allowing GPU-failed items to be retried on CPU.

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
| `TZ` | Host | Timezone (e.g. `America/New_York`). Alternative to mounting `/etc/localtime:/etc/localtime:ro` |

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
| `webhook_delay` | `60` | Yes | Delay before processing (10–300 s). Incoming webhooks are queued per source; a batch runs only after this many seconds with no new imports, so every file gets at least this long for Plex to add it before we process. |
| `webhook_secret` | *(empty)* | Yes | Dedicated secret for webhook auth (falls back to API token) |

Webhook processing respects `selected_libraries`; paths outside unchecked libraries are ignored.

> [!TIP]
> Configure webhooks via the **Webhooks** page in the web UI. See [Webhook Integration](guides.md#webhook-integration) for setup instructions.

---

## Special Commands (Docker CLI mode)

When running the container with `--cli`, you can pass:

| Command | Description |
|---------|-------------|
| `--help` | Show help message and exit |
| `--cli` | Run in CLI mode (instead of web server) |

To see detected GPUs, use the web UI: open **Settings** or **Setup**.

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

### Configuration (Web UI)

In **Settings** and **Setup**, you add mapping rows. Each row has:

- **Path in Plex** — The folder path Plex uses for the media (e.g. `/data`).
- **Path in this app** — The folder path this app uses for the same files (e.g. `/mnt/data`).
- **Path from Sonarr/Radarr (if different)** — Only if Sonarr/Radarr report a different path than Plex (e.g. they use `/data` while Plex uses `/data_disk1`). You can leave this blank if they match.

Add as many rows as you need (e.g. one per disk when Plex uses multiple roots).

### Legacy env/CLI (semicolon pair)

| Variable | CLI Argument | Description |
|----------|--------------|-------------|
| `PLEX_VIDEOS_PATH_MAPPING` | `--plex-videos-path-mapping` | Path(s) as Plex sees it; semicolon-separated for multiple roots |
| `PLEX_LOCAL_VIDEOS_PATH_MAPPING` | `--plex-local-videos-path-mapping` | Path as this app sees it (one value, or semicolon-separated to pair by index) |

If you use the Web UI, the saved **path_mappings** take precedence. Existing semicolon-based values are converted to mapping rows when loading.

### When Plex uses multiple roots (e.g. mergerfs)

If Plex has several roots (e.g. `/data_disk1`, `/data_disk2`) but Sonarr/Radarr see one path (`/data`):

- Add one row per Plex root, each with the same **Path in this app** (e.g. `/data`).
- In **Path from Sonarr/Radarr**, enter `/data` on one of the rows so imports from Sonarr/Radarr still match.

### Examples

| Situation | Path in Plex | Path in this app | Path from Sonarr/Radarr |
|-----------|--------------|------------------|--------------------------|
| Different paths in Docker | `/data` | `/mnt/data` | *(blank)* |
| Multiple disks, Sonarr sees one path | `/data_disk1` | `/data` | `/data` |
| Same (second disk) | `/data_disk2` | `/data` | *(blank)* |

### How to Find Your Paths

1. **Plex path**: Plex Web → Settings → Libraries → Edit → Folders
2. **Container path**: Check your `-v` volume mount

### No Mapping Needed

If both Plex and this container see files at the same path (e.g., both use `/media`), skip this configuration.

### Exclude Paths

Under the same **Media path mapping** settings you can add **Exclude paths**: paths or folders to skip for preview generation. These are applied to the **local** path (as this app sees the file after path mapping).

- **Path prefix** — Any file under this folder is skipped (e.g. `/mnt/media/archive` skips everything under that path).
- **Regex** — The full local path is matched against the pattern (e.g. `.*\.iso$` to skip ISO files).

Add one row per path or pattern. Excluded items are not queued for full-library runs and are skipped for webhook-triggered runs.

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
  "path_mappings": [
    {"plex_prefix": "/data", "local_prefix": "/mnt/data", "webhook_prefixes": []}
  ],
  "gpu_threads": 4,
  "cpu_threads": 2,
  "cpu_fallback_threads": 0,
  "thumbnail_interval": 5,
  "thumbnail_quality": 4
}
```

### POST /api/settings

Update settings. Send only the fields to change.

```json
{
  "gpu_threads": 4,
  "cpu_fallback_threads": 1,
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

## Processing state (global pause)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/processing/state` | Get global processing pause state |
| POST | `/api/processing/pause` | Set global pause (no new jobs start; active job stops dispatch after current tasks) |
| POST | `/api/processing/resume` | Clear global pause |

**GET /api/processing/state** — Response: `{"paused": true}` or `{"paused": false}`. State is persisted and survives restarts.

**POST /api/processing/pause** — Response: `{"paused": true}`.

**POST /api/processing/resume** — Response: `{"paused": false}`.

## Jobs Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/jobs` | List all jobs |
| POST | `/api/jobs` | Create new job |
| GET | `/api/jobs/{id}` | Get job details |
| POST | `/api/jobs/{id}/cancel` | Cancel job |
| POST | `/api/jobs/{id}/pause` | Global pause (delegates to `/api/processing/pause`) |
| POST | `/api/jobs/{id}/resume` | Global resume (delegates to `/api/processing/resume`) |
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

Inbound webhook endpoints for Radarr/Sonarr/Custom integration. Webhook endpoints accept `X-Auth-Token`, `Authorization: Bearer`, or a configured `webhook_secret`.

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

### POST /api/webhooks/custom

Receive a custom webhook payload from any external tool (Tdarr, scripts, etc.). Accepts one or more file paths to process.

**Single file request:**

```json
{
  "file_path": "/media/movies/Movie (2024)/Movie.mkv"
}
```

**Multiple files request:**

```json
{
  "file_paths": [
    "/media/tv/Show/Season 01/S01E01.mkv",
    "/media/tv/Show/Season 01/S01E02.mkv"
  ],
  "title": "Optional display label"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `file_path` | string | One of `file_path` / `file_paths` | Single absolute file path |
| `file_paths` | array of strings | One of `file_path` / `file_paths` | Multiple absolute file paths |
| `title` | string | No | Display label for history/jobs |
| `eventType` | string | No | Set to `"Test"` to verify connectivity |

**Response (202):** `{"success": true, "message": "Processing queued for 1 file"}`

**Test event:** `{"eventType": "Test"}` → **Response (200):** `{"success": true, "message": "Custom webhook configured successfully"}`

**Error (400):** `{"success": false, "error": "Payload must include 'file_path' (string) or 'file_paths' (array of strings)"}`

### GET /api/webhooks/history

Get recent webhook events (newest first, max 100). For events with `status: "triggered"` (a debounced batch that was processed), the response may include `job_id`, `path_count`, and `files_preview` (up to 20 basenames) so the UI can show which files were in the batch. File lists are also available on the Dashboard job queue (expand with the chevron next to "Sonarr: N files" / "Radarr: N files" / "Custom: N files") and on the Webhooks page Recent Activity (expand triggered rows).

```json
{
  "events": [
    {
      "timestamp": "2026-02-12T10:30:00+00:00",
      "source": "sonarr",
      "event_type": "Download",
      "title": "sonarr",
      "status": "triggered",
      "job_id": "abc-123",
      "path_count": 3,
      "files_preview": ["S01E01.mkv", "S01E02.mkv", "S01E03.mkv"]
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

The dashboard uses Flask-SocketIO with WebSocket for real-time updates. The client connects to the `/jobs` namespace.

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
