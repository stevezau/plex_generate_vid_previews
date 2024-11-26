# [stevezau/plex_generate_vid_previews](https://github.com/stevezau/plex_generate_vid_previews/)

## Table of Contents

- [Known issues](#known-issues)
- [Features](#features)
- [Requirements](#requirements)
- [Environment variables](#environment-variables)
- [Guide for Docker](#guide-for-docker)
- [Guide for running locally](#guide-for-running-locally)
- [Guide for Unraid](#guide-for-unraid)
- [FAQ, Support and Questions](#faq-support-and-questions)


## Plex Preview Thumbnail Generator Overview

This script is designed to speed up the process of generating preview thumbnails for your Plex media library. It
utilizes multi-threaded processes and leverages NVIDIA/AMD GPUs and CPUs for maximum throughput.

It supports
- Accelerating preview thumbnail generation using GPU and multi-threaded CPU processing
- Remote generation of previews for your Plex server
- Customizable settings for thumbnail quality, frame interval, and more
- Easy setup with Docker and Docker Compose

## Known issues
- AMD GPU Support was recently added, this is untested as i don't have an AMD GPU. Please log an issue if you find problems

## Requirements

- NVIDIA GPU + NVIDIA Container Toolkit (if using Docker)
- AMC GPU + [amdgpu](https://github.com/ROCm/ROCm-docker/blob/master/quick-start.md) (if using Docker)
- Plex Media Server

## Environment variables

You can customize various settings by modifying the environment variables. If you are running locally you can create
a `.env` file

|            Variables             | Function                                                                                                                                    |
|:--------------------------------:|---------------------------------------------------------------------------------------------------------------------------------------------|
|            `PLEX_URL`            | Plex server URL. (eg: http://localhost:32400)                                                                                               |
|           `PLEX_TOKEN`           | Plex Token. ([click here for how to get a token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)) |
|    `PLEX_BIF_FRAME_INTERVAL`     | Interval between preview images (default: 5, plex default: 2)                                                                                      |
|     `PLEX_LOCAL_MEDIA_PATH`      | Path to Plex Media folder (eg: /path_to/plex/Library/Application Support/Plex Media Server/Media)                                           |
| `THUMBNAIL_QUALITY`              | Preview image quality (2-6, default: 4, plex default: 3). 2 being the highest quality and largest file size and 6 being the lowest quality and smallest file size.   |
|           `TMP_FOLDER`           | Temp folder for image generation. (default: /dev/shm/plex_generate_previews)                                                                |
|          `PLEX_TIMEOUT`          | Timeout for Plex API requests in seconds (default: 60). If you have a large library, you might need to increase the timeout.                |
|          `GPU_THREADS`           | Number of GPU threads for preview generation (default: 4)                                                                                   |
|          `CPU_THREADS`           | Number of CPU threads for preview generation (default: 4)                                                                                   |
| `PLEX_LOCAL_VIDEOS_PATH_MAPPING` | Leave blank unless you need to map your local media files to a remote path (eg: '/path/this/script/sees/to/video/library')                  |
|    `PLEX_VIDEOS_PATH_MAPPING`    | Leave blank unless you need to map your local media files to a remote path (eg: '/path/plex/sees/to/video/library')                         |
|           `LOG_LEVEL`            | Set to debug for troubleshooting                                                                                                            |

# Guide for Docker

> [!IMPORTANT]  
> Note the extra "z" in the Docker Hub url [stevezzau/plex_generate_vid_previews](https://hub.docker.com/repository/docker/stevezzau/plex_generate_vid_previews). 
> stevezau was already taken on dockerhub.  

## GPU Support
### NVIDIA - Install NVIDIA Container Toolkit
To enable GPU access inside the Docker container, you need to install the NVIDIA Container Toolkit on your host system.
Follow the installation instructions for your distribution from the official NVIDIA
documentation: [NVIDIA Container Toolkit Installation Guide](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)

### AMD - Install AMDGPU support
In order to access GPUs in a container explicit access to the GPUs must be granted.

Please follow the steps outlined here [https://rocm.docs.amd.com/en/docs-5.0.2/deploy/docker.html](https://rocm.docs.amd.com/en/docs-5.0.2/deploy/docker.html)

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

> [!IMPORTANT]  
> Note: If you are using AMD GPU, you'll need to modify the docker run command and remove NVIDIA add in AMD as per the instructions [here](https://rocm.docs.amd.com/en/docs-5.0.2/deploy/docker.html) 

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
  -e PLEX_LOCAL_MEDIA_PATH='/config/plex/Library/Application Support/Plex Media Server/Media' \
  -e GPU_THREADS=5 \
  -e CPU_THREADS=5 \
  -v /your/media/files:/your/media/files \
  -v /plex/folder:/plex/folder \
  stevezzau/plex_generate_vid_previews:latest
```

# Guide for running locally

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

# Guide for Unraid

**Note:** In this example, the server is named `server` for the network share on Windows, and the SMB share has user-accessible permissions to your media folder. This guide follows the [TRaSH Guide](https://trash-guides.info/) for folder naming structures & was done using the linuxserver plex docker image.

## Steps

1. **Add a Second Container Path:**
   - In your Plex Docker container settings, add a second container path for `/server/media/plex/`.
   - Map the host path as you normally would (e.g., `/mnt/user/media/plex/`).

2. **Update Plex Library Path Mappings:**
   - Open Plex and delete all of your current library path mappings.
   - Replace them with paths following this format (you must add a second `/` yourself, as Plex will only show the mount with one):  
     `//server/media/plex/<name-of-media-folder>`.  
     - Example: `//server/media/plex/tv`

3. **Modify the Script's Environment File:**
   - The script’s `.env` file only needs one specific adjustment for Unraid:
     - Set `PLEX_LOCAL_MEDIA_PATH` as follows:  
       ```plaintext
       PLEX_LOCAL_MEDIA_PATH=\\SERVER\appdata\plex\Library\Application Support\Plex Media Server\Media
       ```

4. **Grant Script Permissions to the Media Folder:**
   - In order for the script to write to the Media folder in the Plex appdata directory, you may need to adjust the permissions.
   - I used the following command in the Unraid console:  
     ```bash
     chmod -R 777 /mnt/cache/appdata/plex/Library/Application\ Support/Plex\ Media\ Server/Media/
     ```

5. **Run the Script:**
   - After running the script, your GPU should begin working.
   - **Note:**  The script may appear to be frozen on 0 files, but you can still see thumbnails being created in the temporary folder you specified, and it should eventually start to update in your terminal.


# FAQ, Support and Questions

If you have any questions or need support, please create a GitHub issue in this repository

Feel free to contribute to this project by submitting pull requests or reporting issues.

## Skipping as file not found?
If you are getting this error it could be:
1. If you're using docker, you haven't mapped the folder into docker correctly. Please check before opening an issue.
2. If you are running on Windows and using a mapped drive, this can cause issues with python, please use UNC \\<server>\\your\media\path instead (see [#52](https://github.com/stevezau/plex_generate_vid_previews/issues/52))
