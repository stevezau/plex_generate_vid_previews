# [stevezau/plex_generate_vid_previews](https://github.com/stevezau/plex_generate_vid_previews/)

[![Version](https://img.shields.io/badge/version-2.0.0-blue.svg)](https://github.com/stevezau/plex_generate_vid_previews)
[![Python](https://img.shields.io/badge/python-3.7+-green.svg)](https://python.org)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/docker-available-blue.svg)](https://hub.docker.com/repository/docker/stevezzau/plex_generate_vid_previews)

## Table of Contents

- [Why Use This Tool?](#why-use-this-tool)
- [Features](#features)
- [Requirements](#requirements)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
  - [Command-line Arguments](#command-line-arguments)
  - [Environment Variables](#environment-variables)
- [GPU Support](#gpu-support)
- [Installation & Usage](#installation--usage)
  - [Docker](#docker)
  - [Local Installation](#local-installation)
  - [Unraid](#unraid)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)

## Why Use This Tool?

Plex's built-in preview thumbnail generation can be painfully slow, especially for large media libraries. This tool dramatically speeds up the process by leveraging your GPU's hardware acceleration and parallel processing. Instead of waiting hours or days for Plex to generate previews, you can process thousands of videos in minutes.

**What are preview thumbnails?** They're the small images you see when scrubbing through videos in Plex - they make navigation much faster and more intuitive.

**Perfect for:**
- **Large Libraries**: Process thousands of movies and TV shows quickly
- **New Plex Setups**: Generate all previews at once after adding media
- **Library Migrations**: Rebuild previews when moving or reorganizing libraries
- **Performance Optimization**: Use GPU acceleration for maximum speed
- **Remote Processing**: Generate previews on a different machine than your Plex server

**Key Benefits:**
- **10-50x faster** than Plex's built-in generation
- **GPU acceleration** for maximum performance
- **Parallel processing** with configurable worker threads
- **Smart detection** of existing previews to avoid duplicates
- **Production ready** with comprehensive error handling and logging

## Features

🚀 **High-Performance Plex Video Preview Generation**
- **Multi-GPU Support**: Automatically detects and utilizes NVIDIA, AMD, Intel, and WSL2 GPUs
- **Parallel Processing**: Configurable GPU and CPU worker threads for maximum throughput
- **Smart GPU Selection**: Choose specific GPUs or use all available hardware
- **Hardware Acceleration**: CUDA, VAAPI, QSV, and D3D11VA support for optimal performance

🎯 **Advanced Configuration**
- **Flexible Input**: Command-line arguments with environment variable fallbacks
- **Library Filtering**: Process specific Plex libraries (Movies, TV Shows, etc.)
- **Quality Control**: Adjustable thumbnail quality (1-10) and frame intervals
- **Path Mapping**: Support for complex media library setups and remote servers

🔧 **Production Ready**
- **Rich Progress Display**: Real-time FFmpeg progress with animated progress bars
- **Comprehensive Logging**: Debug, info, warning, and error levels with structured output
- **Graceful Shutdown**: Proper cleanup and signal handling
- **Error Recovery**: Robust error handling and validation

📦 **Deployment Options**
- **Docker Support**: Pre-built images with GPU acceleration
- **Cross-Platform**: Linux, WSL2, and containerized environments
- **Zero Dependencies**: Self-contained with minimal external requirements
- **Configuration Validation**: Detailed error messages and setup guidance

## Requirements

- **Plex Media Server**: Running and accessible
- **FFmpeg 7.0+**: For video processing and hardware acceleration
- **Python 3.7+**: For local installation
- **Docker**: For containerized deployment (optional)

### GPU Requirements
- **NVIDIA**: CUDA-compatible GPU + NVIDIA drivers
- **AMD**: ROCm-compatible GPU + amdgpu drivers  
- **Intel**: QSV or VAAPI-compatible iGPU/dGPU
- **WSL2**: D3D11VA-compatible GPU (Intel Arc, etc.)

## Quick Start

### Docker (Recommended)
```bash
# List available GPUs
docker run --rm stevezzau/plex_generate_vid_previews:latest --list-gpus

# Basic usage
docker run --rm --gpus all \
  -e PLEX_URL=http://localhost:32400 \
  -e PLEX_TOKEN=your-token \
  -e PLEX_CONFIG_FOLDER=/config/plex/Library/Application\ Support/Plex\ Media\ Server \
  -v /path/to/plex/config:/config/plex \
  -v /path/to/media:/media \
  stevezzau/plex_generate_vid_previews:latest
```

### Local Installation
```bash
# Install from GitHub
pip install git+https://github.com/stevezau/plex_generate_vid_previews.git

# Basic usage
plex-generate-previews \
  --plex-url http://localhost:32400 \
  --plex-token your-token \
  --plex-config-folder /path/to/plex/Library/Application\ Support/Plex\ Media\ Server
```

## Configuration

You can configure the application using either **command-line arguments** or **environment variables**. CLI arguments take precedence over environment variables, allowing you to override settings for specific runs.

### Command-line Arguments

All configuration options are available as command-line arguments. Run `plex-generate-previews --help` to see all available options:

```bash
# Basic usage with CLI arguments
plex-generate-previews --plex-url http://localhost:32400 --plex-token YOUR_TOKEN

# Override specific settings
plex-generate-previews --gpu-threads 8 --log-level DEBUG

# Mix CLI args with environment variables (CLI takes precedence)
PLEX_URL=http://localhost:32400 plex-generate-previews --plex-token DIFFERENT_TOKEN
```

### Environment Variables

You can customize various settings by modifying the environment variables. If you are running locally you can create
a `.env` file. **Note:** Command-line arguments take precedence over environment variables.

**Precedence Order:**
1. Command-line arguments (highest priority)
2. Environment variables
3. Default values (lowest priority)

#### Plex Server Configuration

| Variable | CLI Argument | Description | Default |
|----------|--------------|-------------|---------|
| `PLEX_URL` | `--plex-url` | Plex server URL (e.g., http://localhost:32400) | *Required* |
| `PLEX_TOKEN` | `--plex-token` | Plex authentication token ([how to get](https://support.plex.tv/articles/204059436/)) | *Required* |
| `PLEX_TIMEOUT` | `--plex-timeout` | Plex API timeout in seconds | 60 |
| `PLEX_LIBRARIES` | `--plex-libraries` | Comma-separated library names (e.g., "Movies, TV Shows") | All libraries |

#### Media Paths

| Variable | CLI Argument | Description | Default |
|----------|--------------|-------------|---------|
| `PLEX_CONFIG_FOLDER` | `--plex-config-folder` | Path to Plex Media Server config folder | *Required* |
| `PLEX_LOCAL_VIDEOS_PATH_MAPPING` | `--plex-local-videos-path-mapping` | Local videos path mapping | Empty |
| `PLEX_VIDEOS_PATH_MAPPING` | `--plex-videos-path-mapping` | Plex videos path mapping | Empty |

#### Processing Configuration

| Variable | CLI Argument | Description | Default |
|----------|--------------|-------------|---------|
| `PLEX_BIF_FRAME_INTERVAL` | `--plex-bif-frame-interval` | Interval between preview images (1-60 seconds) | 5 |
| `THUMBNAIL_QUALITY` | `--thumbnail-quality` | Preview quality 1-10 (2=highest, 10=lowest) | 4 |
| `REGENERATE_THUMBNAILS` | `--regenerate-thumbnails` | Regenerate existing thumbnails | false |

#### Threading Configuration

| Variable | CLI Argument | Description | Default |
|----------|--------------|-------------|---------|
| `GPU_THREADS` | `--gpu-threads` | Number of GPU worker threads (0-32) | 4 |
| `CPU_THREADS` | `--cpu-threads` | Number of CPU worker threads (0-32) | 4 |
| `GPU_SELECTION` | `--gpu-selection` | GPU selection: "all" or "0,1,2" | "all" |

#### System Configuration

| Variable | CLI Argument | Description | Default |
|----------|--------------|-------------|---------|
| `TMP_FOLDER` | `--tmp-folder` | Temporary folder for processing | /tmp/plex_generate_previews |
| `LOG_LEVEL` | `--log-level` | Logging level (DEBUG, INFO, WARNING, ERROR) | INFO |

#### Special Commands

| Command | Description |
|---------|-------------|
| `--list-gpus` | List detected GPUs and exit |
| `--help` | Show help message and exit |

## GPU Support

The tool automatically detects and supports multiple GPU types with hardware acceleration:

### Supported GPU Types

| GPU Type | Acceleration | Requirements | Docker Support |
|----------|--------------|--------------|----------------|
| **NVIDIA** | CUDA | NVIDIA drivers + CUDA toolkit | ✅ NVIDIA Container Toolkit |
| **AMD** | VAAPI | amdgpu drivers + ROCm | ✅ ROCm Docker support |
| **Intel** | QSV/VAAPI | Intel drivers + Media SDK | ✅ Device access |
| **WSL2** | D3D11VA | WSL2 + compatible GPU | ✅ Native WSL2 |

### GPU Detection

The tool automatically detects available GPUs and their capabilities:

```bash
# List all detected GPUs
plex-generate-previews --list-gpus

# Example output:
# ✅ Found 2 GPU(s):
#   [0] NVIDIA GeForce RTX 4090 (CUDA)
#   [1] Intel UHD Graphics 770 (QSV)
```

### Multi-GPU Support

Configure GPU usage with the `--gpu-selection` parameter:

```bash
# Use all detected GPUs (default)
plex-generate-previews --gpu-selection all

# Use specific GPUs by index
plex-generate-previews --gpu-selection "0,2"

# Use only the first GPU
plex-generate-previews --gpu-selection "0"
```

### Hardware Acceleration Methods

- **NVIDIA**: Uses CUDA for maximum performance
- **AMD**: Uses VAAPI with ROCm drivers
- **Intel**: Uses QSV (Quick Sync Video) or VAAPI
- **WSL2**: Uses D3D11VA for Windows GPU passthrough

### Docker GPU Requirements

#### NVIDIA
Install the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) on your host system.

#### AMD
Follow the [ROCm Docker setup guide](https://rocm.docs.amd.com/en/docs-5.0.2/deploy/docker.html) for container GPU access.

#### Intel
Ensure the container has access to `/dev/dri` devices and the render group:

```yaml
services:
  previews:
    user: 1000:1000
    group_add:
      - 109  # render group GID
    devices:
      - /dev/dri:/dev/dri
```

#### WSL2
No special Docker configuration needed - automatically detects WSL2 GPUs.

## Installation & Usage

### Docker

> [!IMPORTANT]  
> Note the extra "z" in the Docker Hub URL: [stevezzau/plex_generate_vid_previews](https://hub.docker.com/repository/docker/stevezzau/plex_generate_vid_previews)  
> (stevezau was already taken on Docker Hub)

#### Docker Compose

**NVIDIA GPU Example:**
```yaml
version: '3.8'
services:
  previews:
    image: stevezzau/plex_generate_vid_previews:latest
    user: 1000:1000
    environment:
      - PLEX_URL=http://localhost:32400
      - PLEX_TOKEN=your-plex-token
      - PLEX_CONFIG_FOLDER=/config/plex/Library/Application Support/Plex Media Server
      - PLEX_BIF_FRAME_INTERVAL=5
      - THUMBNAIL_QUALITY=4
      - GPU_THREADS=4
      - CPU_THREADS=4
      - LOG_LEVEL=INFO
    volumes:
      - /path/to/plex/config:/config/plex
      - /path/to/media:/media
    deploy:
      resources:
        reservations:
          devices:
            - capabilities: [gpu]
    runtime: nvidia
```

**AMD GPU Example:**
```yaml
version: '3.8'
services:
  previews:
    image: stevezzau/plex_generate_vid_previews:latest
    user: 1000:1000
    environment:
      - PLEX_URL=http://localhost:32400
      - PLEX_TOKEN=your-plex-token
      - PLEX_CONFIG_FOLDER=/config/plex/Library/Application Support/Plex Media Server
    volumes:
      - /path/to/plex/config:/config/plex
      - /path/to/media:/media
    devices:
      - /dev/dri:/dev/dri
    group_add:
      - 109  # render group
```

**Intel GPU Example:**
```yaml
version: '3.8'
services:
  previews:
    image: stevezzau/plex_generate_vid_previews:latest
    user: 1000:1000
    environment:
      - PLEX_URL=http://localhost:32400
      - PLEX_TOKEN=your-plex-token
      - PLEX_CONFIG_FOLDER=/config/plex/Library/Application Support/Plex Media Server
    volumes:
      - /path/to/plex/config:/config/plex
      - /path/to/media:/media
    devices:
      - /dev/dri:/dev/dri
    group_add:
      - 109  # render group
```

#### Docker CLI

**NVIDIA GPU:**
```bash
docker run --rm --gpus all \
  -e PLEX_URL=http://localhost:32400 \
  -e PLEX_TOKEN=your-token \
  -e PLEX_CONFIG_FOLDER=/config/plex/Library/Application\ Support/Plex\ Media\ Server \
  -v /path/to/plex/config:/config/plex \
  -v /path/to/media:/media \
  stevezzau/plex_generate_vid_previews:latest
```

**AMD GPU:**
```bash
docker run --rm \
  --device=/dev/dri:/dev/dri \
  --group-add 109 \
  -e PLEX_URL=http://localhost:32400 \
  -e PLEX_TOKEN=your-token \
  -e PLEX_CONFIG_FOLDER=/config/plex/Library/Application\ Support/Plex\ Media\ Server \
  -v /path/to/plex/config:/config/plex \
  -v /path/to/media:/media \
  stevezzau/plex_generate_vid_previews:latest
```

**Intel GPU:**
```bash
docker run --rm \
  --device=/dev/dri:/dev/dri \
  --group-add 109 \
  -e PLEX_URL=http://localhost:32400 \
  -e PLEX_TOKEN=your-token \
  -e PLEX_CONFIG_FOLDER=/config/plex/Library/Application\ Support/Plex\ Media\ Server \
  -v /path/to/plex/config:/config/plex \
  -v /path/to/media:/media \
  stevezzau/plex_generate_vid_previews:latest
```

### Local Installation

#### Prerequisites

Install the required system dependencies:

**Ubuntu/Debian:**
```bash
sudo apt update
sudo apt install ffmpeg mediainfo python3-pip
```

**CentOS/RHEL/Fedora:**
```bash
sudo dnf install ffmpeg mediainfo python3-pip
# or for older versions:
# sudo yum install ffmpeg mediainfo python3-pip
```

**Arch Linux:**
```bash
sudo pacman -S ffmpeg mediainfo python-pip
```

**macOS (with Homebrew):**
```bash
brew install ffmpeg mediainfo
```

#### Install the Package

**Option A: Install from GitHub (Recommended)**
```bash
pip3 install git+https://github.com/stevezau/plex_generate_vid_previews.git
```

**Option B: Install from local source**
```bash
git clone https://github.com/stevezau/plex_generate_vid_previews.git
cd plex_generate_vid_previews
pip3 install .
```

**Option C: Install with optional GPU dependencies**
```bash
# For NVIDIA GPU support
pip3 install git+https://github.com/stevezau/plex_generate_vid_previews.git[nvidia]

# For AMD GPU support  
pip3 install git+https://github.com/stevezau/plex_generate_vid_previews.git[amd]

# For both NVIDIA and AMD
pip3 install git+https://github.com/stevezau/plex_generate_vid_previews.git[nvidia,amd]
```

#### Configuration

Create a `.env` file in your working directory:

```bash
# Plex server configuration
PLEX_URL=http://localhost:32400
PLEX_TOKEN=your-plex-token
PLEX_CONFIG_FOLDER=/path/to/plex/Library/Application Support/Plex Media Server

# Processing settings
GPU_THREADS=4
CPU_THREADS=4
PLEX_BIF_FRAME_INTERVAL=5
THUMBNAIL_QUALITY=4

# Optional: GPU selection
GPU_SELECTION=all

# Optional: Logging
LOG_LEVEL=INFO
```

#### Usage

**Using the console script:**
```bash
plex-generate-previews
```

**Using Python module:**
```bash
python3 -m plex_generate_previews
```

**With command-line arguments:**
```bash
plex-generate-previews \
  --plex-url http://localhost:32400 \
  --plex-token your-token \
  --plex-config-folder "/path/to/plex/Library/Application Support/Plex Media Server" \
  --gpu-threads 4 \
  --cpu-threads 4
```

### Unraid

This guide is for Unraid users following the [TRaSH Guide](https://trash-guides.info/) folder structure with the linuxserver/plex Docker image.

#### Setup Steps

1. **Configure Plex Container Paths:**
   - Add a second container path: `/server/media/plex/`
   - Map to host path: `/mnt/user/media/plex/`

2. **Update Plex Library Mappings:**
   - Delete existing library path mappings in Plex
   - Add new mappings with format: `//server/media/plex/<media-folder>`
   - Example: `//server/media/plex/tv`

3. **Configure Environment Variables:**
   ```bash
   PLEX_URL=http://localhost:32400
   PLEX_TOKEN=your-plex-token
   PLEX_CONFIG_FOLDER=/config/plex/Library/Application Support/Plex Media Server
   ```

4. **Set Permissions:**
   ```bash
   chmod -R 777 /mnt/cache/appdata/plex/Library/Application\ Support/Plex\ Media\ Server/Media/
   ```

5. **Run the Script:**
   - The script may appear frozen initially but will start processing
   - Check the temporary folder for generated thumbnails

## Troubleshooting

### Common Issues

#### "No GPUs detected"
- **Cause**: GPU drivers not installed or FFmpeg doesn't support hardware acceleration
- **Solution**: 
  - Install proper GPU drivers
  - Update FFmpeg to version 7.0+
  - Use `--list-gpus` to check detection
  - Fall back to CPU-only: `--gpu-threads 0 --cpu-threads 4`

#### "PLEX_CONFIG_FOLDER does not exist"
- **Cause**: Incorrect path to Plex Media Server configuration folder
- **Solution**:
  - Verify the path exists: `ls -la "/path/to/plex/Library/Application Support/Plex Media Server"`
  - Check for proper Plex folder structure (Cache, Media folders)
  - Use absolute paths, not relative paths

#### "Permission denied" errors
- **Cause**: Insufficient permissions to access files or directories
- **Solution**:
  - Check file ownership: `ls -la /path/to/plex/config`
  - Fix permissions: `chmod -R 755 /path/to/plex/config`
  - For Docker: ensure proper user/group mapping

#### "Connection failed" to Plex
- **Cause**: Plex server not accessible or incorrect credentials
- **Solution**:
  - Verify Plex URL is correct and accessible
  - Check Plex token is valid and not expired
  - Test connection: `curl -H "X-Plex-Token: YOUR_TOKEN" http://localhost:32400/status/sessions`

#### Docker GPU not working
- **Cause**: Missing GPU runtime or device access
- **Solution**:
  - **NVIDIA**: Install NVIDIA Container Toolkit
  - **AMD**: Add `--device=/dev/dri:/dev/dri --group-add 109`
  - **Intel**: Add `--device=/dev/dri:/dev/dri --group-add 109`
  - Test with: `docker run --rm --gpus all nvidia/cuda:11.0-base nvidia-smi`

#### "Skipping as file not found"
- **Cause**: Incorrect path mappings or missing media files
- **Solution**:
  - Verify media file paths are correct
  - Check path mappings in Plex settings
  - For Windows mapped drives, use UNC paths: `\\server\share\path`

### Debug Mode

Enable debug logging for detailed troubleshooting:

```bash
plex-generate-previews --log-level DEBUG
```

### Getting Help

1. Check the [GitHub Issues](https://github.com/stevezau/plex_generate_vid_previews/issues)
2. Enable debug logging and check logs
3. Verify your configuration with `--help`
4. Test GPU detection with `--list-gpus`


## FAQ

### General Questions

**Q: What's new in version 2.0.0?**
A: Version 2.0.0 introduces multi-GPU support, improved CLI interface, better error handling, WSL2 support, and a complete rewrite with modern Python practices.

**Q: Can I use this without a GPU?**
A: Yes! Set `--gpu-threads 0` and use `--cpu-threads 4` (or higher) for CPU-only processing.

**Q: How do I know which GPUs are detected?**
A: Run `plex-generate-previews --list-gpus` to see all detected GPUs and their capabilities.

**Q: Can I process specific libraries only?**
A: Yes! Use `--plex-libraries "Movies, TV Shows"` to process only specific Plex libraries.

**Q: What's the difference between thumbnail quality 1-10?**
A: Lower numbers = higher quality but larger file sizes. Quality 2 is highest quality, quality 10 is lowest quality.

### Performance Questions

**Q: How many threads should I use?**
A: Start with 4 GPU threads and 4 CPU threads. Adjust based on your hardware - more threads = faster processing but higher resource usage.

**Q: Which GPU type is fastest?**
A: Generally: NVIDIA (CUDA) > AMD (VAAPI) > Intel (QSV/VAAPI) > CPU-only. Actual performance depends on your specific hardware.

**Q: Can I use multiple GPUs?**
A: Yes! The tool automatically detects and can use multiple GPUs. Use `--gpu-selection "0,1,2"` to select specific GPUs.

### Troubleshooting Questions

**Q: "Skipping as file not found" error?**
A: This usually means incorrect path mappings. Check your Docker volume mounts or Plex library path mappings.

**Q: Docker GPU not working?**
A: Ensure you have the proper GPU runtime installed (NVIDIA Container Toolkit, ROCm, etc.) and correct device access.

**Q: How do I enable debug logging?**
A: Use `--log-level DEBUG` or set `LOG_LEVEL=DEBUG` in your environment.

## Support

- **GitHub Issues**: [Report bugs or request features](https://github.com/stevezau/plex_generate_vid_previews/issues)
- **Documentation**: This README and inline help (`--help`)
- **Community**: Check existing issues for solutions to common problems

## Contributing

We welcome contributions! Please:

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests if applicable
5. Submit a pull request

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Acknowledgments

- [Plex](https://www.plex.tv/) for the amazing media server
- [FFmpeg](https://ffmpeg.org/) for video processing capabilities
- [Rich](https://github.com/Textualize/rich) for beautiful terminal output
- All contributors and users who help improve this project
