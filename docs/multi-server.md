# Multi-Media-Server Support (Plex / Emby / Jellyfin)

> [Back to Docs](README.md)

The tool can drive any number of **Plex**, **Emby**, and **Jellyfin** servers
from a single instance. A new file is processed exactly once (one FFmpeg pass
on the GPU) and the resulting frames are published to **every** configured
server that owns it, in the format that server expects.

This page covers:

- [Why this exists](#why-this-exists)
- [How a webhook fires through the system](#how-a-webhook-fires-through-the-system)
- [Adding a server](#adding-a-server)
- [Per-vendor output formats](#per-vendor-output-formats)
- [Webhook configuration per vendor](#webhook-configuration-per-vendor)
- [Library ownership and retry semantics](#library-ownership-and-retry-semantics)
- [REST API summary](#rest-api-summary)

---

## Why this exists

Two real gaps:

1. **Emby has no GPU-accelerated thumbnail generation.** Their VPT (Video
   Preview Thumbnail) task is software-only ([forum](https://emby.media/community/index.php?/topic/145196-)).
   This tool's multi-GPU pipeline gives Emby users a capability their
   server can't match.
2. **Jellyfin's native trickplay is slow with HW accel.** Reports of
   20-30 minutes for 90 minutes of footage are common
   ([issue #13468](https://github.com/jellyfin/jellyfin/issues/13468)).
   Native trickplay generation runs on the same machine as Jellyfin
   itself, and HW decode silently falls back to software on tricky
   files.

Beyond covering those gaps, processing a file once and fanning the result
out to every server that needs it removes redundant work for users who
run more than one media server (a surprisingly common setup).

---

## How a webhook fires through the system

A single inbound URL, `POST /api/webhooks/incoming`, handles every source.

```
                  ┌────────────────────────────────────────────────────┐
  Plex multipart ─►                                                    │
  Emby JSON     ──►  classify_payload  ──►  match server by Server.uuid│
  Jellyfin JSON ──►   (vendor sniff)         / Server.Id / ServerId    │
  Sonarr/Radarr ──►                                                    │
  {"path": ...} ──►                                                    │
                  └────────────────────────────────────────────────────┘
                                               │
                                               ▼
                       resolve_item_to_remote_path  →  apply path_mappings
                                               │              ▼
                                               ▼      canonical local path
                                  process_canonical_path
                                               │
                                               ▼
                                  registry.find_owning_servers
                                               │
                                               ▼
                                 ONE FFmpeg pass → frame dir
                                               │
                       ┌───────────────────────┼───────────────────────┐
                       ▼                       ▼                       ▼
            Plex bundle BIF         Emby sidecar BIF        Jellyfin trickplay
            +scan trigger           +Library/Media/Updated  +Items/{id}/Refresh
```

Per-publisher exceptions are isolated: if Jellyfin's manifest write fails
the Emby sidecar still lands. The dispatcher returns a per-publisher status
list so you can see exactly what happened.

---

## Adding a server

Three vendors, three slightly different UX paths. All three terminate at
`POST /api/servers` with an auth token already in hand.

### Plex

Use the existing **Setup Wizard** at `/setup`. Plex OAuth via plex.tv
issues a token; nothing changes from the single-Plex flow. The migration
to `media_servers[]` happens automatically when the server first boots
the new code (the legacy `plex_*` keys are auto-translated by schema
migration v7).

### Emby

```
1. POST /api/servers/auth/emby/password
   { "url": "http://emby:8096", "username": "admin", "password": "..." }
   → returns { "access_token": "...", "user_id": "...", "server_id": "..." }

2. POST /api/servers
   { "type": "emby",
     "name": "Office Emby",
     "url": "http://emby:8096",
     "auth": {
       "method": "password",
       "access_token": "...",
       "user_id": "..."
     }
   }
   → returns the persisted entry with auth redacted; id is generated
```

API-key paste is also supported — skip step 1 and put `{"method": "api_key", "api_key": "..."}` straight into the auth block.

### Jellyfin (Quick Connect — recommended)

Quick Connect lets the user authorise this tool from inside their Jellyfin
web UI without ever giving us a password. **Note:** Quick Connect must be
enabled by the Jellyfin admin under *Server → Quick Connect*; it's off by
default.

```
1. POST /api/servers/auth/jellyfin/quick-connect/initiate
   { "url": "http://jellyfin:8096" }
   → returns { "code": "ABC123", "secret": "..." }

2. Display "code" to the user. They open Jellyfin → click their profile →
   Quick Connect → enter the code.

3. Poll until approved:
   POST /api/servers/auth/jellyfin/quick-connect/poll
   { "url": "http://jellyfin:8096", "secret": "..." }
   → { "authenticated": false }   (still pending)
   → { "authenticated": true }    (approved!)

4. Exchange the secret for a token:
   POST /api/servers/auth/jellyfin/quick-connect/exchange
   { "url": "http://jellyfin:8096", "secret": "..." }
   → { "access_token": "...", "user_id": "...", ... }

5. POST /api/servers with auth.method = "quick_connect" and the token.
```

### Jellyfin (username + password fallback)

Same shape as Emby — `POST /api/servers/auth/jellyfin/password`.

---

## Per-vendor output formats

Each server type writes a different on-disk layout. The dispatcher picks
the right adapter from `output.adapter` in the server config, defaulting
sensibly per type.

| Vendor | Adapter | Output path |
|---|---|---|
| Plex | `plex_bundle` | `{plex_config}/Media/localhost/{h0}/{h[1:]}.bundle/Contents/Indexes/index-sd.bif` |
| Emby | `emby_sidecar` | `{video_dir}/{basename}-{width}-{interval}.bif` (next to the media file) |
| Jellyfin | `jellyfin_trickplay` | `{video_dir}/trickplay/{basename}-{width}.json` + `trickplay/{basename}-{width}/{0,1,...}.jpg` (10×10 tile sheets) |

**Why Jellyfin's format is different.** Jellyfin 10.9+ uses a native JPG
tile-grid format, *not* BIF. BIF in Jellyfin only works if the user
installs the third-party Jellyscrub plugin. We produce the native format
so no plugin is required. ([Jellyfin 10.9 release notes](https://liuhouliang.com/en/post/jellyfin_10_9/))

**Required Jellyfin server setting.** *Server → Libraries → "Save trickplay
images to media folders"* must be enabled, otherwise Jellyfin won't pick
up the files we write.

---

## Webhook configuration per vendor

Set the same URL — `https://<this-tool>/api/webhooks/incoming?token=<webhook_secret>`
— in every vendor's webhook UI. The `token` query parameter is required for
Plex (Plex's webhook UI offers no header support); other vendors can use
the `X-Auth-Token` header instead.

The router auto-detects the vendor by payload shape and matches the source
server by the identifier embedded in every vendor's payload (Plex's
`Server.uuid`, Emby's `Server.Id`, Jellyfin's `ServerId`).

| Vendor | Webhook source |
|---|---|
| Plex | Server settings → Webhooks → Add webhook (Plex Pass required for outbound webhooks) |
| Emby | Server settings → Notifications → Webhooks (Emby Premiere required) |
| Jellyfin | Install **jellyfin-plugin-webhook**, configure under Plugins → Webhook |
| Sonarr / Radarr | Settings → Connect → +Webhook |

For Jellyfin, the plugin's stock `ItemAdded` template carries `ItemId` /
`ItemType` / `ServerId` but **not** the file path. The router calls back
to Jellyfin's API once to translate the id to a path. If you want to
skip that callback, configure the plugin's template body to be:

```handlebars
{
  "path": "{{Item.Path}}",
  "trigger": "file_added"
}
```

That bypasses the auto-detection altogether and treats the payload as
a path-first webhook.

### Per-server fallback URL

If two servers share a server identifier (rare; usually only happens
with cloned VMs) auto-detection can't disambiguate. Use the explicit
per-server URL: `POST /api/webhooks/server/<server_id>`.

---

## Library ownership and retry semantics

The dispatcher distinguishes three cases when a webhook fires:

| Case | Response |
|---|---|
| 1. Path is under no enabled library on this server | **Skip permanently** |
| 2. Path is under an enabled library, but the server hasn't scanned the file yet | **Slow-backoff retry** (Phase 4 in progress; surfaces as `skipped_not_indexed` per-publisher status today) |
| 3. Server is unreachable | Tight transport retry, eventual failure |

Ownership is decided from the cached `libraries[]` snapshot in each
server config — no per-file index. Toggle a library off via the
per-library `enabled` flag and the dispatcher will skip files in
that library cleanly with no retry storm.

---

## REST API summary

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/servers` | List all configured servers (auth redacted) |
| POST | `/api/servers` | Add a new server |
| GET | `/api/servers/<id>` | Get one server (auth redacted) |
| PUT/PATCH | `/api/servers/<id>` | Update fields (redacted auth values are kept) |
| DELETE | `/api/servers/<id>` | Remove a server |
| POST | `/api/servers/test-connection` | Test a candidate config without saving |
| POST | `/api/servers/<id>/refresh-libraries` | Re-fetch the server's library list |
| GET | `/api/servers/owners?path=...` | Diagnose which servers own a given path |
| GET | `/api/servers/<id>/output-status?path=...&item_id=...` | Whether publisher output files exist for a path on this server |
| POST | `/api/servers/auth/emby/password` | Username+password → Emby token |
| POST | `/api/servers/auth/jellyfin/password` | Username+password → Jellyfin token |
| POST | `/api/servers/auth/jellyfin/quick-connect/initiate` | Start Quick Connect ceremony |
| POST | `/api/servers/auth/jellyfin/quick-connect/poll` | Poll for approval |
| POST | `/api/servers/auth/jellyfin/quick-connect/exchange` | Exchange approved secret for token |
| POST | `/api/webhooks/incoming` | Universal webhook with vendor auto-detection |
| POST | `/api/webhooks/server/<id>` | Per-server fallback webhook URL |

All endpoints (except `/api/webhooks/*` which use the webhook secret) accept
the same `X-Auth-Token` / `Authorization: Bearer` headers as the rest of
the REST API.
