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
4. **Processing Options** — configure GPU threads, CPU threads, CPU fallback workers, thumbnail quality, etc.
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

> [!NOTE]
> Only one job runs at a time. If a job is triggered (manually, by a schedule, or by a webhook) while another is already running, the incoming job is immediately marked **Cancelled** and a warning is logged. This prevents concurrent FFmpeg workloads and temp-folder conflicts.

**Pause / Resume (global):**

- **Pause Processing** — Stops all processing system-wide: no new jobs will start (manual, scheduled, or webhook), and the current job stops dispatching new tasks. Already-running FFmpeg tasks finish their current file (soft pause), then workers idle. Use this to cap bandwidth or pause overnight.
- **Resume Processing** — Clears the global pause; new jobs can start and the current job resumes dispatching.
- Controls appear in the **Current Job** header and to the left of **Clear Jobs** in the Job Queue. State is persisted and survives restarts.

**Scheduling:**

- **Cron schedules** — set up recurring processing
- **Interval-based** — run every X minutes
- **Per-library** — schedule specific libraries

### Settings Page

Access settings at `/settings` to manage:

- **Plex Connection** — re-authenticate, test connection
- **Libraries** — select which libraries to process
- **Path Mappings** — media path, Plex videos path, local videos path
- **Processing Options** — per-GPU settings (enable/disable, workers, FFmpeg threads), CPU threads, CPU fallback workers, thumbnail interval and quality

### CPU Fallback Workers (GPU Safety Net)

Use this when you want GPU-only main processing but still want CPU recovery for unsupported GPU files.

- Set **CPU Workers** to `0`
- Set **CPU Fallback Workers** to `1` or more

Behavior:

- Main queue runs on GPU workers only
- If a GPU worker hits an unsupported codec/runtime decode failure, the item is queued to CPU fallback workers
- If **CPU Workers > 0**, fallback-only workers are not used (regular CPU workers already handle fallback work)

Settings are saved to `/config/settings.json` and persist across restarts.

### Webhooks Page

Access the Webhooks page at `/webhooks` to configure Radarr/Sonarr integration:

- **Enable/Disable** — master toggle for webhook processing
- **Webhook URLs** — copy-ready URLs for Radarr and Sonarr
- **Delay** — seconds to wait after import (gives Plex time to index)
- **Webhook Secret** — optional dedicated authentication token
- **Setup Instructions** — step-by-step guide for Radarr/Sonarr configuration
- **Activity Log** — recent webhook events with status badges

### Production Server

In Docker, the web interface runs on **gunicorn** with the **gthread** worker class:

- **WebSocket support** via Flask-SocketIO with threading async mode
- **Real-time updates** — job progress and worker status over WebSocket

| Setting | Value | Purpose |
|---------|-------|---------|
| Worker class | `gthread` | Threaded worker; SocketIO uses threading async mode |
| Workers | `1` | Single worker (required for in-process job state) |
| Timeout | `300s` | Accommodates long-running FFmpeg processing |
| Keep-alive | `65s` | Outlives typical reverse proxy timeouts (60s) |

> [!NOTE]
> The server uses a single gunicorn worker because job state, schedules, and settings are managed in-process. Multiple workers would require Redis for shared state.

### Reverse Proxy

If you want to expose the web UI outside your local network — for example
with HTTPS, a custom domain, or alongside other services — you can place it
behind a reverse proxy such as Nginx, Apache, or Traefik.

The built-in server listens on port `8080` (HTTP) and the reverse proxy
forwards external requests to it. The web UI uses **WebSocket** (Socket.IO)
for real-time updates, so your reverse proxy **must** forward WebSocket
upgrade requests.

#### Nginx

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

#### Apache

Enable the required modules first:

```bash
sudo a2enmod proxy proxy_http proxy_wstunnel rewrite headers
sudo systemctl restart apache2
```

Example HTTPS virtual host:

```apache
<VirtualHost *:443>
    ServerName previews.example.com

    SSLEngine On
    SSLCertificateFile    /etc/letsencrypt/live/example.com/fullchain.pem
    SSLCertificateKeyFile /etc/letsencrypt/live/example.com/privkey.pem
    SSLProtocol +TLSv1.2

    RequestHeader set X-Forwarded-Proto https
    RequestHeader set X-Forwarded-Ssl on

    RewriteEngine On
    RewriteCond %{HTTP:Upgrade} =websocket [NC]
    RewriteRule /(.*) ws://127.0.0.1:8080/$1 [P,L]

    ProxyPass / http://127.0.0.1:8080/
    ProxyPassReverse / http://127.0.0.1:8080/

    ProxyRequests Off
    ProxyPreserveHost On

    Header edit Location ^http://(.*)$ https://$1
</VirtualHost>
```

