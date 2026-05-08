# Previews Readiness

> [Back to Guides](../guides.md) · [Configuration & API Reference](../reference.md) · [Multi-Media-Server Guide](../multi-server.md)

The **Previews readiness** card on the Edit Server modal is the single
place to verify — and adjust — every server-side setting that affects
whether this app's previews show up in Plex / Emby / Jellyfin.

Each row lives in one of three sections:

1. **Server status** — connection, version, plugin presence.
2. **Library settings** — per-library (or server-wide for Plex) flags.
3. **Advanced** — server trickplay geometry, vendor extraction, path
   mappings, Plex config folder writability.

Every row carries an ⓘ tooltip (the one-liner), a direct link to
**this page** anchored at the relevant check, and — where applicable —
an **Enable** or **Disable** toggle that applies immediately.

> [!WARNING]
> A handful of toggles are **data-destructive**. The UI surfaces a
> typed-confirmation dialog for those cases. The danger is real: flipping
> `EnableTrickplayImageExtraction` off makes Jellyfin delete the
> `.trickplay/` directory this app published on its next library
> refresh. Read the section below before clicking.

---

## Connection  <a id="connection"></a>

**What it checks:** the configured URL + credentials reach the media
server and return an identity response (Plex `machineIdentifier`,
Emby/Jellyfin `/System/Info`).

**Why it matters:** every other check depends on this working. A red
row here almost always means the URL is wrong, the credential
expired, or the container can't see the server.

**Enable / disable:** read-only check — fix the URL or credential in
Server settings.

**Verify:** re-open the Edit Server modal and click the refresh icon
next to the badge.

---

## Server version  <a id="version"></a>

**What it checks:** Jellyfin must be 10.10 or newer; Plex and Emby
are informational (any recent release works).

**Why it matters:** pre-10.10 Jellyfin ignores the
`SaveTrickplayWithMedia` flag and looks for trickplay under
`<config>/data/trickplay/`, which this app never writes to. Upgrade is
the only fix.

**Enable / disable:** read-only — upgrade via your container / package
manager.

---

## Media Preview Bridge plugin  <a id="plugin"></a>
*Jellyfin only*

**What it checks:** the Media Preview Bridge plugin responds on
`GET /MediaPreviewBridge/Ping`.

**Why it matters:** with the plugin, new previews become visible to
Jellyfin **instantly** (Mode A). Without it, previews are adopted on
the next library scan or Jellyfin's daily 3 AM task (Mode B) — still
works, just slower to appear.

**Enable:** one-click **Install plugin**. The app adds its manifest
URL to Jellyfin's plugin repos, queues the install, and restarts
Jellyfin. Takes ~30 s; the readiness card polls until the plugin is
live.

**Disable:** **Uninstall plugin** (with confirm). Removes the package
and restarts Jellyfin. Published tiles stay on disk and are
re-discovered by the next library scan — no data loss.

---

## Library settings  <a id="library-settings"></a>

Per-library (or server-wide for Plex) flags governing preview
generation, scan behaviour, and trickplay adoption.

### Trickplay enabled (EnableTrickplayImageExtraction)  <a id="enable-trickplay"></a>
*Jellyfin only*

**What it checks:** Jellyfin's master trickplay gate on each library.

**Why it matters:** off makes Jellyfin **delete** the `.trickplay/`
directory this app published on the next library refresh
(`TrickplayManager.RefreshTrickplayDataInternal` prunes what it
considers orphaned data). This is the most destructive flag in the
system.

**Enable:** one-click **Enable**. Flips the flag to `true` for this
library.

**Disable:** **requires typing `disable trickplay`** to confirm. Do
not click through the dialog — Jellyfin will delete every preview
tile this app has generated for the library. You'll need to re-run
the generator to restore them.

---

### Save trickplay with media (SaveTrickplayWithMedia)  <a id="save-trickplay-with-media"></a>
*Jellyfin only*

**What it checks:** Jellyfin looks for trickplay in
`<media>.trickplay/` (where this app writes) rather than
`<config>/data/trickplay/` (which this app never writes to).

**Why it matters:** off means published tiles sit on disk but are
invisible to Jellyfin. Files aren't deleted — just unreachable until
the flag flips back on.

**Enable / disable:** toggle via the inline **Enable** / **Disable**
buttons. Disable shows a click-to-confirm dialog (non-destructive but
breaks visibility).

---

### Scan-time extraction (ExtractTrickplayImagesDuringLibraryScan)  <a id="scan-extraction"></a>
*Jellyfin + Emby*

**What it checks:** vendor's scan-time trickplay generation flag.

**Why it matters:**
- **With the Media Preview Bridge plugin installed (Mode A):**
  recommend **off**. The plugin registers previews directly; scan-time
  extraction is wasted CPU.
- **Plugin absent (Mode B):** recommend **on**. Jellyfin's
  `TrickplayProvider` only adopts existing tiles on scan when this
  flag is on. Off without the plugin means adoption stalls until the
  3 AM daily task.

**Enable / disable:** toggle directly. Disable while in Mode B shows a
click-to-confirm dialog.

---

### Chapter-image extraction (ExtractChapterImagesDuringLibraryScan)  <a id="chapter-extraction"></a>
*Emby only*

