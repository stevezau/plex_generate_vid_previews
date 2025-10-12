# [stevezau/plex_generate_vid_previews](https://github.com/stevezau/plex_generate_vid_previews/)

[![Version](https://img.shields.io/badge/version-2.0.0-blue.svg)](https://github.com/stevezau/plex_generate_vid_previews)
[![Python](https://img.shields.io/badge/python-3.7+-green.svg)](https://python.org)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/docker-available-blue.svg)](https://hub.docker.com/repository/docker/stevezzau/plex_generate_vid_previews)
[![codecov](https://codecov.io/gh/stevezau/plex_generate_vid_previews/branch/main/graph/badge.svg)](https://codecov.io/gh/stevezau/plex_generate_vid_previews)

## Table of Contents

- [What This Tool Does](#what-this-tool-does)
- [Quick Start](#quick-start)
- [Features](#features)
- [Requirements](#requirements)
- [Installation Options](#installation-options)
  - [Docker](#docker)
  - [Pip Installation](#pip-installation)
  - [Unraid](#unraid)
- [Configuration](#configuration)
  - [Command-line Arguments](#command-line-arguments)
  - [Environment Variables](#environment-variables)
- [GPU Support](#gpu-support)
- [Usage Examples](#usage-examples)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)

## What This Tool Does

Generates video preview thumbnails for Plex Media Server using GPU acceleration and parallel processing. Plex's built-in preview generation is slow - this tool makes it much faster.

Preview thumbnails are the small images you see when scrubbing through videos in Plex.

## Quick Start

**Before you begin, you'll need:**
1. A Plex Media Server running and accessible
2. Your Plex authentication token ([how to get it](https://support.plex.tv/articles/204059436/))
3. The path to your Plex config folder

**Docker (easiest):**
```bash
# 1. Check your GPUs
docker run --rm stevezzau/plex_generate_vid_previews:latest --list-gpus

# 2. Run with your details
docker run --rm --gpus all \
  -e PLEX_URL=http://localhost:32400 \
  -e PLEX_TOKEN=your-token-here \
  -e PLEX_CONFIG_FOLDER=/config/plex/Library/Application\ Support/Plex\ Media\ Server \
  -v /path/to/your/plex/config:/config/plex \
  -v /path/to/your/media:/media \
  stevezzau/plex_generate_vid_previews:latest
```

**Pip (local install):**
```bash
# 1. Install
pip install git+https://github.com/stevezau/plex_generate_vid_previews.git

# 2. Check your GPUs
plex-generate-previews --list-gpus

# 3. Run with your details
plex-generate-previews \
  --plex-url http://localhost:32400 \
  --plex-token your-token-here \
  --plex-config-folder /path/to/your/plex/Library/Application\ Support/Plex\ Media\ Server
```

## Features

- **Multi-GPU Support**: NVIDIA, AMD, Intel, and WSL2 GPUs
- **Parallel Processing**: Configurable GPU and CPU worker threads
- **Hardware Acceleration**: CUDA, VAAPI, QSV, and D3D11VA
- **Library Filtering**: Process specific Plex libraries
- **Quality Control**: Adjustable thumbnail quality (1-10)
- **Docker Support**: Pre-built images with GPU acceleration
- **Command-line Interface**: CLI arguments and environment variables

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

## Installation Options

Choose the installation method that best fits your setup:

### Docker (Recommended)

> [!IMPORTANT]  
> Note the extra "z" in the Docker Hub URL: [stevezzau/plex_generate_vid_previews](https://hub.docker.com/repository/docker/stevezzau/plex_generate_vid_previews)  
> (stevezau was already taken on Docker Hub)

**Quick Start:**
```bash
# 1. Check available GPUs
docker run --rm stevezzau/plex_generate_vid_previews:latest --list-gpus

# 2. Run with GPU acceleration
docker run --rm --gpus all \
  -e PLEX_URL=http://localhost:32400 \
  -e PLEX_TOKEN=your-token \
  -e PLEX_CONFIG_FOLDER=/config/plex/Library/Application\ Support/Plex\ Media\ Server \
  -v /path/to/plex/config:/config/plex \
  -v /path/to/media:/media \
  stevezzau/plex_generate_vid_previews:latest
```

**GPU Requirements:**
- **NVIDIA**: Install [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)
- **AMD**: Follow [ROCm Docker setup guide](https://rocm.docs.amd.com/en/docs-5.0.2/deploy/docker.html)
- **Intel**: Ensure container has access to `/dev/dri` devices and render group
- **WSL2**: No special configuration needed - automatically detects WSL2 GPUs

### Pip Installation

**Quick Start:**
```bash
# Install from GitHub
pip install git+https://github.com/stevezau/plex_generate_vid_previews.git

# Check available GPUs
plex-generate-previews --list-gpus

# Basic usage
plex-generate-previews \
  --plex-url http://localhost:32400 \
  --plex-token your-token \
  --plex-config-folder /path/to/plex/Library/Application\ Support/Plex\ Media\ Server
```

**Prerequisites:**
Install FFmpeg and MediaInfo:

**Ubuntu/Debian:**
```bash
sudo apt update && sudo apt install ffmpeg mediainfo
```

**macOS (with Homebrew):**
```bash
brew install ffmpeg mediainfo
```

**Windows:**
Download from [FFmpeg](https://ffmpeg.org/download.html) and [MediaInfo](https://mediaarea.net/en/MediaInfo/Download)

**Usage Methods:**
```bash
# Method 1: Console script (recommended)
plex-generate-previews --help

# Method 2: Python module
python -m plex_generate_previews --help
```

### Unraid

This guide is for Unraid users following the [TRaSH Guide](https://trash-guides.info/) folder structure with the linuxserver/plex Docker image.

**Setup Steps:**
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

## Configuration

You can configure using either **command-line arguments** or **environment variables**. CLI arguments take precedence over environment variables.

### Basic Configuration

**Required settings:**
- `PLEX_URL` - Your Plex server URL (e.g., http://localhost:32400)
- `PLEX_TOKEN` - Your Plex authentication token
- `PLEX_CONFIG_FOLDER` - Path to Plex config folder

**Common settings:**
- `GPU_THREADS` - Number of GPU worker threads (default: 4)
- `CPU_THREADS` - Number of CPU worker threads (default: 4)
- `THUMBNAIL_QUALITY` - Preview quality 1-10 (default: 4)
- `PLEX_LIBRARIES` - Specific libraries to process (default: all)

### Command-line Arguments

```bash
# Basic usage
plex-generate-previews --plex-url http://localhost:32400 --plex-token YOUR_TOKEN

# With custom settings
plex-generate-previews \
  --plex-url http://localhost:32400 \
  --plex-token YOUR_TOKEN \
  --plex-config-folder /path/to/plex/config \
  --gpu-threads 8 \
  --cpu-threads 4 \
  --thumbnail-quality 2
```

### Environment Variables

Create a `.env` file for persistent settings:

```bash
PLEX_URL=http://localhost:32400
PLEX_TOKEN=your-token-here
PLEX_CONFIG_FOLDER=/path/to/plex/config
GPU_THREADS=4
CPU_THREADS=4
THUMBNAIL_QUALITY=4
```

### Advanced Configuration

For detailed configuration options, see the complete reference tables below:

#### All Configuration Options

| Variable | CLI Argument | Description | Default |
|----------|--------------|-------------|---------|
| `PLEX_URL` | `--plex-url` | Plex server URL | *Required* |
| `PLEX_TOKEN` | `--plex-token` | Plex authentication token | *Required* |
| `PLEX_CONFIG_FOLDER` | `--plex-config-folder` | Path to Plex config folder | *Required* |
| `PLEX_TIMEOUT` | `--plex-timeout` | Plex API timeout in seconds | 60 |
| `PLEX_LIBRARIES` | `--plex-libraries` | Comma-separated library names | All libraries |
| `GPU_THREADS` | `--gpu-threads` | Number of GPU worker threads (0-32) | 4 |
| `CPU_THREADS` | `--cpu-threads` | Number of CPU worker threads (0-32) | 4 |
| `GPU_SELECTION` | `--gpu-selection` | GPU selection: "all" or "0,1,2" | "all" |
| `THUMBNAIL_QUALITY` | `--thumbnail-quality` | Preview quality 1-10 (2=highest, 10=lowest) | 4 |
| `PLEX_BIF_FRAME_INTERVAL` | `--plex-bif-frame-interval` | Interval between preview images (1-60 seconds) | 5 |
| `REGENERATE_THUMBNAILS` | `--regenerate-thumbnails` | Regenerate existing thumbnails | false |
| `TMP_FOLDER` | `--tmp-folder` | Temporary folder for processing | /tmp/plex_generate_previews |
| `LOG_LEVEL` | `--log-level` | Logging level (DEBUG, INFO, WARNING, ERROR) | INFO |

#### Special Commands

| Command | Description |
|---------|-------------|
| `--list-gpus` | List detected GPUs and exit |
| `--help` | Show help message and exit |

#### Path Mappings (Docker/Remote)

Path mappings are crucial when running in Docker or when Plex and the tool see different file paths. This is one of the most common issues users encounter.

**What are Path Mappings?**
Path mappings tell the tool how to convert Plex's file paths to the actual file paths accessible within the container or on the remote machine.

**The Problem:**
- Plex stores file paths like: `/server/media/movies/avatar.mkv`
- Inside Docker container, files are at: `/media/movies/avatar.mkv`
- The tool needs to know how to convert between these paths

**When You Need Path Mappings:**
- Running in Docker with volume mounts
- Plex running on a different machine than the tool
- Different path structures between Plex and the tool
- Using network shares or mounted drives

**How to Use Path Mappings:**

Using the Avatar example from above:
```bash
# Plex sees: /server/media/movies/avatar.mkv
# Container sees: /media/movies/avatar.mkv
# Solution: Map /server/media to /media
--plex-videos-path-mapping "/server/media" \
--plex-local-videos-path-mapping "/media"
```

**Common Examples:**

**Example 1: Docker with Volume Mounts**
```bash
# Plex sees: /server/media/movies/avatar.mkv
# Container sees: /media/movies/avatar.mkv
docker run --rm --gpus all \
  -e PLEX_URL=http://localhost:32400 \
  -e PLEX_TOKEN=your-token \
  -e PLEX_CONFIG_FOLDER=/config/plex/Library/Application\ Support/Plex\ Media\ Server \
  -v /path/to/plex/config:/config/plex \
  -v /path/to/media:/media \
  stevezzau/plex_generate_vid_previews:latest \
  --plex-videos-path-mapping "/server/media" \
  --plex-local-videos-path-mapping "/media"
```

**Example 2: Different Server Names**
```bash
# Plex sees: /mnt/media/movies/avatar.mkv
# Container sees: /media/movies/avatar.mkv
--plex-videos-path-mapping "/mnt/media" \
--plex-local-videos-path-mapping "/media"
```

**Example 3: Windows Network Shares**
```bash
# Plex sees: \\server\media\movies\avatar.mkv
# Container sees: /media/movies/avatar.mkv
--plex-videos-path-mapping "\\\\server\\media" \
--plex-local-videos-path-mapping "/media"
```

**Example 4: Multiple Path Mappings**
```bash
# If you have multiple different path structures
--plex-videos-path-mapping "/server/media,/mnt/media" \
--plex-local-videos-path-mapping "/media,/media"
```

**How to Find Your Path Mappings:**

1. **Check Plex Library Settings:**
   - Go to Plex Web → Settings → Libraries
   - Click on a library → Edit → Folders
   - Note the path Plex shows (e.g., `/server/media/movies`)

2. **Check Your Docker Volume Mounts:**
   ```bash
   # Your Docker command should have something like:
   -v /host/path/to/media:/container/path/to/media
   ```

3. **Test the Mapping:**
   ```bash
   # Run with debug logging to see path conversions
   plex-generate-previews --log-level DEBUG \
     --plex-videos-path-mapping "/server/media" \
     --plex-local-videos-path-mapping "/media"
   ```

**Troubleshooting Path Mappings:**

**Problem: "Skipping as file not found"**
- **Cause**: Incorrect path mappings
- **Solution**: Check that the mapping correctly converts Plex paths to accessible paths

**Problem: "Permission denied"**
- **Cause**: Container can't access the mapped path
- **Solution**: Check Docker volume mount permissions and user mapping

**Problem: "No videos found"**
- **Cause**: Path mapping doesn't match any Plex library paths
- **Solution**: Verify Plex library paths match your mapping

**Quick Test:**
```bash
# Test if your paths are correct
docker run --rm -v /your/media:/media stevezzau/plex_generate_vid_previews:latest \
  --list-gpus --plex-videos-path-mapping "/server/media" \
  --plex-local-videos-path-mapping "/media"
```

**Pro Tips:**
- Always use absolute paths
- Test with `--log-level DEBUG` to see path conversions
- Check Plex library settings to see exact paths
- Use forward slashes even on Windows
- Escape backslashes in Windows paths: `\\\\server\\share`

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

## Usage Examples

### Docker Compose Examples

**NVIDIA GPU:**
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
    deploy:
      resources:
        reservations:
          devices:
            - capabilities: [gpu]
    runtime: nvidia
```

**AMD GPU:**
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

### Docker CLI Examples

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

### Advanced Usage

**Process specific libraries:**
```bash
plex-generate-previews \
  --plex-url http://localhost:32400 \
  --plex-token your-token \
  --plex-config-folder /path/to/plex/config \
  --plex-libraries "Movies, TV Shows"
```

**Use specific GPUs:**
```bash
plex-generate-previews \
  --plex-url http://localhost:32400 \
  --plex-token your-token \
  --plex-config-folder /path/to/plex/config \
  --gpu-selection "0,2" \
  --gpu-threads 8
```

**CPU-only processing:**
```bash
plex-generate-previews \
  --plex-url http://localhost:32400 \
  --plex-token your-token \
  --plex-config-folder /path/to/plex/config \
  --gpu-threads 0 \
  --cpu-threads 8
```

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