#### Traefik

Traefik v2+ forwards WebSocket upgrade headers automatically. No extra
configuration is required beyond a standard HTTP router and service.

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

### Real-Time Updates

The dashboard uses Flask-SocketIO with WebSocket for real-time job progress updates. The client connects to the `/jobs` namespace.

| Event | Description |
|-------|-------------|
| `job_created` | New job was started |
| `job_progress` | Progress update (percentage, current item) |
| `job_complete` | Job finished successfully |
| `job_error` | Job encountered an error |
| `worker_update` | Worker status changed |

---

## Webhook Integration

Automatically generate preview thumbnails when Radarr or Sonarr imports new media, or when any external tool (Tdarr, scripts, etc.) modifies a file. Webhooks trigger processing of **only the imported file(s)** after a configurable delay, giving Plex time to detect and index the new files.

### How It Works

1. Radarr/Sonarr imports a file (or an external tool sends a custom webhook) and a POST is sent to this app.
2. The app **queues** the file and starts (or resets) a timer. Imports from the same source (Radarr, Sonarr, or Custom) are batched together.
3. A batch is processed only after the **delay** (e.g. 60s) has passed with **no new** imports from that source. So if another file arrives 1 second before the batch would run, it is added to the queue and the timer resets — the batch runs 60 seconds after that file. Every file gets at least 60 seconds before we process it.
4. This delay is important because **Plex needs time to add the new file to its library**. If we process too soon, Plex may not have indexed the file yet and the job can fail or skip the item.
5. When the timer fires, the app resolves each queued path to a Plex item and processes only those items (no full-library scan), limited to libraries selected in Settings. Items that already have preview thumbnails are skipped automatically.

### Prerequisites

- Plex Generate Previews running with the web UI accessible
- Radarr and/or Sonarr installed and managing your media (for Radarr/Sonarr webhooks)

### Configure Radarr

1. Open the web UI and navigate to **Webhooks** (in the top nav)
2. Copy the **Radarr Webhook URL**
3. In Radarr, go to **Settings → Connect → + → Webhook**
4. Set **Name**: `Plex Previews`
5. Set **URL**: paste the Radarr Webhook URL
6. Under **Events**, enable:
   - On Import
   - On Upgrade
