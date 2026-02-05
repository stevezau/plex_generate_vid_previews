# API Reference

> [Back to Docs](docs/README.md)

Complete API documentation for all HTTP endpoints.

---

## Authentication

All API endpoints (except `/api/health` and `/api/setup/status`) require authentication.

### Token Authentication

Include the auth token in requests using one of these methods:

```bash
# X-Auth-Token header
curl -H "X-Auth-Token: YOUR_TOKEN" http://localhost:8080/api/jobs

# Authorization Bearer header
curl -H "Authorization: Bearer YOUR_TOKEN" http://localhost:8080/api/jobs
```

### Getting Your Token

```bash
# From container logs
docker logs plex-generate-previews | grep "Token:"

# Or set a fixed token via environment variable
WEB_AUTH_TOKEN=your-password
```

---

## Setup & Settings

### GET /api/setup/status

Check if setup is complete. **No authentication required.**

**Response:**
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

**Response:**
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

**Response:**
```json
{
  "success": true
}
```

### POST /api/setup/complete

Mark setup as complete.

**Response:**
```json
{
  "success": true,
  "redirect": "/"
}
```

### GET /api/setup/token-info

Get information about the current authentication token. Used by Step 5 of the setup wizard.

**Response:**
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

**Response (success):**
```json
{
  "success": true
}
```

**Response (error - tokens don't match):**
```json
{
  "success": false,
  "error": "Tokens do not match."
}
```

**Response (error - token too short):**
```json
{
  "success": false,
  "error": "Token must be at least 8 characters long."
}
```

**Response (error - env controlled):**
```json
{
  "success": false,
  "error": "Token is controlled by WEB_AUTH_TOKEN environment variable and cannot be changed."
}
```

> **Note:** This endpoint cannot change the token if `WEB_AUTH_TOKEN` environment variable is set.

### GET /api/settings

Get current settings.

**Response:**
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

Update settings.

**Request:**
```json
{
  "gpu_threads": 4,
  "thumbnail_interval": 5,
  "plex_url": "http://192.168.1.100:32400"
}
```

**Response:**
```json
{
  "success": true
}
```

---

## Plex OAuth

### POST /api/plex/auth/pin

Create a new Plex OAuth PIN.

**Response:**
```json
{
  "id": 12345,
  "code": "ABCD1234",
  "auth_url": "https://app.plex.tv/auth#?clientID=...&code=ABCD1234"
}
```

### GET /api/plex/auth/pin/{id}

Check if PIN has been authenticated.

**Response (not authenticated):**
```json
{
  "authenticated": false,
  "auth_token": null
}
```

**Response (authenticated):**
```json
{
  "authenticated": true,
  "auth_token": "user-plex-token"
}
```

### GET /api/plex/servers

Get list of user's Plex servers.

**Response:**
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

Get libraries from connected Plex server.

**Query Parameters:**
- `url` (optional): Plex server URL
- `token` (optional): Plex token

**Response:**
```json
{
  "libraries": [
    {
      "id": "1",
      "name": "Movies",
      "type": "movie"
    },
    {
      "id": "2",
      "name": "TV Shows",
      "type": "show"
    }
  ]
}
```

### POST /api/plex/test

Test Plex connection.

**Request:**
```json
{
  "url": "http://192.168.1.100:32400",
  "token": "your-token"
}
```

**Response:**
```json
{
  "success": true,
  "server_name": "My Server",
  "version": "1.32.0"
}
```

---

## Jobs

### GET /api/jobs

List all jobs.

**Response:**
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

Create a new job.

**Request:**
```json
{
  "library_id": "1",
  "library_name": "Movies"
}
```

**Response:**
```json
{
  "id": "job-123",
  "status": "pending",
  "message": "Job created successfully"
}
```

### GET /api/jobs/{id}

Get job details.

**Response:**
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

### POST /api/jobs/{id}/cancel

Cancel a running job.

**Response:**
```json
{
  "success": true,
  "message": "Job cancelled"
}
```

### DELETE /api/jobs/{id}

Delete a job from history.

**Response:**
```json
{
  "success": true
}
```

---

## Schedules

### GET /api/schedules

List all schedules.

**Response:**
```json
{
  "schedules": [
    {
      "id": "schedule-1",
      "name": "Nightly Movies",
      "library_id": "1",
      "schedule_type": "cron",
      "cron_expression": "0 2 * * *",
      "enabled": true,
      "next_run": "2024-01-16T02:00:00Z"
    }
  ]
}
```

### POST /api/schedules

Create a new schedule.

**Request (cron):**
```json
{
  "name": "Nightly Movies",
  "library_id": "1",
  "schedule_type": "cron",
  "cron_expression": "0 2 * * *"
}
```

**Request (interval):**
```json
{
  "name": "Every 4 Hours",
  "library_id": "1",
  "schedule_type": "interval",
  "interval_minutes": 240
}
```

**Response:**
```json
{
  "id": "schedule-1",
  "success": true
}
```

### PUT /api/schedules/{id}

Update a schedule.

**Request:**
```json
{
  "enabled": false
}
```

**Response:**
```json
{
  "success": true
}
```

### DELETE /api/schedules/{id}

Delete a schedule.

**Response:**
```json
{
  "success": true
}
```

### POST /api/schedules/{id}/run

Run a schedule immediately.

**Response:**
```json
{
  "success": true,
  "job_id": "job-456"
}
```

---

## System

### GET /api/health

Health check endpoint. **No authentication required.**

**Response:**
```json
{
  "status": "healthy",
  "version": "1.0.0"
}
```

### GET /api/system/status

Get system status.

**Response:**
```json
{
  "gpus": [
    {
      "type": "vaapi",
      "device": "/dev/dri/renderD128",
      "name": "Intel UHD Graphics"
    }
  ],
  "workers": {
    "gpu": 4,
    "cpu": 2
  },
  "jobs": {
    "running": 1,
    "pending": 0,
    "completed": 15,
    "failed": 2
  }
}
```

### GET /api/system/config

Get current configuration.

**Response:**
```json
{
  "plex_url": "http://192.168.1.100:32400",
  "gpu_threads": 4,
  "cpu_threads": 2,
  "thumbnail_interval": 5,
  "thumbnail_quality": 4
}
```

### GET /api/libraries

Get Plex libraries.

**Response:**
```json
{
  "libraries": [
    {
      "id": "1",
      "name": "Movies",
      "type": "movie",
      "item_count": 500
    }
  ]
}
```

---

## WebSocket Events

The dashboard uses WebSocket for real-time updates.

### Events (Server → Client)

| Event | Description |
|-------|-------------|
| `job_progress` | Job progress update |
| `job_complete` | Job finished |
| `job_error` | Job failed |
| `worker_update` | Worker status change |

### Example: Job Progress

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

## Error Responses

All errors follow this format:

```json
{
  "error": "Error message",
  "code": "ERROR_CODE"
}
```

### Common Error Codes

| Code | HTTP Status | Description |
|------|-------------|-------------|
| `UNAUTHORIZED` | 401 | Missing or invalid auth token |
| `NOT_FOUND` | 404 | Resource not found |
| `VALIDATION_ERROR` | 400 | Invalid request data |
| `SERVER_ERROR` | 500 | Internal server error |

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

[Back to Main README](README.md)
