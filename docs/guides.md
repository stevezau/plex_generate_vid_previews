# Guides & Troubleshooting

> [Back to Docs](README.md)

User guides for the web interface, webhooks, load testing, and answers to common questions.

> [!IMPORTANT]
> This page is the source of truth for web operations, webhook workflows, and troubleshooting.
> For installation and first-time setup, use [Getting Started](getting-started.md).
> For exact configuration values and API contracts, use [Configuration & API Reference](reference.md).

## Related Docs

- [Getting Started](getting-started.md)
- [Configuration & API Reference](reference.md)
- [Main README](../README.md)

---

## Web Interface

Dashboard for managing preview generation jobs, settings, and schedules.

### Setup Wizard

When you first access the web interface, you'll be guided through a **Setup Wizard**:

1. **Sign in with Plex** — authenticate securely via Plex OAuth (no manual token copying!)
2. **Select Server** — choose which Plex server to connect to
3. **Configure Paths** — set up media paths and path mappings
4. **Processing Options** — configure GPU threads, thumbnail quality, etc.
5. **Security** — view or customize your access token (optional)

After setup completes, you'll be taken to the dashboard.

### Accessing the Dashboard

1. Start the container
2. Open `http://YOUR_SERVER_IP:8080`
3. Get your authentication token using [Authentication Token](getting-started.md#authentication-token)
4. Enter the token to log in

### Dashboard Features

**Connection Status** — shows your Plex connection status:

- **Connected** — server name and available GPUs displayed
- **Not configured** — link to setup wizard

**Job Management:**

- **Start new jobs** — process all libraries or specific ones
- **View progress** — real-time progress with WebSocket updates
- **Cancel jobs** — stop running jobs
- **Job history** — view completed/failed jobs

**Scheduling:**

- **Cron schedules** — set up recurring processing
- **Interval-based** — run every X minutes
- **Per-library** — schedule specific libraries

### Settings Page

Access settings at `/settings` to manage:

- **Plex Connection** — re-authenticate, test connection
- **Libraries** — select which libraries to process
- **Path Mappings** — media path, Plex videos path, local videos path
- **Processing Options** — GPU/CPU threads, thumbnail interval and quality

Settings are saved to `/config/settings.json` and persist across restarts.

### Webhooks Page

Access the Webhooks page at `/webhooks` to configure Radarr/Sonarr integration:

- **Enable/Disable** — master toggle for webhook processing
- **Webhook URLs** — copy-ready URLs for Radarr and Sonarr
- **Delay** — seconds to wait after import (gives Plex time to index)
- **Library Mapping** — which library to scan for each source
- **Webhook Secret** — optional dedicated authentication token
- **Setup Instructions** — step-by-step guide for Radarr/Sonarr configuration
- **Activity Log** — recent webhook events with status badges

### Production Server

In Docker, the web interface runs on **gunicorn** with the **gthread** (threaded) worker class:

- **Native WebSocket support** via `simple-websocket`
- **Reliable real-time updates** — Python threads handle concurrent HTTP and WebSocket connections
- **No monkey-patching** — standard library modules work unmodified

| Setting | Value | Purpose |
|---------|-------|---------|
| Worker class | `gthread` | Threaded worker for concurrent requests |
| Threads | `4` | Handles parallel HTTP + WebSocket |
| Workers | `1` | Single worker (required for in-process job state) |
| Timeout | `300s` | Accommodates long-running FFmpeg processing |
| Keep-alive | `65s` | Outlives typical reverse proxy timeouts (60s) |

> [!NOTE]
> The server uses a single gunicorn worker because job state, schedules, and settings are managed in-process. Multiple workers would require Redis for shared state.

### Reverse Proxy

If placing behind nginx or Traefik, ensure WebSocket upgrade headers are forwarded:

```nginx
location / {
    proxy_pass http://localhost:8080;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

### Authentication

The web interface uses token-based authentication:

1. **Auto-generated token** — created on first run, saved to `/config/auth.json`
2. **Custom token via wizard** — set your own token during the setup wizard (Step 5)
3. **Fixed token** — set `WEB_AUTH_TOKEN` environment variable (overrides wizard setting)
4. **Token masking** — tokens are always masked in logs (only last 4 chars shown)

API authentication:

```bash
# Bearer token
curl -H "Authorization: Bearer YOUR_TOKEN" http://localhost:8080/api/jobs

# X-Auth-Token header
curl -H "X-Auth-Token: YOUR_TOKEN" http://localhost:8080/api/jobs
```

### Rate Limiting

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

### CLI Mode

To skip the web interface and run one-time processing:

```bash
# Docker
docker run ... stevezzau/plex_generate_vid_previews:latest --cli

# Local
plex-generate-previews --cli --plex-url ... --plex-token ...
```

### Real-Time Updates

The dashboard uses WebSocket connections (via Flask-SocketIO + simple-websocket) for real-time job progress updates. The client connects to the `/jobs` namespace using WebSocket transport with automatic polling fallback.

| Event | Description |
|-------|-------------|
| `job_created` | New job was started |
| `job_progress` | Progress update (percentage, current item) |
| `job_complete` | Job finished successfully |
| `job_error` | Job encountered an error |
| `worker_update` | Worker status changed |

---

## Webhook Integration

Automatically generate preview thumbnails when Radarr or Sonarr imports new media. Webhooks trigger a library scan after a configurable delay, giving Plex time to detect and index the new files.

### How It Works

1. Radarr/Sonarr imports a file and sends a webhook POST to this app
2. The app waits for the configured delay (default 60s) to let Plex index the file
3. If multiple imports arrive for the same library, they are **debounced** — only one scan runs
4. A job is created and appears on the dashboard, sorted by newest items first
5. Items that already have preview thumbnails are skipped automatically

### Prerequisites

- Plex Generate Previews running with the web UI accessible
- Radarr and/or Sonarr installed and managing your media

### Configure Radarr

1. Open the web UI and navigate to **Webhooks** (in the top nav)
2. Copy the **Radarr Webhook URL**
3. In Radarr, go to **Settings → Connect → + → Webhook**
4. Set **Name**: `Plex Previews`
5. Set **URL**: paste the Radarr Webhook URL
6. Under **Events**, enable:
   - On Import
   - On Upgrade
7. Add a header for authentication:
   - **Key**: `X-Auth-Token`
   - **Value**: your API token (see [Authentication Token](getting-started.md#authentication-token)) or webhook secret (if configured)
8. Click **Test** to verify the connection
9. Click **Save**

### Configure Sonarr

1. Copy the **Sonarr Webhook URL** from the web UI Webhooks page
2. In Sonarr, go to **Settings → Connect → + → Webhook**
3. Set **Name**: `Plex Previews`
4. Set **URL**: paste the Sonarr Webhook URL
5. Under **Events**, enable:
   - On Import
   - On Upgrade
6. Add authentication header: **Key**: `X-Auth-Token`, **Value**: your API token or webhook secret
7. Click **Test** then **Save**

### Configuration
All settings are configurable from the **Webhooks** page in the web UI.

| Setting | Default | Description |
|---------|---------|-------------|
| **Enable Webhooks** | On | Master toggle |
| **Delay** | 60s | Wait time before scanning (10–300 s) |
| **Radarr Library** | All | Which Plex library to scan for movie imports |
| **Sonarr Library** | All | Which Plex library to scan for TV imports |
| **Webhook Secret** | *(empty)* | Dedicated authentication token for webhooks |

### Webhook Secret

By default, webhooks authenticate using your main API token. You can optionally configure a **dedicated webhook secret** for better security isolation:

1. On the Webhooks page, click **Generate** next to the secret field
2. Click **Save Changes**
3. Use the generated secret as the `X-Auth-Token` value in Radarr/Sonarr

### Debouncing

When multiple files are imported in quick succession (e.g., a season pack), the app **debounces** the webhook triggers. Each new import for the same library resets the delay timer, so only one scan runs after all imports complete.

Example: Sonarr imports 10 episodes over 30 seconds with a 60s delay configured. The scan starts 60 seconds after the *last* episode is imported, not after each one.

---

## Load Testing

A Locust load test is available for stress testing the web API.

### Running Load Tests

```bash
# Interactive mode (opens browser UI)
locust -f tests/load/locustfile.py

# Open http://localhost:8089 to configure and start
```

```bash
# Headless mode
locust -f tests/load/locustfile.py --headless -u 50 -r 10 -t 60s
```

> [!NOTE]
> Locust is a dev dependency. Install with `pip install -e ".[dev]"`.

---

## FAQ

### General

**What does this tool do?**

Generates video preview thumbnails (BIF files) for Plex Media Server. These are the small images you see when scrubbing through videos. Plex's built-in generation is slow — this tool makes it 5-10x faster using GPU acceleration.

**Does this work on Windows?**

Yes! Windows supports GPU acceleration via D3D11VA, which works with NVIDIA, AMD, and Intel GPUs. Install the latest GPU drivers and it just works.

**Can I use this without a GPU?**

Yes! Set `--gpu-threads 0` and `--cpu-threads 4` (or higher) for CPU-only processing.

**What's the difference between web mode and CLI mode?**

- **Web mode** (default): runs a dashboard at port 8080 for managing jobs and schedules
- **CLI mode** (`--cli`): runs one-time processing and exits

### GPUs

**How do I know which GPUs are detected?**

```bash
plex-generate-previews --list-gpus
```

**Can I use multiple GPUs?**

Yes! The tool automatically detects and can use multiple GPUs. Use `--gpu-selection "0,1,2"` to select specific ones.

**Which GPU should I use?**

| GPU Type | Best For |
|----------|----------|
| NVIDIA | Fastest for video processing |
| Intel iGPU | Great for low-power setups, common on Unraid |
| AMD | Good VAAPI support on Linux |
| CPU-only | Works everywhere, slower |

### Performance

**How many threads should I use?**

| Scenario | GPU Threads | CPU Threads |
|----------|-------------|-------------|
| Default | 1 | 1 |
| Balanced | 4 | 2 |
| High-end | 8 | 4 |
| CPU-only | 0 | 8 |

> [!TIP]
> Start with the defaults and increase gradually while monitoring system load.

**What's thumbnail quality 1-10?**

Lower numbers = higher quality but larger file sizes.

- Quality 2 = highest quality
- Quality 4 = default (good balance)
- Quality 10 = lowest quality

### Docker

**Why does my container fail to start?**

Most common cause: using `init: true` in docker-compose. Remove it — this container uses s6-overlay.

**Why can't the container find my files?**

Path mapping issue. See [Path Mappings](reference.md#path-mappings).

**How do I get the authentication token?**

Use [Authentication Token](getting-started.md#authentication-token).

### Processing

**Can I process specific libraries only?**

Yes! Use `--plex-libraries "Movies, TV Shows"` to process only specific libraries.

**How do I regenerate existing thumbnails?**

Use `--regenerate-thumbnails` or set `REGENERATE_THUMBNAILS=true`.

**Why is it "skipping" some files?**

Possible causes:

- Thumbnails already exist (use `--regenerate-thumbnails` to force)
- File not found (check [path mappings](reference.md#path-mappings))
- Invalid file format

**Why does ETA show "Calculating..." for so long?**

The ETA calculation is designed to be **accurate, not fast**:

1. **Initial skip burst (0-30 seconds)**: shows "Calculating..." — many files may already have thumbnails and are skipped instantly
2. **First few items processed (30s-5 min)**: still shows "Calculating..." — real FFmpeg encoding is underway, but not enough data yet
3. **Realistic estimate appears (5+ min)**: shows time like "8h 30m" — calculated from actual per-item processing time, updates every 3 seconds
4. **During processing**: ETA counts down and adjusts in real-time as processing rate varies

Early ETA guesses based on incomplete data are wildly inaccurate. The "Calculating..." phase filters out this noise.

---

## Troubleshooting

Use this table to diagnose common failures quickly.

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| `Skipping as file not found` | Path mapping mismatch between Plex and this container | Verify mappings in [Path Mappings](reference.md#path-mappings). |
| `GPU permission denied` | Container user cannot access GPU device files | Set `PUID`/`PGID` to a user with GPU access; on Unraid use `PUID=99`, `PGID=100`. |
| `PLEX_CONFIG_FOLDER does not exist` | Incorrect mount or Plex config path | Confirm mounted path contains `Cache`, `Media`, and `Metadata`. |
| `Connection failed to Plex` | Bad Plex URL, unreachable host, or invalid token | Use server IP (not `localhost` in Docker), verify Plex is running, and test token with curl. |
| Webhook returns `401` | Invalid or missing authentication token in webhook headers | Set `X-Auth-Token` to your API token or configured webhook secret. |
| Webhook test passes but imports do not trigger jobs | Wrong webhook events or webhooks disabled | Enable **On Import** in Radarr/Sonarr and verify `webhook_enabled=true`. |
| New files are imported but previews are not generated | Plex indexing delay or wrong library mapping | Increase webhook delay and verify Radarr/Sonarr library mapping in Webhooks settings. |
| Radarr/Sonarr cannot reach webhook URL | Network routing or hostname issue | Use host IP or reachable Docker hostname (not `localhost`), then verify firewall and port `8080`. |

### Validate Plex Config Path

```bash
ls -la "/path/to/Library/Application Support/Plex Media Server"
```

Expected directories include `Cache`, `Media`, and `Metadata`.

### Debug Logging

Enable detailed logs when diagnosing persistent issues:

```bash
-e LOG_LEVEL=DEBUG
# or
--log-level DEBUG
```

---

## Support

Open a [GitHub Issue](https://github.com/stevezau/plex_generate_vid_previews/issues).

---

## Next Steps

- Validate installation and mounts in [Getting Started](getting-started.md)
- Confirm environment variables and API behavior in [Configuration & API Reference](reference.md)

---

[Back to Docs](README.md) | [Main README](../README.md)