7. **Authentication** (use one):
   - **Username/Password** (works in all versions): Leave **Username** empty and set **Password** to your API token (see [Authentication Token](getting-started.md#authentication-token)) or webhook secret. The app treats the password as the token.
   - **Custom headers** (if your webhook form has a Headers section): Add **Key** = `X-Auth-Token`, **Value** = your API token or webhook secret.
8. Click **Test** to verify the connection
9. Click **Save**

### Configure Sonarr

1. Copy the **Sonarr Webhook URL** from the web UI Webhooks page
2. In Sonarr, go to **Settings → Connect → + → Webhook**
3. Set **Name**: `Plex Previews`
4. Set **URL**: paste the Sonarr Webhook URL
5. Under **Events**, enable **On File Import** and **On File Upgrade**
6. **Authentication** (use one):
   - **Username/Password** (works in all versions): Leave **Username** empty and set **Password** to your API token or webhook secret. The app treats the password as the token.
   - **Custom headers** (if your webhook form has a Headers section): Add **Key** = `X-Auth-Token`, **Value** = your API token or webhook secret.
7. Click **Test** then **Save**

### Custom Webhook (Tdarr, scripts, etc.)

The custom webhook endpoint lets any tool trigger preview generation by POSTing a file path. This is useful when an external tool (like Tdarr) modifies a media file after Sonarr/Radarr has already imported it — Plex detects the change and removes the old thumbnails, but Sonarr/Radarr won't send a new webhook since no import occurred.

**Endpoint:** `POST /api/webhooks/custom`

**Expected payload — single file:**

```json
{
  "file_path": "/media/movies/Movie (2024)/Movie.mkv"
}
```

**Expected payload — multiple files:**

```json
{
  "file_paths": [
    "/media/tv/Show/Season 01/S01E01.mkv",
    "/media/tv/Show/Season 01/S01E02.mkv"
  ],
  "title": "Optional display label"
}
```

**Test connectivity (no processing):**

```json
{
  "eventType": "Test"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `file_path` | string | One of `file_path` or `file_paths` required | Single absolute file path to process |
| `file_paths` | array of strings | One of `file_path` or `file_paths` required | Multiple absolute file paths to process |
| `title` | string | No | Display label shown in history/jobs (defaults to first file's basename) |
| `eventType` | string | No | Set to `"Test"` to verify the connection without triggering processing |

Authentication is the same as Radarr/Sonarr: use `X-Auth-Token` header, `Authorization: Bearer`, or Basic auth (password = token).

#### Configure Tdarr

Tdarr doesn't have built-in webhook support like Sonarr/Radarr. Instead, use the **Send Web Request** Flow plugin to POST to the custom endpoint after each transcode.

1. Open the web UI and navigate to **Webhooks** — copy the **Custom Webhook URL**
2. In Tdarr, open the **Flow** you want to trigger previews from
3. Add a **Send Web Request** plugin after your transcode step
4. Configure the plugin:
   - **Method**: `POST`
   - **Request URL**: paste the Custom Webhook URL (e.g. `http://your-server:8080/api/webhooks/custom`)
   - **Request Headers**: `{"Content-Type": "application/json", "X-Auth-Token": "YOUR_TOKEN"}`
   - **Request Body**: `{"file_path": "{{{args.inputFileObj._id}}}"}`
5. Save the Flow

The `{{{args.inputFileObj._id}}}` template variable is replaced by Tdarr at runtime with the full path of the transcoded file.

> [!TIP]
> If the webhook request fails (e.g. the server is temporarily down), add a **Reset Flow Error** plugin after the Send Web Request step so Tdarr doesn't mark the entire transcode as failed.

#### curl Example

```bash
curl -X POST "http://your-server:8080/api/webhooks/custom" \
  -H "Content-Type: application/json" \
  -H "X-Auth-Token: YOUR_TOKEN" \
  -d '{"file_path": "/media/movies/Movie (2024)/Movie.mkv"}'
```

### Configuration
All settings are configurable from the **Webhooks** page in the web UI.

| Setting | Default | Description |
|---------|---------|-------------|
| **Enable Webhooks** | On | Master toggle |
| **Delay before processing** | 60s | How long to wait with no new imports before running a batch (10–300 s). Incoming files are queued; a batch runs only after this many seconds of “quiet” from that source. Each new import resets the timer so every file gets at least this long for Plex to add it to the library before we process. |
| **Webhook Secret** | *(empty)* | Dedicated authentication token for webhooks |

Webhook processing uses your Settings library selection. If a webhook path belongs to an unchecked library, it is skipped.

### Webhook Secret

By default, webhooks authenticate using your main API token. You can optionally configure a **dedicated webhook secret** for better security isolation:

1. On the Webhooks page, click **Generate** next to the secret field
2. Click **Save Changes**
3. Use the generated secret as the token: in Radarr/Sonarr, either put it in **Password** (leave Username empty) or in the **X-Auth-Token** header if your form has a Headers section.

### Batching and the delay

When multiple files are imported in quick succession (e.g., a season pack), the app **queues** them per source (Radarr, Sonarr, or Custom). Each new import **resets** the delay timer for that source. A batch runs only when the timer finally fires — i.e. when that many seconds have passed with no new imports. So every file in the batch has had at least that long for Plex to add it to the library before we process.

**Example:** Sonarr imports 10 episodes over 30 seconds with a 60s delay. The timer keeps resetting as each episode arrives. One job runs 60 seconds after the *last* episode and processes all 10 files. A file that arrived at 59 seconds is not processed in an earlier batch — it goes in this batch, and the batch runs 60 seconds after it, so Plex has time to index it.

**Viewing files in a batch:** On the **Dashboard**, jobs from webhooks show a label like "Sonarr: 3 files". Click the **+** (chevron) next to the label to expand and see the list of files. On the **Webhooks** page, **Recent Activity** rows for triggered batches include a chevron; click it to expand and see the files in that batch.

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

**What Plex settings should I use?**

In Plex Settings → Library, set **"Generate video preview thumbnails"** to **Never**. This tool replaces Plex's built-in generation. Disabling it in Plex avoids duplicate work and prevents Plex from using CPU for thumbnails when you want this app to handle them.

**Does this generate chapter thumbnails?**

No. This tool only generates **video preview thumbnails** (BIF files for timeline scrubbing). It does not generate chapter thumbnails, intro/credit detection, or other Plex media analysis.

**Does this work on Windows?**

Yes! Windows supports GPU acceleration via D3D11VA, which works with NVIDIA, AMD, and Intel GPUs. Install the latest GPU drivers and it just works.

**Can I use this without a GPU?**

Yes! In **Settings** → **Processing Options**, disable all GPUs (or set workers to 0) and set **CPU Workers** to your desired value (e.g. `4` or `8`).

**Is Docker required? Is there a standalone .exe?**

Docker is the recommended and supported way to run this tool. There is no standalone executable. Advanced users can install from source on Linux (requires Python 3.10+, FFmpeg, and mediainfo), but this is not officially supported. See [Getting Started](getting-started.md) for Docker setup.

**Does Plex need to run in Docker too?**

No. Plex can run bare-metal, in Docker, or any other way. This tool just needs network access to the Plex API and read/write access to the Plex application data directory (where BIF files are stored).

**Can I run this on a different machine than my Plex server?**

Yes, as long as the tool can reach the Plex API over the network and both machines have access to the media files and Plex config directory (e.g. via NFS or SMB mounts). See [Networking](getting-started.md#networking) for setup details.

**Does this work with Jellyfin or Emby?**

No. This tool is Plex-only — it generates Plex-specific BIF files and uses the Plex API to discover libraries and media items.

### GPUs

**How do I know which GPUs are detected?**

Open **Settings** → **Processing Options**. The GPU panel lists all detected GPUs with their device IDs, names, and types.

**Can I use multiple GPUs?**

Yes! In **Settings** → **Processing Options**, enable individual GPUs and set workers and FFmpeg threads per GPU. Each GPU can be enabled/disabled independently.

**Which GPU should I use?**

| GPU Type | Best For |
|----------|----------|
| NVIDIA | Fastest for video processing |
| Intel iGPU | Great for low-power setups, common on Unraid |
| AMD | Good VAAPI support on Linux |
| CPU-only | Works everywhere, slower |

### HDR / Tone Mapping

**Does it handle HDR content correctly?**

Yes. The tool auto-detects HDR metadata and tone maps to SDR before generating thumbnails:

| Format | Status |
|--------|--------|
| HDR10 | Fully tone mapped (configurable algorithm via zscale/tonemap, default: Hable) |
| HLG | Fully tone mapped (configurable algorithm via zscale/tonemap, default: Hable) |
| Dolby Vision Profile 7/8 (with HDR10 compatible base layer) | Fully tone mapped (configurable algorithm via zscale/tonemap, default: Hable) |
| Dolby Vision Profile 5 (no backward-compatible layer) | Supported via `libplacebo` with BT.2390 ([#172](https://github.com/stevezau/plex_generate_vid_previews/issues/172)) |

The tone mapping algorithm can be changed in **Settings > Thumbnail Settings > HDR Tone Mapping** or via the `TONEMAP_ALGORITHM` environment variable. Available options: `hable` (default), `reinhard`, `mobius`, `clip`, `gamma`, `linear`. If your HDR thumbnails look too dark, try `reinhard`.

Without tone mapping, HDR content (especially DV Profile 5) can produce thumbnails with a green or purple tint.

### Performance

**How many threads should I use?**

Configure per-GPU workers and FFmpeg threads in **Settings** → **Processing Options**. The GPU panel lets you set workers and FFmpeg threads per GPU.

| Scenario | GPU Workers (per GPU) | CPU Threads |
|----------|-------------|-------------|
| Default | 1 | 1 |
| Balanced | 4 total | 2 |
| High-end | 8 total | 4 |
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

Most common cause: using `init: true` in docker-compose. Remove it -- this container uses s6-overlay (a built-in process manager) and `init: true` conflicts with it.

**Why can't the container find my files?**

Path mapping issue. See [Path Mappings](reference.md#path-mappings).

**How do I get the authentication token?**

Use [Authentication Token](getting-started.md#authentication-token).

**Does GPU passthrough work with Docker Desktop on Windows?**

Docker Desktop's GPU passthrough (via WSL2) is not currently supported by this tool. For Windows with GPU acceleration, run natively with D3D11VA instead of Docker.

**Windows: paths in config must use forward slashes**

On Windows, use forward slashes (`/`) in all path configuration (environment variables, `.env` files, Settings). Backslashes (`\`) will cause path resolution failures.

### Processing

**Can I process specific libraries only?**

Yes! In **Settings** → **Libraries**, select which libraries to process.

**How do I regenerate existing thumbnails?**

When starting a job, use the **Regenerate** option to force regeneration of existing thumbnails.

**Why is it "skipping" some files?**

Possible causes:

- Thumbnails already exist (use the **Regenerate** option when starting a job to force)
- File not found (check [path mappings](reference.md#path-mappings))
- Invalid file format

**Why does ETA show "Calculating..." for so long?**

The ETA calculation is designed to be **accurate, not fast**:

1. **Initial skip burst (0-30 seconds)**: shows "Calculating..." — many files may already have thumbnails and are skipped instantly
2. **First few items processed (30s-5 min)**: still shows "Calculating..." — real FFmpeg encoding is underway, but not enough data yet
3. **Realistic estimate appears (5+ min)**: shows time like "8h 30m" — calculated from actual per-item processing time, updates every 3 seconds
4. **During processing**: ETA counts down and adjusts in real-time as processing rate varies

Early ETA guesses based on incomplete data are wildly inaccurate. The "Calculating..." phase filters out this noise.

**What is the Sonarr/Radarr path column for?**

Only relevant if you use [webhook integration](guides.md#webhook-integration). When Sonarr/Radarr fire a webhook, they include the file path as *they* see it inside their container, which may differ from the path inside this tool's container. The path column translates between them. For example:

| Container | Might see the file as |
|-----------|----------------------|
| Plex | `/data/tv/Show/episode.mkv` |
| Sonarr | `/tv/Show/episode.mkv` |
| This tool | `/mnt/media/tv/Show/episode.mkv` |

If you are not using webhooks, or all containers use the same media paths, leave it blank.

---

## Troubleshooting

Use this table to diagnose common failures quickly.

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| `Skipping as file not found` | Path mapping mismatch between Plex and this container | Verify mappings in [Path Mappings](reference.md#path-mappings). |
| `GPU permission denied` | Container user cannot access GPU device files | Set `PUID`/`PGID` to a user with GPU access; on Unraid use `PUID=99`, `PGID=100`. |
| `PLEX_CONFIG_FOLDER does not exist` | Incorrect mount or Plex config path | Confirm mounted path contains `Cache`, `Media`, and `Metadata`. |
| `Connection failed to Plex` | Bad Plex URL, unreachable host, or invalid token | Use server IP (not `localhost` in Docker), verify Plex is running, and test token with curl. |
| Webhook job shows as **Cancelled** in history | Another job was already running when the webhook delay expired | Wait for the active job to finish; webhooks fired while idle will run normally. To avoid this, increase the webhook delay so imports do not fire during long processing runs. |
| Webhook returns `401` | Invalid or missing authentication | In Sonarr/Radarr webhook settings, leave **Username** empty and set **Password** to your API token or webhook secret. |
| Webhook test passes but imports do not trigger jobs | Wrong webhook events or webhooks disabled | Enable **On Import** in Radarr/Sonarr and verify `webhook_enabled=true`. |
| New files are imported but previews are not generated | Plex indexing delay or wrong library mapping | Increase webhook delay and verify Radarr/Sonarr library mapping in Webhooks settings. |
| Radarr/Sonarr cannot reach webhook URL | Network routing or hostname issue | Use host IP or reachable Docker hostname (not `localhost`), then verify firewall and port `8080`. |
| New job starts after I paused | Global pause not set or UI not refreshed | Use **Pause Processing** (Current Job or Job Queue header). Pause is global and persisted; in-flight files finish before workers idle. |

### Validate Plex Config Path

```bash
ls -la "/path/to/Library/Application Support/Plex Media Server"
```

Expected directories include `Cache`, `Media`, and `Metadata`.

### Debug Logging

Enable detailed logs when diagnosing persistent issues. In **Settings** → **Processing Options**, set **Log Level** to `DEBUG`. Alternatively, set `LOG_LEVEL=DEBUG` as an environment variable (one-time seed on first start).

---

## Support

Open a [GitHub Issue](https://github.com/stevezau/plex_generate_vid_previews/issues).

---

## Next Steps

- Validate installation and mounts in [Getting Started](getting-started.md)
- Confirm environment variables and API behavior in [Configuration & API Reference](reference.md)

---

[Back to Docs](README.md) | [Main README](../README.md)
