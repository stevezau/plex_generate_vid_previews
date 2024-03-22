
# Plex Preview Thumbnail Generator

This script is designed to speed up the process of generating preview thumbnails for your Plex media library. It utilizes multi-threaded processes and leverages both NVIDIA GPUs and CPUs for maximum throughput.

## Features

- Accelerates preview thumbnail generation using NVIDIA GPUs and multi-threaded CPU processing
- Supports remote generation of previews for your Plex server
- Customizable settings for thumbnail quality, frame interval, and more
- Easy setup with Docker and Docker Compose
- Utilizes the NVIDIA Container Toolkit for seamless GPU access inside the container

## Requirements

- NVIDIA GPU with CUDA support
- Docker and Docker Compose
- NVIDIA Container Toolkit
- Plex Media Server

## Setup

### 1. Install Dependencies

Make sure you have the following dependencies installed and available in your system's PATH:

- FFmpeg: [Download FFmpeg](https://www.ffmpeg.org/download.html)
- MediaInfo: [Download MediaInfo](https://mediaarea.net/fr/MediaInfo/Download)

### 2. Install NVIDIA Container Toolkit

To enable GPU access inside the Docker container, you need to install the NVIDIA Container Toolkit on your host system. Follow the installation instructions for your distribution from the official NVIDIA documentation: [NVIDIA Container Toolkit Installation Guide](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)

### 3. Clone the Repository

Clone this repository to your local machine:

```bash
git clone https://github.com/yourusername/plex-preview-thumbnail-generator.git
cd plex-preview-thumbnail-generator
pip3 install -r requirements.txt
```

### 4. Configure Environment Variables

Copy the `.env.example` file to `.env`:

```bash
cp .env.example .env
```

Open the `.env` file in a text editor and set the following environment variables:

```
PLEX_URL=https://your-plex-server-url:32400
PLEX_TOKEN=your-plex-token
PLEX_BIF_FRAME_INTERVAL=5
THUMBNAIL_QUALITY=4
PLEX_LOCAL_MEDIA_PATH=/path/to/plex/media
TMP_FOLDER=/tmp/previews
PLEX_TIMEOUT=60
GPU_THREADS=4
CPU_THREADS=4
```

To obtain your Plex URL and token:

1. Open Plex Web (https://app.plex.tv/) and navigate to any video file.
2. Click the "..." menu and select "Get Info".
3. Click "View XML" at the bottom left.
4. Use the URL (without the path) for the `PLEX_URL` variable. For example, `https://your-plex-server-url:32400` or `http://localhost:32400` if running locally.
5. Find the `Plex-Token` value in the URL and use it for the `PLEX_TOKEN` variable.

If you want to generate previews remotely, set the following variables in the `.env` file:

```
PLEX_LOCAL_VIDEOS_PATH_MAPPING=/videos
PLEX_VIDEOS_PATH_MAPPING=/path/to/plex/videos
```

### 5. Build and Run the Docker Container

Build and run the Docker container using Docker Compose:

```bash
docker-compose up --build
```

The script will start generating preview thumbnails for your Plex media library.

## Usage

By default, the script will process all video files in your Plex library. If you want to limit the processing to specific files, you can pass an optional command-line argument:

```bash
docker-compose run previews python3 plex_generate_previews.py <ONLY_PROCESS_FILES_IN_PATH>
```

Replace `<ONLY_PROCESS_FILES_IN_PATH>` with the path or pattern to filter the files you want to process.

## GPU Acceleration

This script leverages the NVIDIA Container Toolkit to enable GPU access inside the Docker container. By default, it uses up to 4 GPU threads (`GPU_THREADS`) for hardware-accelerated video processing with FFmpeg.

Make sure you have the NVIDIA Container Toolkit installed on your host system and a compatible NVIDIA GPU with CUDA support. The script automatically detects the available GPU and utilizes it for faster thumbnail generation.

## Customization

You can customize various settings by modifying the environment variables in the `.env` file:

- `PLEX_BIF_FRAME_INTERVAL`: Interval between preview images (default: 5)
- `THUMBNAIL_QUALITY`: Preview image quality (2-6, default: 4)
- `PLEX_TIMEOUT`: Timeout for Plex API requests in seconds (default: 60)
- `GPU_THREADS`: Number of GPU threads for preview generation (default: 4)
- `CPU_THREADS`: Number of CPU threads for preview generation (default: 4)

Adjust these settings based on your requirements and available resources.

## Support and Questions

If you have any questions or need support, please:

- Create a GitHub issue in this repository
- Visit the Plex forum thread: [Script to regenerate video previews multi-threaded](https://forums.plex.tv/t/script-to-regenerate-video-previews-multi-threaded/788360)

Feel free to contribute to this project by submitting pull requests or reporting issues.
