<!-- This file is the Docker Hub "Full Description" for the DEPRECATED mirror
     image at stevezzau/plex_generate_vid_previews. The canonical README at
     DOCKERHUB_README.md drives the new repo. CI publishes both via the
     update-dockerhub-description job. -->

# ⚠️ This image has been renamed

This Docker Hub repository — `stevezzau/plex_generate_vid_previews` — is now a **mirror** of the canonical image:

> **➡ [`stevezzau/media_preview_generator`](https://hub.docker.com/r/stevezzau/media_preview_generator)**

The project supports **Plex, Emby, and Jellyfin** today, so the old "Plex" name is misleading. We renamed the image (and the upstream repository) to match the broader scope.

## What you need to do

Update your `docker-compose.yml` (or `docker run` script):

```diff
 services:
   media-preview-generator:
-    image: stevezzau/plex_generate_vid_previews:latest
+    image: stevezzau/media_preview_generator:latest
     # ... everything else stays the same
```

Then `docker compose pull && docker compose up -d`. Existing volumes, settings, jobs, schedules, and configuration are unchanged — only the image name moves.

## Timeline

- **Now → 2026-10-29**: Both image names mirror the same builds. Watchtower / `:latest` users on the old name keep getting updates automatically.
- **After 2026-10-29**: Only `stevezzau/media_preview_generator` receives updates. The old name stops being published.

## Why the rename?

The app started life as a Plex-only tool. Phases 0–L of the multi-server refactor (PR #225) added Emby and Jellyfin adapters with full per-server libraries, path mappings, exclude paths, webhooks, and dispatch fan-out. The "Plex Generate Previews" name no longer reflected what the project does.

## Where to read more

- Canonical Docker Hub: <https://hub.docker.com/r/stevezzau/media_preview_generator>
- GitHub: <https://github.com/stevezau/media_preview_generator> (auto-redirects from the old URL)
- Documentation: <https://github.com/stevezau/media_preview_generator/tree/main/docs>

If you have any issues migrating, open an issue on GitHub — the import path inside the container is unchanged, so any third-party scripts hitting `/api/...` keep working without modification.