**What it checks:** Emby's older preview pipeline that predates
trickplay.

**Why it matters:** when this app owns trickplay, chapter-image
extraction is wasted CPU. Disabling it doesn't affect anything this
app publishes.

**Enable / disable:** toggle directly. Non-destructive either way.

---

### Real-time monitor (EnableRealtimeMonitor)  <a id="realtime-monitor"></a>
*Emby + Jellyfin*

**What it checks:** vendor's filesystem watcher that auto-detects new
files without waiting for a manual scan.

**Why it matters:** off means Sonarr/Radarr imports only get noticed
on the next manual scan or webhook nudge — the "not in library yet"
state hangs around longer than necessary.

**Enable / disable:** toggle directly. Non-destructive either way.

---

### FSEvent library updates (FSEventLibraryUpdatesEnabled)  <a id="fsevent-updates"></a>
*Plex only — server-wide*

**What it checks:** Plex's filesystem event subscription in `Settings
→ Library`.

**Why it matters:** off = Plex never reacts to filesystem changes.
Your only signals for new files become this app's scan-nudges and
Plex's periodic timer. Most "why didn't Plex pick up the file?"
complaints trace back here.

**Enable / disable:** toggle directly. Server-wide setting (not
per-library).

---

### FSEvent partial scan (FSEventLibraryPartialScanEnabled)  <a id="fsevent-partial"></a>
*Plex only — server-wide*

**What it checks:** when on, Plex only re-scans the directory that
changed; off = full library scan per added file.

**Why it matters:** off can turn a single-episode import into a
multi-minute full scan.

**Enable / disable:** toggle directly.

---

### Scheduled library updates (ScheduledLibraryUpdatesEnabled)  <a id="scheduled-scan"></a>
*Plex only — server-wide*

**What it checks:** Plex's periodic-scan safety net.

**Why it matters:** belt-and-braces in case the real-time watcher
misses an event (network mounts, container restarts). Default 12 h
interval is fine.

**Enable / disable:** toggle directly.

---

## Server trickplay options  <a id="trickplay-options"></a>
*Jellyfin only*

**What it checks:** server-wide `TrickplayOptions` (tile width, tile
height, interval, resolution widths) match this app's adapter
geometry.

**Why it matters:** Jellyfin synthesises the client-facing
`TrickplayInfo` row from server-wide `TrickplayOptions` **verbatim**
— not measured from the tiles themselves. A mismatch (e.g. server
`TileWidth=8` vs adapter `10`) makes the scrubber pull the wrong
pixel range per tile. Previews appear to load but render wrong.

**Enable:** **Sync options** — fetches the server config,
rewrites only `TileWidth`, `TileHeight`, `Interval`, and ensures the
adapter's width is listed first in `WidthResolutions`, then POSTs
back.

**Disable:** no disable — syncing is idempotent.

---

## Vendor-side preview generation  <a id="vendor-extraction"></a>

**What it checks:** whether the vendor is generating its own previews
on top of this app's output.

**Why it matters:** with this app owning previews, vendor-side
generation is wasted CPU. Plex: `enableBIFGeneration` per library
section. Emby: `ExtractTrickplayImagesDuringLibraryScan` +
`ExtractChapterImagesDuringLibraryScan` per library. Jellyfin:
same pair but `EnableTrickplayImageExtraction` stays on (destructive
when off — see above).

**Enable / disable:** toggles with the current aggregate state
reported (e.g. "stopped on 3/5 libraries"). Non-destructive.

---

## Plex config folder  <a id="plex-config-folder"></a>
*Plex only*

**What it checks:** the configured Plex data folder exists on this
container and is writable. The app probes `os.access(folder, W_OK)`
only — **no test write**, no tempfile, no chmod.

**Why it matters:** BIF bundles land under `Media/localhost/<hash>/`
inside this folder. A :ro Docker mount, a wrong path, or a PUID/PGID
mismatch silently blocks every publish.

**Enable / disable:** read-only status row. Fix the mount or the
path in Server settings.

---

## Path mappings  <a id="path-mappings"></a>

**What it checks:** every configured `local_prefix` exists on this
container.

**Why it matters:** if a `local_prefix` is missing, the mapping
effectively no-ops — scan-nudges go out with unmapped paths and
publishing fails silently.

**Enable / disable:** read-only status row. Fix under Server settings
→ Path mappings.

---

## Troubleshooting

- **Card says "ready (next scan)" and I want "ready (instant)":**
  install the Media Preview Bridge plugin (Jellyfin only).
- **Card flickers between states after a fix:** normal during
  Jellyfin restart (up to ~30 s). The card polls until the server
  stabilises.
- **"action needed" badge persists after fixing a flag:** click the
  refresh icon next to the badge — the probe caches for the duration
  of the modal open.
- **A disable toggle is greyed out:** that flag is already disabled.
  The UI only shows the toggle that would actually change state.

## Related

- [Multi-Media-Server Guide](../multi-server.md) — webhook routing,
  per-server path mappings
- [FAQ](../faq.md)
- [Configuration & API Reference](../reference.md) — API endpoint
  schemas including `/previews-readiness`, `/health-check/apply`,
  `/install-plugin`, `/uninstall-plugin`
