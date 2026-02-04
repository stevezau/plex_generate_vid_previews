# GPU Support

> [Back to Docs](README.md)

Hardware-accelerated video processing for faster thumbnail generation.

---

## Supported GPUs

| GPU Type | Platform | Acceleration | Docker Support |
|----------|----------|--------------|----------------|
| **NVIDIA** | Linux | CUDA/NVENC | ✅ NVIDIA Container Toolkit |
| **AMD** | Linux | VAAPI | ✅ Device passthrough |
| **Intel** | Linux | VAAPI/QuickSync | ✅ Device passthrough |
| **All GPUs** | Windows | D3D11VA | ❌ Native only |
| **Apple Silicon** | macOS | VideoToolbox | ❌ Native only |

---

## GPU Detection

Check which GPUs are detected:

```bash
# Docker
docker run --rm --device /dev/dri:/dev/dri stevezzau/plex_generate_vid_previews:latest --list-gpus

# Local install
plex-generate-previews --list-gpus
```

Example output:
```
✅ Found 2 GPU(s):
  [0] NVIDIA GeForce RTX 4090 (CUDA)
  [1] Intel UHD Graphics 770 (VAAPI - /dev/dri/renderD128)
```

---

## Intel iGPU (QuickSync)

Most common setup, especially on Unraid.

### Docker

```bash
docker run -d \
  --device /dev/dri:/dev/dri \
  -e PUID=1000 \
  -e PGID=1000 \
  stevezzau/plex_generate_vid_previews:latest
```

### Verify Device Exists

```bash
ls -la /dev/dri
# Should show: card0, renderD128
```

### Troubleshooting

If permission denied:
```bash
# Find video group ID
getent group video
# Add to Docker: --group-add <gid>
```

---

## NVIDIA GPU

### Prerequisites

1. Install NVIDIA drivers
2. Install [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)

### Docker

```bash
docker run -d \
  --gpus all \
  -e NVIDIA_VISIBLE_DEVICES=all \
  -e NVIDIA_DRIVER_CAPABILITIES=compute,video,utility \
  stevezzau/plex_generate_vid_previews:latest
```

### Docker Compose

```yaml
services:
  plex-previews:
    image: stevezzau/plex_generate_vid_previews:latest
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```

---

## AMD GPU

### Docker

```bash
docker run -d \
  --device /dev/dri:/dev/dri \
  --group-add video \
  -e PUID=1000 \
  -e PGID=1000 \
  stevezzau/plex_generate_vid_previews:latest
```

### Note

AMD requires proper VAAPI drivers on the host system.

---

## Windows (Native)

Windows uses D3D11VA hardware acceleration automatically with any GPU.

```bash
# Just install and run - GPU is auto-detected
plex-generate-previews --list-gpus
```

**Requirements:**
- Latest GPU drivers (NVIDIA, AMD, or Intel)
- FFmpeg with D3D11VA support

---

## macOS (Native)

Apple Silicon and Intel Macs use VideoToolbox.

```bash
plex-generate-previews --list-gpus
```

---

## Multi-GPU Selection

Use specific GPUs:

```bash
# Use only GPU 0 and 2
--gpu-selection "0,2"

# Use all GPUs (default)
--gpu-selection "all"
```

---

## CPU-Only Mode

Disable GPU acceleration:

```bash
--gpu-threads 0 --cpu-threads 8
```

---

## Performance Tuning

| Threads | Recommendation |
|---------|----------------|
| GPU: 4, CPU: 2 | Balanced (default) |
| GPU: 8, CPU: 4 | High-end systems |
| GPU: 0, CPU: 8 | CPU-only |

---

[Back to Docs](README.md) | [Main README](../README.md)
