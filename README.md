<!-- PROJECT SHIELDS -->
<div align="center">

[![Contributors][contributors-shield]][contributors-url]
[![Forks][forks-shield]][forks-url]
[![Stargazers][stars-shield]][stars-url]
[![Issues][issues-shield]][issues-url]
[![MIT License][license-shield]][license-url]
[![Docker Pulls][docker-shield]][docker-url]
[![codecov][codecov-shield]][codecov-url]
[![AI-Assisted][ai-shield]][ai-url]

</div>

<!-- PROJECT LOGO -->
<div align="center">
  <img src="docs/images/icon.svg" alt="Logo" width="120" height="120">

  <h1 align="center">Plex Generate Previews</h1>

  <p align="center">
    GPU-accelerated video preview thumbnail generation for Plex Media Server
    <br />
    <a href="docs/README.md"><strong>Explore the docs</strong></a>
    <br />
    <br />
    <a href="#quick-start">Quick Start</a>
    &middot;
    <a href="https://github.com/stevezau/plex_generate_vid_previews/issues/new?labels=bug">Report Bug</a>
    &middot;
    <a href="https://github.com/stevezau/plex_generate_vid_previews/issues/new?labels=enhancement">Request Feature</a>
  </p>
</div>

---

## About

Generates video preview thumbnails for **Plex, Emby, and Jellyfin**. These are the small images you see when scrubbing through videos in any of those servers.

**The Problem:** Server-side preview generation is painfully slow — Plex's is single-threaded software-decoded, Emby has no GPU acceleration at all, and Jellyfin's HW-accelerated trickplay is buggy/slow on many systems.

**The Solution:** This tool uses GPU acceleration and parallel processing to generate previews **5-10x faster**, and can drive any number of Plex / Emby / Jellyfin servers from a single instance — each new file is processed once and the result is published to every server that owns it, in the format that server expects (BIF for Plex/Emby, native JPG tile-grid for Jellyfin).

> [!NOTE]
> This project was originally hand-written. Recent development is AI-assisted (Cursor + Claude). All changes are reviewed and tested.

---

## Features

| Feature | Description |
|---------|-------------|
| **Multi-Vendor** | Plex, Emby, and Jellyfin — any combination, any number of each ([guide](docs/multi-server.md)) |
| **One Pass, Many Servers** | A single FFmpeg pass produces output for every server that owns the file |
| **Multi-GPU** | NVIDIA, AMD, Intel, and Windows GPUs |
| **Parallel Processing** | Configurable GPU and CPU worker threads |
| **GPU to CPU Fallback** | Automatic in-place CPU retry when a GPU worker hits an unsupported codec |
| **Hardware Acceleration** | CUDA, VAAPI, D3D11VA, VideoToolbox |
| **Library Filtering** | Per-library enable/disable per server |
| **Quality Control** | Adjustable thumbnail quality (1-10) |
| **Docker Ready** | Pre-built images with GPU support |
| **Web Dashboard** | Manage jobs, schedules, and status |
| **Scheduling** | Cron and interval-based automation |
| **Smart Skipping** | Automatically skips files that already have thumbnails |
| **Smart Dedup Journal** | A `.meta` sidecar records source `(mtime, size)` so late-arriving webhooks short-circuit FFmpeg entirely; Sonarr quality upgrades correctly trigger regen |
| **Slow-Backoff Retries** | Plex not yet scanned? The dispatcher schedules 30 s → 2 m → 5 m → 15 m → 60 m retries automatically — no manual re-run needed |
| **Universal Webhooks** | One URL handles Plex / Emby / Jellyfin / Sonarr / Radarr — vendor auto-detected |
| **Plex direct webhook** | Auto-trigger on `library.new` (Plex Pass) for media added without Sonarr/Radarr |
| **Plex multi-server discovery** | One Plex.tv OAuth sign-in lists every server your account can access — tick multiple to add them all at once |
| **Jellyfin Quick Connect** | Friendliest auth — no password ever leaves the user's browser |
| **Jellyfin trickplay one-click fix** | Detects + auto-flips Jellyfin's `EnableTrickplayImageExtraction` flag (the most common gotcha) so the previews you publish actually appear in Jellyfin's web UI |
| **Multi-Server BIF Viewer** | Inspect published previews per-server in the browser — works for Plex bundle BIFs, Emby sidecar BIFs, and Jellyfin trickplay tile-grid sheets |
| **Recently Added scanner** | Polling fallback that catches manually-added items without Plex Pass |

