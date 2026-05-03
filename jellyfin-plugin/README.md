# Media Preview Bridge — Jellyfin plugin

Bridges externally-published trickplay tiles into Jellyfin's `TrickplayInfos` store so the player can serve scrubbing previews **without spawning ffmpeg**. Built for the [Media Preview Generator](https://github.com/stevezau/plex_generate_vid_previews) tool, but the endpoint is generic — anything that writes Jellyfin-format trickplay sheets to disk can use it.

## What problem does this solve?

Jellyfin's only public path for trickplay registration is `RefreshTrickplayDataAsync`, gated by the per-library `ExtractTrickplayImagesDuringLibraryScan` flag. With that flag off (recommended when an external tool owns generation), externally-published trickplay sits on disk forever invisible to the player. This plugin closes the gap with a single internal call to `ITrickplayManager.SaveTrickplayInfo`.

## Install

In Jellyfin admin → Dashboard → Plugins → Repositories → **+** add:

```
https://stevezau.github.io/media_preview_generator/jellyfin-plugin/manifest.json
```

Then go to Catalogue → install **Media Preview Bridge**. Restart Jellyfin.

## API

| Endpoint | Auth | What it does |
|---|---|---|
| `GET /MediaPreviewBridge/Ping` | anonymous | Returns `{plugin, version, ok:true}`. Use this to detect whether the plugin is installed. |
| `POST /MediaPreviewBridge/Trickplay/{itemId}?width=320&intervalMs=10000` | admin | Reads `<basename>.trickplay/<width> - <tileW>x<tileH>/*.jpg` from disk for the item, computes `ThumbnailCount` + dimensions + bandwidth, persists via `SaveTrickplayInfo`. Returns 204 on success, 404 if item or sheet directory missing. |

The publisher writes tiles to:

```
<media_dir>/<basename>.trickplay/<width> - <tileW>x<tileH>/<n>.jpg
```

(Same layout Jellyfin's `PathManager.GetTrickplayDirectory(item, saveWithMedia=true)` returns.)

## Required Jellyfin library options

The publisher should set these on each library it owns trickplay for:

| Option | Value | Why |
|---|---|---|
| `EnableTrickplayImageExtraction` | `true` | Must be on. Off = Jellyfin **deletes** trickplay directories on the next refresh. |
| `ExtractTrickplayImagesDuringLibraryScan` | `false` | Off = no per-item ffmpeg burn during library scans. |
| `SaveTrickplayWithMedia` | `true` | On = Jellyfin reads from `<media_dir>/<basename>.trickplay/`, where the publisher writes. |

The Media Preview Generator's "Disable vendor extraction" toggle on each Jellyfin server flips all three for you.

## Build locally

```bash
cd jellyfin-plugin
docker run --rm -v "$PWD:/src" -w /src mcr.microsoft.com/dotnet/sdk:9.0 \
    dotnet build -c Release
```

The DLL lands at `bin/Release/net9.0/Jellyfin.Plugin.MediaPreviewBridge.dll`. Drop it into `<jellyfin-config>/plugins/MediaPreviewBridge_<version>/` and restart Jellyfin.

## Compatibility

- Requires Jellyfin **10.11.x** or newer (uses `ITrickplayManager.SaveTrickplayInfo` and the `Jellyfin.Database.Implementations` namespace which both landed in 10.10).
- Targets **net9.0** (matches Jellyfin 10.11's runtime).
- Single small DLL, zero runtime configuration, no UI page.

## Release process

Tag with `plugin-vX.Y.Z.W` (matching the Jellyfin version family). The CI workflow at `.github/workflows/jellyfin-plugin.yml` builds the DLL, attaches the zip to a GitHub release, and updates `manifest.json` on the `gh-pages` branch — Jellyfin's plugin catalogue auto-detects the new version.
