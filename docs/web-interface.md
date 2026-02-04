# Web Interface

> [Back to Docs](README.md)

Dashboard for managing preview generation jobs, settings, and schedules.

---

## First-Time Setup

When you first access the web interface, you'll be guided through a **Setup Wizard**:

### Setup Wizard Steps

1. **Sign in with Plex** - Authenticate securely via Plex OAuth (no manual token copying!)
2. **Select Server** - Choose which Plex server to connect to
3. **Configure Paths** - Set up media paths and path mappings
4. **Processing Options** - Configure GPU threads, thumbnail quality, etc.

After setup completes, you'll be taken to the dashboard.

---

## Accessing the Dashboard

1. Start the container
2. Open `http://YOUR_SERVER_IP:8080`
3. Get auth token from logs:
   ```bash
   docker logs plex-generate-previews | grep "Token:"
   ```
4. Enter the token to log in

---

## Dashboard Features

### Connection Status

The dashboard shows your Plex connection status:
- **Connected** - Server name and available GPUs displayed
- **Not configured** - Link to setup wizard

### Job Management

- **Start new jobs** - Process all libraries or specific ones
- **View progress** - Real-time progress with WebSocket updates
- **Cancel jobs** - Stop running jobs
- **Job history** - View completed/failed jobs

### Scheduling

- **Cron schedules** - Set up recurring processing
- **Interval-based** - Run every X minutes
- **Per-library** - Schedule specific libraries

---

## Settings Page

Access settings at `/settings` to manage:

### Plex Connection
- **Re-authenticate** - Update your Plex token via OAuth
- **Test Connection** - Verify connection to your server

### Libraries
- **Select Libraries** - Choose which libraries to process

### Path Mappings
- **Media Path** - Path where media files are mounted
- **Plex Videos Path** - Path as Plex sees files
- **Local Videos Path** - Path as this container sees files

### Processing Options
- **GPU Threads** - Number of parallel GPU workers
- **CPU Threads** - Number of parallel CPU workers
- **Thumbnail Interval** - Seconds between preview frames
- **Thumbnail Quality** - Image quality (2=highest, 10=lowest)

Settings are saved to `/config/settings.json` and persist across restarts.

---

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `WEB_PORT` | `8080` | Web server port |
| `WEB_AUTH_TOKEN` | Auto-generated | Fixed auth token |
| `WEB_HIDE_TOKEN` | `false` | Hide token from logs |
| `FLASK_SECRET_KEY` | Auto-generated | Session secret |
| `CORS_ORIGINS` | `*` | Allowed origins |

---

## Authentication

### Token Authentication

The web interface uses token-based authentication:

1. **Auto-generated token** - Created on first run, saved to `/config/auth.json`
2. **Fixed token** - Set `WEB_AUTH_TOKEN` environment variable
3. **Hide from logs** - Set `WEB_HIDE_TOKEN=true`

### API Authentication

Use token in headers:
```bash
# Bearer token
curl -H "Authorization: Bearer YOUR_TOKEN" http://localhost:8080/api/jobs

# X-Auth-Token header
curl -H "X-Auth-Token: YOUR_TOKEN" http://localhost:8080/api/jobs
```

---

## Rate Limiting

Protection against brute force:

| Endpoint | Limit |
|----------|-------|
| `/login` POST | 5 per minute |
| `/api/auth/login` | 10 per minute |
| Default | 200 per day, 50 per hour |

For multi-worker deployments, configure Redis:
```bash
RATELIMIT_STORAGE_URL=redis://localhost:6379
```

---

## API Endpoints

### Jobs

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/jobs` | List all jobs |
| POST | `/api/jobs` | Create new job |
| GET | `/api/jobs/{id}` | Get job details |
| POST | `/api/jobs/{id}/cancel` | Cancel job |
| DELETE | `/api/jobs/{id}` | Delete job |

### Schedules

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/schedules` | List schedules |
| POST | `/api/schedules` | Create schedule |
| PUT | `/api/schedules/{id}` | Update schedule |
| DELETE | `/api/schedules/{id}` | Delete schedule |
| POST | `/api/schedules/{id}/run` | Run now |

### System

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/system/status` | System status |
| GET | `/api/system/config` | Current config |
| GET | `/api/libraries` | Plex libraries |
| GET | `/api/health` | Health check (no auth) |

---

## CLI Mode

To skip the web interface and run one-time processing:

```bash
# Docker
docker run ... stevezzau/plex_generate_vid_previews:latest --cli

# Local
plex-generate-previews --cli --plex-url ... --plex-token ...
```

---

## Real-time Updates

The dashboard uses WebSocket connections for real-time job progress updates. No polling required.

---

[Back to Docs](README.md) | [Main README](../README.md)
