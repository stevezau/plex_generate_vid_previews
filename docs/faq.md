# FAQ

> [Back to Docs](README.md)

Common questions about setup, usage, and behavior. For troubleshooting specific errors, see [Guides — Troubleshooting](guides.md#troubleshooting). For HDR and Dolby Vision behavior, see [Guides — HDR & Dolby Vision](guides.md#hdr--dolby-vision).

## Contents

- [General](#general)
- [GPUs](#gpus)
- [Performance](#performance)
- [Docker](#docker)
- [Processing](#processing)

## Related Docs

- [Getting Started](getting-started.md)
- [Guides & Troubleshooting](guides.md)
- [Configuration & API Reference](reference.md)

---

## General

**What does this tool do?**

Generates video preview thumbnails (BIF files) for Plex Media Server. These are the small images you see when scrubbing through videos. Plex's built-in generation is slow — this tool makes it 5-10x faster using GPU acceleration.

**What Plex settings should I use?**

In Plex Settings → Library, set **"Generate video preview thumbnails"** to **Never**. This tool replaces Plex's built-in generation. Disabling it in Plex avoids duplicate work and prevents Plex from using CPU for thumbnails when you want this app to handle them.

**Does this generate chapter thumbnails?**

No. This tool only generates **video preview thumbnails** (BIF files for timeline scrubbing). It does not generate chapter thumbnails, intro/credit detection, or other Plex media analysis.

**Does this work on Windows?**

Yes. Windows supports GPU acceleration: NVIDIA GPUs use CUDA, and AMD/Intel GPUs use D3D11VA. Install the latest GPU drivers and it just works — but you need to run from source, not Docker (Docker Desktop on Windows runs a Linux VM that can't reach those accelerators).

**Can I use this without a GPU?**

Yes. In **Settings** → **Processing Options**, disable all GPUs (or set workers to 0) and set **CPU Workers** to your desired value (e.g. `4` or `8`).

**Is Docker required? Is there a standalone .exe?**

Docker is the recommended and supported way to run this tool. There is no standalone executable. Advanced users can install from source on Linux (requires Python 3.10+, FFmpeg, and mediainfo), but this is not officially supported. See [Getting Started](getting-started.md) for Docker setup.

**Does Plex need to run in Docker too?**

No. Plex can run bare-metal, in Docker, or any other way. This tool just needs network access to the Plex API and read/write access to the Plex application data directory (where BIF files are stored).

**Can I run this on a different machine than my Plex server?**

Yes, as long as the tool can reach the Plex API over the network and both machines have access to the media files and Plex config directory (e.g. via NFS or SMB mounts). See [Networking](getting-started.md#networking) for setup details.

**Does this work with Jellyfin or Emby?**

No. This tool is Plex-only — it generates Plex-specific BIF files and uses the Plex API to discover libraries and media items.

---

## GPUs

**How do I know which GPUs are detected?**

Open **Settings** → **Processing Options**. The GPU panel lists all detected GPUs with their device IDs, names, and types.

**Can I use multiple GPUs?**

Yes. In **Settings** → **Processing Options**, enable individual GPUs and set workers and FFmpeg threads per GPU. Each GPU can be enabled/disabled independently.

**Which GPU should I use?**

| GPU Type | Best For |
|----------|----------|
| NVIDIA | Fastest for video processing |
| Intel iGPU | Great for low-power setups, common on Unraid |
| AMD | Good VAAPI support on Linux |
| CPU-only | Works everywhere, slower |

**Does GPU passthrough work with Docker Desktop on Windows?**

Docker Desktop's GPU passthrough (via WSL2) is not currently supported by this tool. For Windows with GPU acceleration, run natively (CUDA for NVIDIA, D3D11VA for AMD/Intel) instead of Docker.

**HDR / Dolby Vision support?**

See the dedicated [HDR & Dolby Vision](guides.md#hdr--dolby-vision) section in Guides for the full per-vendor breakdown and expected speeds.

---

## Performance

**How many threads should I use?**

Start with the defaults and increase gradually while monitoring system load. See the [Performance Tuning](getting-started.md#performance-tuning) table in Getting Started for concrete starting points across hardware tiers.

**Why is CPU usage high when I have a GPU configured?**

GPU workers use both GPU and CPU — this is normal. The GPU handles video decoding (via NVDEC, VAAPI, etc.), downscaling to thumbnail size (via `scale_cuda` / `scale_vaapi`), and tone mapping for Dolby Vision Profile 5 (via Vulkan/libplacebo). The CPU handles frame selection, the final HDR10 tone-map pass (if applicable), and JPEG encoding.

For **standard (SDR) content**, the GPU does nearly all the work and CPU usage is minimal — you'll see speeds of 500× or higher.

For **Dolby Vision content**, CPU usage is noticeably higher because frames must be moved between CPU and GPU memory for the libplacebo tone map. Expected speeds on 4K DV content:

- **DV Profile 7/8** (HDR10-compatible, e.g. `.DV.HDR10Plus.h265`) — 15–60× across all GPU vendors. Uses HW decode + GPU downscale + zscale tonemap on the 320×240 frame.
- **DV Profile 5** (no HDR10 fallback, e.g. `.DV.h265` with no HDR10 marker):
  - **Intel** (iGPU, Arc): ~17× via VAAPI decode + OpenCL tonemap. Requires `intel-opencl-icd` (already in the image) and a render node (`--device /dev/dri:/dev/dri`).
  - **NVIDIA**: ~10–16× via NVDEC + Vulkan libplacebo. Needs `NVIDIA_DRIVER_CAPABILITIES=all` (or at minimum `compute,video,utility,graphics`) so the NVIDIA Vulkan ICD reaches the container.
  - **AMD / Apple / CPU-only**: ~5–10× via software decode + libplacebo.

The **FFmpeg Threads** setting per GPU controls how many CPU cores each worker can use. If you're running multiple GPU workers and seeing CPU contention, lower this value.

**How much RAM does each worker use?**

Typical per-worker RSS with hardware decode:

| Content | Per-worker RSS |
|---|---|
| SDR 1080p | ~90–200 MB |
| 4K HDR10 / DV P7+8 | ~250–300 MB |
| 4K DV Profile 5 (libplacebo) | ~350–500 MB |

Earlier builds held ~1 GB per worker on 4K HDR content because decoded frames were downloaded from the GPU at source resolution. As of the GPU-scale fix ([#218](https://github.com/stevezau/media_preview_generator/issues/218)), the downscale runs on the GPU and only the 320×240 frame is moved back to system RAM, so the memory ceiling on an 8 GB container comfortably supports 12+ GPU workers.

**What's thumbnail quality 1-10?**

Lower numbers = higher quality but larger file sizes.

- Quality 2 = highest quality
- Quality 4 = default (good balance)
- Quality 10 = lowest quality

**Generation feels disk-bound on my multi-disk setup (unraid/mergerfs/JBOD) — how do I speed it up?**

On setups where one share is backed by multiple physical disks (unraid's `shfs`, mergerfs, JBOD), parallel workers processing files in alphabetical order tend to pile onto one disk at a time. Open the **New Job** modal (or edit a full-library schedule) and set **Processing Order** to **Random**. Workers will pull items from different disks in parallel, so disk read throughput — not GPU — sets the ceiling. Webhook jobs and Recently Added scans don't expose this setting because they only touch a handful of files where ordering doesn't matter. See [Issue #219](https://github.com/stevezau/media_preview_generator/issues/219) for background.

---

## Docker

**Why does my container fail to start?**

Most common cause: using `init: true` in docker-compose. Remove it — this container uses s6-overlay (a built-in process manager) and `init: true` conflicts with it.

**Why can't the container find my files?**

Path mapping issue. See [Path Mappings](reference.md#path-mappings).

**How do I get the authentication token?**

Use [Authentication Token](getting-started.md#authentication-token).

**Windows: paths in config must use forward slashes**

On Windows, use forward slashes (`/`) in all path configuration (environment variables, `.env` files, Settings). Backslashes (`\`) will cause path resolution failures.

---

## Processing

**Can I process specific libraries only?**

Yes. In **Settings** → **Libraries**, select which libraries to process.

**How do I regenerate existing thumbnails?**

When starting a job, use the **Regenerate** option to force regeneration of existing thumbnails.

**Why is it "skipping" some files?**

Possible causes:

- Thumbnails already exist (use the **Regenerate** option when starting a job to force)
- File not found (check [path mappings](reference.md#path-mappings))
- Invalid file format

**Why does ETA show "Calculating..." for so long?**

The ETA calculation is designed to be **accurate, not fast**:

1. **Initial skip burst (0–30 seconds)** — shows "Calculating…"; many files may already have thumbnails and are skipped instantly.
2. **First few items processed (30s–5 min)** — still shows "Calculating…"; real FFmpeg encoding is underway, but not enough data yet.
3. **Realistic estimate appears (5+ min)** — shows time like "8h 30m"; calculated from actual per-item processing time, updates every 3 seconds.
4. **During processing** — ETA counts down and adjusts in real-time as processing rate varies.

Early ETA guesses based on incomplete data are wildly inaccurate. The "Calculating…" phase filters out this noise.

**What is the Sonarr/Radarr path column for?**

Only relevant if you use [webhook integration](guides.md#webhook-integration). When Sonarr/Radarr fire a webhook, they include the file path as *they* see it inside their container, which may differ from the path inside this tool's container. The path column translates between them. For example:

| Container | Might see the file as |
|-----------|----------------------|
| Plex | `/data/tv/Show/episode.mkv` |
| Sonarr | `/tv/Show/episode.mkv` |
| This tool | `/mnt/media/tv/Show/episode.mkv` |

If you are not using webhooks, or all containers use the same media paths, leave it blank.

---

[Back to Docs](README.md) | [Main README](../README.md)