---

## Screenshots

| Home | Settings | Webhooks |
|:----:|:--------:|:--------:|
| [![Home](docs/images/home.png)](docs/images/home.png) | [![Settings](docs/images/settings.png)](docs/images/settings.png) | [![Webhooks](docs/images/webhooks.png)](docs/images/webhooks.png) |

*Web UI: dashboard and job management, configuration and GPU detection, Radarr/Sonarr webhook setup.*

---

## Quick Start

### Docker (Recommended)

```bash
docker run -d \
  --name plex-generate-previews \
  --restart unless-stopped \
  -p 8080:8080 \
  --device /dev/dri:/dev/dri \
  -e PUID=1000 \
  -e PGID=1000 \
  -v /path/to/media:/media:ro \
  -v /path/to/plex/config:/plex:rw \
  -v /path/to/app/config:/config:rw \
  -v /etc/localtime:/etc/localtime:ro \
  stevezzau/plex_generate_vid_previews:latest
```

Replace `/path/to/media`, `/path/to/plex/config`, and `/path/to/app/config` with your actual paths.

> **Timezone:** The `/etc/localtime` mount ensures log timestamps and scheduled jobs use your local time. Alternatively, use `-e TZ=America/New_York` (replace with your [timezone](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)).

Then open `http://YOUR_IP:8080`, retrieve the authentication token from container logs, and complete the setup wizard.

For Docker Compose, Unraid, and GPU-specific setup:

- [Getting Started](docs/getting-started.md)
- [Configuration & API Reference](docs/reference.md)

---

## Installation

