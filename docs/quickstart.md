# Quick Start

> ⏱️ **Time**: 5 minutes | [Back to Docs](README.md)

Get Plex preview thumbnails generating in minutes with the new web-based setup wizard.

---

## Prerequisites

Before you begin, you'll need:

1. ✅ A Plex Media Server running and accessible
2. ✅ A Plex account (for OAuth sign-in)
3. ✅ Docker installed on your server

---

## Option 1: Docker (Recommended)

### Step 1: Run the Container

```bash
docker run -d \
  --name plex-generate-previews \
  -p 8080:8080 \
  --device /dev/dri:/dev/dri \
  -e PUID=1000 \
  -e PGID=1000 \
  -v /path/to/media:/media:ro \
  -v /path/to/plex/config:/plex:rw \
  -v /path/to/app/config:/config:rw \
  stevezzau/plex_generate_vid_previews:latest
```

> 💡 **No environment variables needed!** The setup wizard will guide you through configuration.

### Step 2: Get Your Access Token

```bash
docker logs plex-generate-previews | grep "Token:"
```

### Step 3: Complete the Setup Wizard

1. Open `http://YOUR_SERVER_IP:8080`
2. Enter the authentication token from the logs
3. The **Setup Wizard** will guide you through:
   - **Sign in with Plex** - Connect your Plex account via OAuth
   - **Select Server** - Choose which Plex server to use
   - **Configure Paths** - Set up media and config paths
   - **Processing Options** - GPU threads, thumbnail settings

That's it! Your previews will start generating.

---

## Option 2: Pip Install

### Step 1: Install

```bash
pip install git+https://github.com/stevezau/plex_generate_vid_previews.git
```

### Step 2: Check GPUs

```bash
plex-generate-previews --list-gpus
```

### Step 3: Run CLI Mode

```bash
plex-generate-previews \
  --plex-url http://localhost:32400 \
  --plex-token your-plex-token \
  --plex-config-folder "/path/to/Plex Media Server"
```

> **Note**: CLI mode requires `--plex-url` and `--plex-token` flags. For the guided setup experience, use Docker.

---

## What's Next?

| Goal                        | Guide                                   |
| --------------------------- | --------------------------------------- |
| Configure all options       | [Configuration](configuration.md)       |
| Enable GPU acceleration     | [GPU Support](gpu-support.md)           |
| Set up scheduling           | [Web Interface](web-interface.md)       |
| Running on Unraid           | [Unraid Guide](unraid.md)               |
| Having issues               | [Troubleshooting](troubleshooting.md)   |

---

[Back to Docs](README.md) | [Main README](../README.md)
