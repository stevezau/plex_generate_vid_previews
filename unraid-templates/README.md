# Unraid Templates

This folder contains the [Community Applications](https://forums.unraid.net/topic/38582-plug-in-community-applications/) template for Unraid.

## Installation

### Option 1: Community Applications (Recommended)

Search for **plex-generate-previews** in the Community Applications plugin.

### Option 2: Manual Template URL

1. Go to **Docker** → **Add Container** → **Template** dropdown
2. Click **Add Template Repository**
3. Enter: `https://github.com/stevezau/plex_generate_vid_previews`
4. Save, then select **plex-generate-previews** from templates

## Files

- `plex-generate-previews.xml` - Unraid Docker template

## Documentation

See [Unraid Guide](../docs/getting-started.md#unraid) for complete setup instructions including:
- GPU passthrough (Intel QuickSync, NVIDIA)
- Network configuration (bridge vs custom)
- Path mapping for Plex
- Troubleshooting