| Method | Best For | Guide |
|--------|----------|-------|
| **Docker** | Most users, easy GPU setup | [Getting Started](docs/getting-started.md) |
| **Docker Compose** | Managed deployments | [docker-compose.example.yml](docker-compose.example.yml) |
| **Unraid** | Unraid servers | [Getting Started — Unraid](docs/getting-started.md#unraid) |

- **Web UI only:** The Docker image runs the web interface. There is no CLI; all configuration and job management is done via the web UI.
- **PyPI:** The package is no longer published on PyPI; use Docker or install from source.

> [!IMPORTANT]
> The Docker Hub image is published as `stevezzau/plex_generate_vid_previews` (double-`z`):
> [stevezzau/plex_generate_vid_previews](https://hub.docker.com/r/stevezzau/plex_generate_vid_previews).

---

## GPU Support

| Platform | Supported GPUs | Via |
|---|---|---|
| **Linux (Docker)** | NVIDIA, AMD, Intel | CUDA/NVENC, VAAPI, QuickSync |
| **Windows (native)** | NVIDIA, AMD, Intel | CUDA, D3D11VA |
| **macOS (native)** | Apple Silicon, Intel | VideoToolbox |
| **Linux / Windows / macOS** | No GPU | CPU workers only |

On Docker Desktop (Windows/WSL2 and macOS) the container runs inside a Linux VM, so D3D11VA and VideoToolbox aren't reachable — Docker on those platforms processes on CPU. For GPU acceleration on Windows or macOS, install from source.

See [Getting Started — GPU Acceleration](docs/getting-started.md#gpu-acceleration) for per-vendor setup, tuning, and detection. Detected GPUs are shown in the web UI under **Settings** or **Setup**.

### GPU + CPU Fallback

CPU fallback is automatic and built into every GPU worker — there is no separate "fallback" pool to configure. If FFmpeg fails on the GPU (unsupported codec, hardware-accelerator error, driver crash), the same worker retries the file on CPU in-place and the dashboard shows a yellow **CPU fallback** badge.

If you have a lot of content that never decodes on the GPU, raise **CPU Workers** above `0` so that those files route straight to dedicated CPU workers instead of blocking a GPU worker each time.

See [Automatic GPU → CPU Fallback](docs/guides.md#automatic-gpu--cpu-fallback) for details.

---

## Documentation

| Document | What's there |
|---|---|
| [Documentation Hub](docs/README.md) | Pick the right doc for your task |
| [Getting Started](docs/getting-started.md) | Install with Docker, GPU setup, Unraid, networking |
| [Guides](docs/guides.md) | Web UI, schedules, webhooks, HDR handling, troubleshooting |
| [Reference](docs/reference.md) | Config options, env vars, REST API, WebSocket events |
| [FAQ](docs/faq.md) | Common questions about setup, performance, and compatibility |

---

## Built With

<div align="center">

[![Python][python-shield]][python-url]
[![Docker][docker-tech-shield]][docker-tech-url]
[![FFmpeg][ffmpeg-shield]][ffmpeg-url]
[![Flask][flask-shield]][flask-url]
[![Gunicorn][gunicorn-shield]][gunicorn-url]

</div>

---

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for local setup, tests, code style, and the PR workflow.

---

## License

Distributed under the MIT License. See [LICENSE](LICENSE) for details.

---

## Acknowledgments

- [Plex](https://www.plex.tv/) for the media server
- [FFmpeg](https://ffmpeg.org/) for video processing
- [LinuxServer.io](https://www.linuxserver.io/) for the Docker base image
- [Rich](https://github.com/Textualize/rich) for beautiful terminal output
- All contributors and users

---

<div align="center">

Made with care by [stevezau](https://github.com/stevezau)

Star this repo if you find it useful!

</div>

<!-- MARKDOWN LINKS & IMAGES -->
[contributors-shield]: https://img.shields.io/github/contributors/stevezau/plex_generate_vid_previews.svg?style=for-the-badge
[contributors-url]: https://github.com/stevezau/plex_generate_vid_previews/graphs/contributors
[forks-shield]: https://img.shields.io/github/forks/stevezau/plex_generate_vid_previews.svg?style=for-the-badge
[forks-url]: https://github.com/stevezau/plex_generate_vid_previews/network/members
[stars-shield]: https://img.shields.io/github/stars/stevezau/plex_generate_vid_previews.svg?style=for-the-badge
[stars-url]: https://github.com/stevezau/plex_generate_vid_previews/stargazers
[issues-shield]: https://img.shields.io/github/issues/stevezau/plex_generate_vid_previews.svg?style=for-the-badge
[issues-url]: https://github.com/stevezau/plex_generate_vid_previews/issues
[license-shield]: https://img.shields.io/github/license/stevezau/plex_generate_vid_previews.svg?style=for-the-badge
[license-url]: https://github.com/stevezau/plex_generate_vid_previews/blob/main/LICENSE
[docker-shield]: https://img.shields.io/docker/pulls/stevezzau/plex_generate_vid_previews?style=for-the-badge
[docker-url]: https://hub.docker.com/r/stevezzau/plex_generate_vid_previews
[codecov-shield]: https://img.shields.io/codecov/c/github/stevezau/plex_generate_vid_previews?style=for-the-badge
[codecov-url]: https://codecov.io/gh/stevezau/plex_generate_vid_previews

[ai-shield]: https://img.shields.io/badge/AI--Assisted-Cursor%20%2B%20Claude-blue?style=for-the-badge&logo=openai&logoColor=white
[ai-url]: #about

[python-shield]: https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white
[python-url]: https://python.org
[docker-tech-shield]: https://img.shields.io/badge/Docker-2496ED?style=for-the-badge&logo=docker&logoColor=white
[docker-tech-url]: https://docker.com
[ffmpeg-shield]: https://img.shields.io/badge/FFmpeg-007808?style=for-the-badge&logo=ffmpeg&logoColor=white
[ffmpeg-url]: https://ffmpeg.org
[flask-shield]: https://img.shields.io/badge/Flask-000000?style=for-the-badge&logo=flask&logoColor=white
[flask-url]: https://flask.palletsprojects.com
[gunicorn-shield]: https://img.shields.io/badge/Gunicorn-499848?style=for-the-badge&logo=gunicorn&logoColor=white
[gunicorn-url]: https://gunicorn.org
