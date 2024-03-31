# [stevezau/plex_generate_vid_previews](https://github.com/stevezau/plex_generate_vid_previews/)

## Plex Preview Thumbnail Generator

This script is designed to speed up the process of generating preview thumbnails for your Plex media library. It
utilizes multi-threaded processes and leverages both NVIDIA GPUs and CPUs for maximum throughput.

## Known issues
- There is a known issue where AV1 codes are very slow via the docker container. If there are any ffmpeg experts out there, please help [here](https://github.com/stevezau/plex_generate_vid_previews/issues/33)

## Features

- Accelerates preview thumbnail generation using NVIDIA GPUs and multi-threaded CPU processing
- Supports remote generation of previews for your Plex server
- Customizable settings for thumbnail quality, frame interval, and more
- Easy setup with Docker and Docker Compose
- Utilizes the NVIDIA Container Toolkit for seamless GPU access inside the container

## Requirements

- NVIDIA GPU with CUDA support
- NVIDIA Container Toolkit (if using Docker)
- Plex Media Server

## Environment variables

You can customize various settings by modifying the environment variables. If you are running locally you can create
a `.env` file

|            Variables             | Function                                                                                                                                    |
|:--------------------------------:|---------------------------------------------------------------------------------------------------------------------------------------------|
|            `PLEX_URL`            | Plex server URL. (eg: http://localhost:32400)                                                                                               |
|           `PLEX_TOKEN`           | Plex Token. ([click here for how to get a token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)) |
|    `PLEX_BIF_FRAME_INTERVAL`     | Interval between preview images (default: 5)                                                                                                |
|     `PLEX_LOCAL_MEDIA_PATH`      | Path to Plex Media folder (eg: /path_to/plex/Library/Application Support/Plex Media Server/Media)                                           |
|       `THUMBNAIL_QUALITY`        | Preview image quality (2-6, default: 4). 2 being highest quality and largest file size and 6 being lowest quality and smallest file size.   |
|           `TMP_FOLDER`           | Temp folder for image generation. (default: /dev/shm/plex_generate_previews)                                                                |
|          `PLEX_TIMEOUT`          | Timeout for Plex API requests in seconds (default: 60). If you have a large library, you might need to increase the timeout.                |
|          `GPU_THREADS`           | Number of GPU threads for preview generation (default: 4)                                                                                   |
|          `CPU_THREADS`           | Number of CPU threads for preview generation (default: 4)                                                                                   |
| `PLEX_LOCAL_VIDEOS_PATH_MAPPING` | Leave blank unless you need to map your local media files to a remote path (eg: '/path/this/script/sees/to/video/library')                  |
|    `PLEX_VIDEOS_PATH_MAPPING`    | Leave blank unless you need to map your local media files to a remote path (eg: '/path/plex/sees/to/video/library')                         |

# Usage via Docker

> [!IMPORTANT]  
> Not the extra "z" in the Docker Container (stevezzau/plex_generate_vid_previews). stevezau was already taken on dockerhub.  

## Install NVIDIA Container Toolkit

Make sure you have the NVIDIA Container Toolkit installed on your host system and a compatible NVIDIA GPU with CUDA
support. The script automatically detects the available GPU and utilizes it for faster thumbnail generation.

To enable GPU access inside the Docker container, you need to install the NVIDIA Container Toolkit on your host system.
Follow the installation instructions for your distribution from the official NVIDIA
documentation: [NVIDIA Container Toolkit Installation Guide](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)

## docker-compose ([click here for more info](https://docs.linuxserver.io/general/docker-compose))

```yaml
---
version: '3'
services:
  previews:
    image: stevezzau/plex_generate_vid_previews:latest
    environment:
      - PLEX_URL=https://xxxxxx.plex.direct:32400 
      - PLEX_TOKEN=your-plex-token 
      - PLEX_BIF_FRAME_INTERVAL=5
      - THUMBNAIL_QUALITY=4
      - PLEX_LOCAL_MEDIA_PATH=/path/to/plex/media
      - TMP_FOLDER=/tmp/previews
      - PLEX_TIMEOUT=60
      - GPU_THREADS=5  
      - CPU_THREADS=5
    volumes:
      - /path/to/plex/media:/path/to/plex/media
      - /path/to/plex/videos:/videos
    deploy:
      resources:
        reservations:
          devices:
            - capabilities: [gpu]
    runtime: nvidia
```

## docker cli ([click here for more info](https://docs.docker.com/engine/reference/commandline/cli/))

```bash
docker run -it --rm \
  --name=plex_generate_vid_previews \
  --runtime=nvidia \
  -e NVIDIA_VISIBLE_DEVICES=all \
  -e PUID=1000 \
  -e PGID=1000 \
  -e PLEX_URL='http://localhost:32400' \
  -e PLEX_TOKEN='XXXXXX' \
  -e PLEX_BIF_FRAME_INTERVAL=2 \
  -e THUMBNAIL_QUALITY=4 \
  -e PLEX_LOCAL_MEDIA_PATH='/config/plex/Library/Application Support/Plex Media Server/Media/localhost' \
  -e GPU_THREADS=5 \
  -e CPU_THREADS=5 \
  -v /your/media/files:/your/media/files \
  -v /plex/folder:/plex/folder \
  stevezzau/plex_generate_vid_previews:latest
```

# Usage running locally

## 1. Install Dependencies

Make sure you have the following dependencies installed and available in your system's PATH:

- FFmpeg: [Download FFmpeg](https://www.ffmpeg.org/download.html)
- MediaInfo: [Download MediaInfo](https://mediaarea.net/fr/MediaInfo/Download)

## 2. Clone the Repository

Clone this repository to your local machine:

```bash
git clone https://github.com/yourusername/plex-preview-thumbnail-generator.git
cd plex-preview-thumbnail-generator
pip3 install -r requirements.txt
```

## 3. Configure Environment Variables

Copy the `.env.example` file to `.env`:

```bash
cp .env.example .env
```

Open the `.env` file in a text editor and set the environment variables:

## 4. Run

Run the script

```
python3 plex_preview_thumbnail_generator.py
````

# Support and Questions

If you have any questions or need support, please create a GitHub issue in this repository

Feel free to contribute to this project by submitting pull requests or reporting issues.





