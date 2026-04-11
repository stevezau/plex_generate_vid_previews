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

**settings.json** (at `/config/settings.json`) is the sole source of truth for application configuration. On first start, environment variables are migrated into settings.json as seed values. After that, all configuration is managed via the **Web UI** (Setup Wizard and Settings page).

**Infrastructure environment variables** remain active and are not migrated (see [Infrastructure Variables](#infrastructure-variables)).

---

## Plex Connection

Configured via the **Setup Wizard** (Plex OAuth) or the **Settings** page. Values are stored in settings.json (seeded from env vars on first start).

| Setting | Web UI | Description |
|---------|--------|-------------|
| `plex_url` | Yes | Plex server URL (e.g., `http://192.168.1.100:32400`) |
| `plex_token` | Yes | Plex authentication token (auto-set via OAuth) |
| `plex_config_folder` | Yes | Path to Plex config folder |

> [!TIP]
> Use the Setup Wizard to sign in with Plex OAuth. Your token is obtained securely without manually copying it.

---

## Processing Options
<a id="cpu-fallback-workers"></a>

### Per-GPU Configuration (gpu_config)

GPU settings are configured per-GPU in **Settings** → **Processing Options**. Each entry in `gpu_config` has:

| Field | Type | Description |
|-------|------|-------------|
| `device` | string | GPU device identifier (e.g. `/dev/dri/renderD128`) |
| `name` | string | Display name (e.g. "Intel UHD Graphics 630") |
| `type` | string | `nvidia`, `intel`, `amd`, `apple` |
| `enabled` | boolean | Whether this GPU is used for processing |
| `workers` | int | Number of worker threads for this GPU (0–32) |
| `ffmpeg_threads` | int | CPU threads per FFmpeg job on this GPU (0–32, 0 = no limit). Recommended: 2 |

### Other Processing Settings

| Setting | Web UI | Default | Description |
|---------|--------|---------|-------------|
| `cpu_threads` | Yes | `1` | Number of CPU worker threads (0–32) |
| `cpu_fallback_threads` | Yes | `0` | CPU fallback workers for GPU failures (0–32, used when `cpu_threads=0`) |
| `thumbnail_quality` | Yes | `4` | Preview quality 1-10 (2=highest) |
| `thumbnail_interval` | Yes | `5` | Interval between preview images (1–60 s) |
| `selected_libraries` | Yes | All | Library IDs to process |

> [!TIP]
> For GPU-first processing with CPU safety net:
> set `cpu_threads=0` and `cpu_fallback_threads>0`.
> This prevents regular CPU main-queue work while still allowing GPU-failed items to be retried on CPU.

---

## Environment Variables

### Infrastructure Variables (always active) <a id="infrastructure-variables"></a>

These are not migrated to settings.json and remain in effect:

| Variable | Default | Description |
|----------|---------|-------------|
| `CONFIG_DIR` | `/config` | Directory for settings.json, auth, schedules |
| `WEB_PORT` | `8080` | Web server port |
| `PUID` | `1000` | User ID (Unraid: `99`) |
| `PGID` | `1000` | Group ID (Unraid: `100`) |
| `TZ` | Host | Timezone (e.g. `America/New_York`) |
| `CORS_ORIGINS` | `*` | Allowed CORS origins (comma-separated) |
| `HTTPS` | `false` | Enable HTTPS for cookies |
| `DEV_RELOAD` | `false` | Enable Flask auto-reload (development) |
| `WEB_AUTH_TOKEN` | Auto-generated | Fixed authentication token (overrides wizard-set token) |
| `AUTH_METHOD` | `internal` | Set to `external` to disable built-in auth when using a reverse proxy or VPN (see below) |

### External Authentication (AUTH_METHOD)

If you secure access via a reverse proxy (Authelia, Authentik, Caddy Security, nginx basic auth, etc.) or a VPN (Tailscale, WireGuard), you can disable the built-in login screen:

```yaml
environment:
  - AUTH_METHOD=external
```

When set to `external`:

- The login page is bypassed; all browser and API requests are treated as authenticated.
- Webhook authentication (`webhook_secret` / Bearer token) is **not** affected — external services like Radarr and Sonarr still need their shared secret.
- The setup wizard still runs on first boot.
- Removing the variable (or setting it back to `internal`) instantly re-enables built-in auth.

> [!CAUTION]
> Only use `AUTH_METHOD=external` when you are certain that network-level access control is in place. Without it, anyone who can reach the web UI has full access.

### Deprecated (no longer used)

These env vars are deprecated. Configure via **Settings** instead:

| Variable | Replacement |
|----------|--------------|
| `GPU_SELECTION` | Per-GPU enable/disable in Settings → Processing Options |
| `GPU_THREADS` | Per-GPU workers in `gpu_config` |
| `FFMPEG_THREADS` | Per-GPU `ffmpeg_threads` in `gpu_config` |

### One-time seed values (migrated on first start)

On first run, these env vars are migrated into settings.json. After that, settings.json is the source of truth:

- `PLEX_URL`, `PLEX_TOKEN`, `PLEX_CONFIG_FOLDER`, `PLEX_VERIFY_SSL`, `PLEX_TIMEOUT`
- `PLEX_BIF_FRAME_INTERVAL`, `THUMBNAIL_QUALITY`, `CPU_THREADS`, `FALLBACK_CPU_THREADS`
- `MEDIA_PATH`, `TMP_FOLDER`, `LOG_LEVEL`

---

## Web Interface Settings

The web server uses **gunicorn** with **gthread** workers in production (Docker). `WEB_PORT`, `CORS_ORIGINS`, `HTTPS`, and `DEV_RELOAD` are infrastructure variables (see above).

---

## Webhook Settings

Settings for automatic preview generation when media is imported via Radarr or Sonarr.

| Setting | Default | Web UI | Description |
|---------|---------|--------|-------------|
| `webhook_enabled` | `true` | Yes | Master enable/disable for webhook processing |
| `webhook_delay` | `60` | Yes | Delay before processing (10–300 s). Incoming webhooks are queued per source; a batch runs only after this many seconds with no new imports, so every file gets at least this long for Plex to add it before we process. |
| `webhook_secret` | *(empty)* | Yes | Dedicated secret for webhook auth (falls back to API token) |
| `plex_webhook_enabled` | `false` | Yes | Enable the Plex direct webhook (`/api/webhooks/plex`). Requires Plex Pass on the server-owner account. |
| `plex_webhook_public_url` | *(empty)* | Yes | URL Plex Media Server should POST to. Defaults to the URL you registered through. Override for reverse-proxy / split-network setups. |

Webhook processing respects `selected_libraries`; paths outside unchecked libraries are ignored.

The **Recently Added Scanner** is not configured via settings keys any more — it's a first-class schedule type (see [Schedules](#post-apischedules) below). Create one through the Webhooks page's "Create default scanner" shortcut, or through the Schedules modal with **Scan mode → Recently added only**.

> [!IMPORTANT]
> The Plex direct webhook and Recently Added schedules trigger only on **new** library items (new `ratingKey`s). They do **not** detect in-place file upgrades — Plex keeps the same item when Sonarr/Radarr replaces a file. Use the existing Sonarr/Radarr webhooks (which fire on `On Upgrade`) for that case.

> [!TIP]
> Configure webhooks via the **Webhooks** page in the web UI. See [Webhook Integration](guides.md#webhook-integration) for setup instructions.

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
- **Webhook path (if different)** — Only needed when Sonarr, Radarr, Tdarr, etc. use a different path than Plex (e.g. they use `/data` while Plex uses `/data_disk1`). Leave blank if they match.

Add as many rows as you need (e.g. one per disk when Plex uses multiple roots).

### Legacy env (semicolon pair)

| Variable | Description |
|----------|-------------|
| `PLEX_VIDEOS_PATH_MAPPING` | Path(s) as Plex sees it; semicolon-separated for multiple roots (seed value) |
| `PLEX_LOCAL_VIDEOS_PATH_MAPPING` | Path as this app sees it (seed value) |

The saved **path_mappings** in settings.json take precedence. Existing semicolon-based values are converted to mapping rows when migrated.

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
  "gpu_config": [
    {"device": "/dev/dri/renderD128", "name": "Intel UHD 630", "type": "intel", "enabled": true, "workers": 4, "ffmpeg_threads": 2}
  ],
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
  "gpu_config": [{"device": "/dev/dri/renderD128", "enabled": true, "workers": 4, "ffmpeg_threads": 2}],
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

**Cron request — full library scan (default):**

```json
{
  "name": "Nightly Movies",
  "library_id": "1",
  "cron_expression": "0 2 * * *"
}
```

**Interval request — full library scan:**

```json
{
  "name": "Every 4 Hours",
  "library_id": "1",
  "interval_minutes": 240
}
```

**Recently Added scanner schedule:**

```json
{
  "name": "Recently Added Scanner",
  "library_id": null,
  "interval_minutes": 15,
  "enabled": true,
  "config": {
    "job_type": "recently_added",
    "lookback_hours": 1
  }
}
```

`config.job_type` accepts:

- `"full_library"` *(default — optional, omit to get the same behaviour)* — schedule runs a full library scan via the standard job pipeline, processing every item in `library_id` that's missing previews.
- `"recently_added"` — schedule runs a Recently Added scan instead. Requires `config.lookback_hours` (float, clamped to 0.25–720). Scans only items whose Plex `addedAt` falls within the lookback window, queuing each through the webhook job pipeline. When `library_id` is `null`, the scan falls back to the globally selected libraries in Settings (or every supported library when no global filter is set); when set, only that section is scanned.

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

### POST /api/webhooks/plex

Receive a native Plex webhook (Plex Pass feature). Plex POSTs `multipart/form-data` with a `payload` part containing the JSON event body. Only `library.new` events trigger work; other events (`media.play`, `media.rate`, `library.on.deck`, etc.) are acknowledged with 200 and ignored.

The endpoint also accepts a synthetic `test.ping` event used by the **Test reachability** button on the Webhooks page.

**`library.new` payload (excerpt):**

```json
{
  "event": "library.new",
  "owner": true,
  "Metadata": {
    "ratingKey": "153037",
    "type": "movie",
    "title": "Some Movie",
    "Media": [{ "Part": [{ "file": "/data/movies/Some Movie/Some Movie.mkv" }] }]
  }
}
```

When `Media[].Part[].file` is missing from the payload (Plex doesn't always include it), the app fetches the item by `ratingKey` via the Plex API to recover the file paths.

**Authentication:** same as the other webhook endpoints — `X-Auth-Token` header, `Authorization: Bearer`, or HTTP Basic password.

> [!IMPORTANT]
> Plex's `library.new` webhook is wired through the same code path as mobile push notifications. If push notifications are disabled on your Plex server, library events are silently dropped — enable them under Plex Web → Settings → Server → Notifications. See the [Auto-trigger from Plex guide](guides.md#auto-trigger-from-plex-no-sonarrradarr) for full details.

### POST /api/settings/plex_webhook/register

Register the Plex direct webhook (`/api/webhooks/plex`) with the user's plex.tv account, using the configured Plex token.

**Request body:**

```json
{ "public_url": "http://your-host:8080/api/webhooks/plex" }
```

`public_url` is optional — when omitted the server uses `<request scheme>://<host>/api/webhooks/plex`.

**Response (200):** `{"success": true, "registered_in_plex": true, "public_url": "..."}`

**Errors:**
- `400` — token missing
- `403` — Plex Pass required (`reason: "plex_pass_required"`)
- `502` — registration call to plex.tv failed

### POST /api/settings/plex_webhook/unregister

Remove the Plex direct webhook from the user's plex.tv account and turn off the local toggle. Returns `{"success": true, "registered_in_plex": false}`.

### GET /api/settings/plex_webhook/status

Probe live state. Returns the configured public URL, whether it is currently registered with Plex, and Plex Pass detection.

```json
{
  "enabled_in_settings": true,
  "registered_in_plex": true,
  "public_url": "http://your-host:8080/api/webhooks/plex",
  "default_url": "http://your-host:8080/api/webhooks/plex",
  "has_plex_pass": true,
  "error": null,
  "error_reason": null
}
```

### POST /api/settings/plex_webhook/test

Self-POST a synthetic `test.ping` payload to the configured public URL to verify reachability. The receiving endpoint records a "test" history entry. Returns `{"success": true, "status_code": 200, ...}` on success.

To run a Recently Added scan immediately, call `POST /api/schedules/<id>/run` on the scanner schedule — it's a standard user schedule now, not a dedicated settings endpoint.

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
