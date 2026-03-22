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

Generates video preview thumbnails (BIF files) for Plex Media Server. These are the small images you see when scrubbing through videos in Plex.

**The Problem:** Plex's built-in preview generation is painfully slow.

**The Solution:** This tool uses GPU acceleration and parallel processing to generate previews **5-10x faster**.

> [!NOTE]
> This project was originally hand-written. Recent development is AI-assisted (Cursor + Claude). All changes are reviewed and tested.

---

## Features

| Feature | Description |
|---------|-------------|
| **Multi-GPU** | NVIDIA, AMD, Intel, and Windows GPUs |
| **Parallel Processing** | Configurable GPU and CPU worker threads |
| **GPU to CPU Fallback** | Optional fallback-only CPU workers for GPU decode failures |
| **Hardware Acceleration** | CUDA, VAAPI, D3D11VA, VideoToolbox |
| **Library Filtering** | Process specific Plex libraries |
| **Quality Control** | Adjustable thumbnail quality (1-10) |
| **Docker Ready** | Pre-built images with GPU support |
| **Web Dashboard** | Manage jobs, schedules, and status |
| **Scheduling** | Cron and interval-based automation |
| **Smart Skipping** | Automatically skips files that already have thumbnails |
| **Radarr/Sonarr** | Webhook integration for auto-processing on import |

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
> Note the extra "z" in Docker Hub: [stevezzau/plex_generate_vid_previews](https://hub.docker.com/r/stevezzau/plex_generate_vid_previews)
> (stevezau was taken)

---

## GPU Support

| GPU Type | Platform | Acceleration | Docker |
|----------|----------|--------------|--------|
| **NVIDIA** | Linux | CUDA/NVENC | `--gpus all` |
| **AMD** | Linux | VAAPI | `--device /dev/dri` |
| **Intel** | Linux | QuickSync/VAAPI | `--device /dev/dri` |
| **NVIDIA** | Windows | CUDA | Native only |
| **AMD/Intel** | Windows | D3D11VA | Native only |
| **Apple Silicon** | macOS | VideoToolbox | Native only |

> **"Native only"** means GPU acceleration requires running the app from source on that platform. Docker on Windows (WSL2) and macOS runs a Linux VM — D3D11VA and VideoToolbox are not available inside Docker. Docker on these platforms will use CPU-only processing. Apple Silicon users benefit from the native ARM64 Docker image (no Rosetta overhead).

For complete GPU setup, tuning, and troubleshooting:

- [Getting Started — GPU Acceleration](docs/getting-started.md#gpu-acceleration)
- [Guides & Troubleshooting](docs/guides.md#troubleshooting)

**Check detected GPUs:** Open the web UI (http://YOUR_IP:8080) and go to **Settings** or **Setup** — detected GPUs are shown there.

### GPU + CPU Fallback Mode

If you want GPU-only main processing but still want CPU recovery for unsupported files:

- Set **CPU Workers** to `0`
- Set **CPU Fallback Workers** to `1` (or higher)

This keeps normal jobs on GPU workers and only uses CPU when a GPU worker reports an unsupported codec/runtime decode failure.

> [!NOTE]
> `CPU Fallback Workers` is only used when `CPU Workers=0`.
> If `CPU Workers>0`, regular CPU workers already handle fallback work.

---

## Documentation

| Document | Description |
|----------|-------------|
| [Documentation Hub](docs/README.md) | Start here — architecture diagrams |
| [Getting Started](docs/getting-started.md) | Docker, GPU, Unraid, devcontainer |
| [Reference](docs/reference.md) | Configuration options & REST API |
| [Guides](docs/guides.md) | Web interface, webhooks, FAQ, troubleshooting |

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

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit changes (`git commit -m 'Add amazing feature'`)
4. Push to branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

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
