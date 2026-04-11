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

The Dashboard shows a compact "Schedules" teaser with the next upcoming run and a total count. Full schedule management lives on the dedicated **Schedules** page (`/schedules`, also linked from the top nav):

- **Cron schedules** — set up recurring processing
- **Interval-based** — run every X minutes
- **Per-library** — schedule specific libraries
- **Scan mode** — each schedule is either a *Full library scan* (default) or a *Recently added only* scan (see [Auto-trigger from Plex](#auto-trigger-from-plex-no-sonarrradarr))

### Settings Page

Access settings at `/settings` to manage:

- **Plex Connection** — re-authenticate, test connection
- **Libraries** — select which libraries to process
- **Path Mappings** — media path, Plex videos path, local videos path
- **Processing Options** — per-GPU settings (enable/disable, workers, FFmpeg threads), CPU threads, CPU fallback workers, thumbnail interval and quality

Settings and Webhooks pages **save automatically as you edit** — there's no Save button. Toggles, sliders, and dropdowns commit immediately; text fields commit on blur (or ~1 s after you stop typing). A small status indicator in the page header shows `Saving…` / `Saved at HH:MM` so you can tell the change landed. If a save fails (e.g. the backend is down), the indicator shows an error and you can click it to retry.

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

## Auto-trigger from Plex (no Sonarr/Radarr)

For media you add to Plex **manually** — copying files into a watched folder, importing through Plex itself, or using any tool other than Sonarr/Radarr/Tdarr — there are two built-in ways to auto-trigger preview generation. Both live on the **Webhooks** page as dedicated sections (**Plex Direct** and **Recently Added Scanner** in the sidebar), and both feed into the same job pipeline as the existing webhooks.

> [!IMPORTANT]
> **Both options trigger only on _new_ library items.** When Sonarr or Radarr **upgrades** an existing file in place, Plex keeps the same library item, so neither option will see it. Use the existing Sonarr/Radarr webhooks (which fire on `On Upgrade`) for that case.

### Option A — Plex direct webhook (instant)

This uses Plex's built-in webhook feature. The app calls Plex's account API to register its own `/api/webhooks/plex` endpoint, so you don't have to copy/paste anything into Plex Web → Settings → Webhooks (though you still can if you'd rather).

**Requirements:**
- An active **Plex Pass** subscription on the server-owner account. Plex's webhook feature is Plex-Pass-only.
- **Mobile Push Notifications enabled** on your Plex server. This is the catch: Plex's `library.new` event is delivered through the same code path as mobile push notifications, and if push notifications are off, library events are silently dropped. Enable them under Plex Web → Settings → Server → Notifications. You don't have to actually use mobile push — they just need to be turned on.

**Setup:**
1. Open the web UI → **Webhooks** and scroll to (or click) the **Plex Direct** sidebar link.
2. The URL field is pre-filled with the URL you're currently accessing the app at (typically correct for same-host setups). If your Plex Media Server is on a different host or behind a different network/proxy, override it with a URL Plex can reach.
3. Click **Test reachability** to verify the URL is routable. The app self-POSTs a synthetic ping; success means Plex should also be able to deliver.
4. Click **Register with Plex**. If you're missing Plex Pass, the UI will tell you and disable the button.
5. (Optional) Confirm by checking Plex Web → Settings → Webhooks — your URL should appear there.

**How it works at runtime:** Plex POSTs a `library.new` event to `/api/webhooks/plex` whenever a new item is added. The app filters out everything else (`media.play`, `media.rate`, etc.), pulls the file paths from `Metadata.Media[].Part[].file` if present, otherwise looks the item up by `ratingKey`, and feeds the paths into the same debounce → batch → process pipeline as Radarr/Sonarr.

**How auth works:** Plex's webhook UI doesn't allow custom headers or HTTP Basic credentials, so there's no way to put an `X-Auth-Token` header on the requests Plex sends. Instead, the **Register with Plex** button appends your webhook secret (or API token) to the URL Plex stores as a `?token=…` query parameter. When Plex POSTs to that URL, the endpoint validates the query token the same way it validates header tokens from Radarr/Sonarr. **If you rotate the webhook secret**, click **Re-register with Plex** (or just save settings — the app auto-re-registers on secret change) so Plex picks up the new value.

### Option B — Recently Added scanner (universal)

A scheduled poll for items where Plex's `addedAt` falls within a configured lookback window. Works without Plex Pass and without push notifications, at the cost of a polling interval of latency.

**The scanner is a first-class schedule type.**  You create, edit, enable, disable, and delete Recently Added scanners through the same Schedules UI as any other scheduled job — and you can create **multiple scanners** with different libraries, intervals, or lookback windows.  For example: scan Movies every 15 minutes with a 1-hour lookback, and your 4K library every 6 hours with a 24-hour lookback.

**Quick start (one click):**
1. Open the web UI → **Webhooks** → **Recently Added Scanner** (sidebar link).
2. Click **Create default scanner**.  A schedule is created with sensible defaults: runs every **15 minutes**, lookback window **1 hour**, all libraries.
3. That's it.  You can stop here, or continue to customize it.

**Customize or add more scanners:**
1. Click **Manage on Schedules page** on the Webhooks card, or open the **Schedules** page directly from the top nav.
2. Click **Add Schedule** (or **Edit** on an existing scanner).
3. In the modal, choose **Scan mode → Recently added only**.  The Schedule Type field defaults to Interval; pick your frequency.
4. Choose a **Lookback window** — 15 min / 30 min / 1 hour (default) / 2 hours / 6 hours / 24 hours / 3 days / 7 days.
5. Pick a **Library** (or leave as "All Libraries") and click **Create** / **Save**.

**Choosing a lookback window:** items that already have BIF previews are skipped automatically by the job runner, so a larger lookback is cheap — it just re-queries Plex for a wider window. Pick something a few times larger than your scan interval so transient outages (e.g. a 30-minute Plex hiccup) don't cause missed items. The default **1 hour** gives a 4× safety buffer over a 15-min interval, which is plenty for the happy path while staying light on Plex.

**Scheduled scanners are marked with a blue "Recently Added" badge** next to the schedule name in the Schedules table, so you can tell them apart from full-library scans at a glance.

**Why stateless?** The scanner doesn't track a "last seen" timestamp. Every tick it asks Plex for items added within the lookback window and submits them to the job pipeline; the job runner's existing BIF-existence check skips anything that's already done. This avoids cursor migrations, restart races, and clock-skew bugs.

### Which option to pick

| | Plex direct webhook | Recently Added scanner |
|---|---|---|
| **Latency** | Instant (event-driven) | Up to your scan interval |
| **Plex Pass required?** | Yes | No |
| **Other Plex requirements?** | Mobile Push Notifications must be enabled | None |
| **Detects new items?** | Yes | Yes |
| **Detects in-place file upgrades?** | No | No |
| **Setup complexity** | One click after entering URL | Toggle + pick interval |
| **Network requirements** | Plex must be able to reach this app | This app must be able to reach Plex |

You can enable **both** if you want belt-and-suspenders behavior — the recently-added scan acts as a safety net for any `library.new` event Plex's push-notification code path might drop.

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

Yes! Windows supports GPU acceleration: NVIDIA GPUs use CUDA, and AMD/Intel GPUs use D3D11VA. Install the latest GPU drivers and it just works.

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

| Format | Method |
|--------|--------|
| HDR10 | zscale/tonemap (configurable algorithm, default: Hable) |
| HLG | zscale/tonemap (configurable algorithm, default: Hable) |
| HDR10+ (without Dolby Vision) | zscale/tonemap (configurable algorithm, default: Hable) |
| Dolby Vision Profile 7/8 (with HDR10 fallback) | zscale/tonemap via HDR10 base layer + HW decode ([#178](https://github.com/stevezau/plex_generate_vid_previews/issues/178)) |
| Dolby Vision Profile 5 (no backward-compat layer) | libplacebo via Vulkan; NVDEC on NVIDIA, software decode elsewhere ([#172](https://github.com/stevezau/plex_generate_vid_previews/issues/172), [#178](https://github.com/stevezau/plex_generate_vid_previews/issues/178)) |

**Dolby Vision Profile 5** (no backward-compatible HDR10 layer) requires `libplacebo` for tone mapping because the zscale/tonemap chain cannot read DV RPU reshaping metadata and produces dark or blank thumbnails. libplacebo's `apply_dolbyvision` (enabled by default in FFmpeg 8+) handles this correctly. On NVIDIA hosts, the HEVC decode step runs on NVDEC before libplacebo picks up the frames — this is about 3× faster than software decode on 4K DV5 content with identical visual output. Other vendors (Intel VAAPI, QSV, AMD, Apple VideoToolbox, D3D11VA) stay on software decode for this path because NVDEC is the only HW decoder currently validated with libplacebo's DV5 tone map; Intel VAAPI specifically benchmarked slower than software decode. If libplacebo/Vulkan is unavailable, the tool falls back to a basic filter chain without tone mapping.

> [!IMPORTANT]
> **NVIDIA users: `NVIDIA_DRIVER_CAPABILITIES` must include `graphics`.**
> libplacebo needs a working Vulkan driver to tone-map DV Profile 5. The NVIDIA Container Toolkit only injects the NVIDIA Vulkan ICD into the container when the `graphics` driver capability is declared — `compute,video,utility` is not enough (that only covers CUDA/NVDEC/nvidia-smi). If the app detects that your container is running Vulkan on the software rasterizer (`llvmpipe`), your DV Profile 5 thumbnails will contain a green rectangle due to a libplacebo+llvmpipe rendering bug.
>
> **Fix:** set `NVIDIA_DRIVER_CAPABILITIES=all` in your `docker run` (`-e NVIDIA_DRIVER_CAPABILITIES=all`) or `docker-compose.yml` (`environment:` block) and restart the container. `all` is the simplest value and is what the upstream `nvidia/vulkan` image uses. If you prefer minimum-privilege, use `compute,video,utility,graphics`.
>
> If the warning banner persists after the restart, your setup may be hitting one of the less-common causes (driver 570–579 regression, CDI manifest missing `libnvidia-glvkspirv.so`, or ICD JSON at the wrong path). The in-app warning will name the specific cause it detected. You can also open `GET /api/system/vulkan/debug` to fetch a plain-text diagnostic bundle to attach to a GitHub issue.

**Dolby Vision Profile 7/8** (with HDR10 fallback) uses the standard zscale/tonemap chain. FFmpeg reads the HDR10 base layer by default, so no libplacebo or special handling is needed.

**Non-DV HDR** content (HDR10, HLG, HDR10+) uses the zscale/tonemap chain with a configurable algorithm. The tone mapping algorithm can be changed in **Settings > Thumbnail Settings > HDR Tone Mapping** or via the `TONEMAP_ALGORITHM` environment variable. Available options: `hable` (default), `reinhard`, `mobius`, `clip`, `gamma`, `linear`. If your HDR thumbnails look too dark, try `reinhard`.

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

**Why is CPU usage high when I have a GPU configured?**

GPU workers use both GPU and CPU — this is normal. The GPU handles video decoding (via NVDEC, VAAPI, etc.) and tone mapping (via Vulkan/libplacebo for Dolby Vision content). The CPU handles frame selection, pixel format conversion, thumbnail scaling, and JPEG encoding.

For **standard (SDR) content**, GPU does nearly all the work and CPU usage is minimal — you'll see speeds of 500x or higher.

For **Dolby Vision content**, CPU usage is noticeably higher because frames must be moved between CPU and GPU memory for the libplacebo tone map. Expected speeds on 4K DV content:

- **DV Profile 7/8** (HDR10-compatible, e.g. `.DV.HDR10Plus.h265`) — 15–60x+ across all GPU vendors. Uses HW decode + zscale on the HDR10 base layer.
- **DV Profile 5** (no HDR10 fallback, e.g. `.DV.h265` with no HDR10 marker) — about 12x on NVIDIA (NVDEC decode + libplacebo), about 4x on Intel / AMD / Apple / CPU (software decode + libplacebo).

The **FFmpeg Threads** setting per GPU controls how many CPU cores each worker can use. If you're running multiple GPU workers and seeing CPU contention, lower this value.

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

Docker Desktop's GPU passthrough (via WSL2) is not currently supported by this tool. For Windows with GPU acceleration, run natively (CUDA for NVIDIA, D3D11VA for AMD/Intel) instead of Docker.

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
| DV Profile 5 thumbnails have a bright green rectangle or overall green cast | libplacebo is falling back to `llvmpipe` (software Vulkan) because the container has no real Vulkan device | Forward an iGPU or render node to the container with `--device /dev/dri:/dev/dri` (Intel/AMD) or ensure the NVIDIA runtime exposes the Vulkan ICD with `NVIDIA_DRIVER_CAPABILITIES=compute,video,utility,graphics`. Most users already forward `/dev/dri` for VAAPI, which brings Mesa's Vulkan driver along for free. |

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
