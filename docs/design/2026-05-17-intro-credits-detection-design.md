# Intro/Credits Detection — Multi-Server Design Spec

**Date:** 2026-05-17
**Branch:** `feat/markers-detection`
**Status:** Draft for review
**Path:** `docs/design/2026-05-17-intro-credits-detection-design.md`
**Supersedes:** PR #191 (`feat/intro-credits-detection`, Plex-only, stalled 2026-04-23)

---

## 0. TL;DR

We add intro/credits ("marker") detection as a first-class processing stage alongside BIF generation. Detection runs through a **four-tier cascade** (TheIntroDB cloud lookup → chromaprint cross-episode for TV → adaptive binary-search blackdetect for movies → optional PaddleOCR), emits a **canonical `.markers.json` sidecar** next to the media file, and fans out to three **per-server marker publishers**:

- **Plex** — read existing markers via plexapi; trigger native detection (`episode.analyze()`) for Plex Pass users; for non-Pass users, write directly to the `taggings` SQLite table with provenance/restore (off by default, big warnings).
- **Emby** — read via `GET /Items/{Id}?Fields=Chapters`; trigger native scheduled task; write via the existing community `sydlexius/Segment_Reporting` plugin's `POST /emby/segment_reporting/update_segment` endpoint (we require this plugin be installed; surfaced as a Setup Health check).
- **Jellyfin** — read via `GET /MediaSegments/{itemId}`; write by **extending our existing `Jellyfin.Plugin.MediaPreviewBridge` plugin** (already shipped for trickplay registration, distributed via `stevezau.github.io/media_preview_generator/jellyfin-plugin/manifest.json`). Add a new `MarkerBridgeController` (`POST /MediaPreviewBridge/Markers/{itemId}`) plus an `IMediaSegmentProvider` implementation so Jellyfin's scheduled scan also picks up our sidecars. No new plugin install for users — version bump only.

A new **Marker Inspector** UI page (sibling of the BIF Inspector) lets users search, visualize the timeline against an audio waveform, compare our detected markers vs. the server's existing ones, manually nudge boundaries, and one-click apply.

Phased rollout in four PRs (A → D) so we can land scaffolding + TheIntroDB lookups first, ship detection algorithms next, then publishers + Jellyfin plugin, finally the Inspector.

---

## 1. Background &amp; motivation

### Why now

The app already does GPU-accelerated FFmpeg per file. Intro/credits detection is the natural next per-file output to add. Three of our supported servers (Plex, Emby, Jellyfin) all support skip-intro / skip-credits client UIs, but:

- **Plex** users without Plex Pass get no native detection at all.
- **Emby** users without Premiere get no native detection. Even with Premiere, native credits detection is weak/absent (the `CreditsStart` enum exists but the native scheduled task primarily writes intros).
- **Jellyfin** has **no native detection algorithm** — 10.10+ shipped only the *infrastructure* (`MediaSegments` table + `IMediaSegmentProvider` extension point). Detection lives in third-party plugins (Intro Skipper).

A single multi-server detector + publisher lets us cover every user, regardless of subscription tier.

### Why PR #191 didn't work

PR #191 was Plex-only and used `ffmpeg blackdetect` + `silencedetect` on the last 25% of each file. Two real-world failures killed it:

1. **Superman II** — credits marker fired 12 minutes early on a single mid-scene black frame.
2. **NCIS: Sydney** — credits 10 minutes early on a mid-episode act-break black cluster.

A stricter "≥2 black frames in 60s" cluster heuristic addressed the movie case but failed on episodic TV with normal ad-break blacks. The community tester reported "intro detection silently broken" and the PR stalled.

### Salvageable from PR #191

- **`plex_db.py` safety patterns** — `fcntl.flock` Docker-on-Windows probe, `busy_timeout=5000`, short-transaction pattern, `tags` read-only / `taggings` write-only invariant. These are battle-tested and reused verbatim.
- **Two-pass shape** — parallel per-file fingerprint extraction → sequential cross-episode pairwise match. Correct shape for chromaprint TV intros.
- **Detection Debug page concept** — Steve explicitly asked for "a debug tool like the BIF viewer." This becomes the **Marker Inspector**, expanded scope.

Everything else (the `blackdetect+silencedetect` credits algorithm, the pure-Python Hamming with `bin().count("1")`, the monkey-patched `_fingerprint_store` on Config, the bolted-onto-`process_item` pipeline integration, the lack of multi-server abstraction) we redo.

---

## 2. Goals &amp; non-goals

### Goals

1. **Multi-server symmetric** — detection runs once; publishers fan out to Plex, Emby, Jellyfin per the user's enabled servers.
2. **Accuracy first** — better failure modes than PR #191. A missed marker is fine; a wrong marker shipped to clients is bad. Two-tier agreement requirement, confidence-floor gating.
3. **Reuse prior art correctly** — TheIntroDB integration for crowd-sourced markers; reimplement (don't copy) intro-skipper's GPL-3 algorithms in MIT-licensed Python from public spec.
4. **First-class pipeline integration** — own `ProcessingResult` outcomes, own job phase, own settings, own retry semantics. Not a side-effect of `process_item()`.
5. **Inspector UI** — visualize, compare, manually nudge, apply. Mirrors BIF Inspector's role.
6. **Opt-in per server** — default OFF in v1. Users explicitly enable per-server. The dangerous bits (Plex SQLite write) require a second confirmation.
7. **GPU-aware where it helps** — chromaprint and blackdetect can decode on GPU; OCR (if enabled) runs on GPU via PaddleOCR PP-OCRv5.

### Non-goals

1. **Per-user auto-skip preferences** — that's a client-side concern (Plex/Emby/Jellyfin all expose it in their own clients). We write markers, not preferences.
2. **Replacing native detection where it works** — if Plex Pass detection is enabled and the user wants Plex to do it, we trigger and read, we don't compete.
3. **Reverse-engineering Plex's cloud marker API** — undocumented, hash-keyed, Pass-only. Legal and rate-limit risk.
4. **Custom commercial-break / ad-skip** — that's Comskip's territory and a separate problem.
5. **Detection of recap / preview / commercial segments** — out of scope for v1. The MediaSegmentType enum supports them; we leave hooks in the schema but don't detect them.
6. **NFO sidecar marker writes** — neither Plex nor Emby nor Jellyfin honor markers in NFO files. Dead format.

---

## 3. The write-path matrix (the core design constraint)

This is the single most important table in the spec. It dictates everything about the per-server publishers.

| Server | Read existing markers | Write via HTTP API | Write via SQLite | Write via plugin | Has its own detection? |
|---|---|---|---|---|---|
| **Plex** | ✅ plexapi `episode.markers` (read-only in practice) | ❌ `POST /library/metadata/{id}/marker` returns **400** on intro/credits (only `bookmark` works, no client honors it) | ⚠️ `taggings` table — risky: re-analysis wipes; PMS should be stopped or use provenance/restore loop | N/A — no plugin SDK | ✅ **Plex Pass** — intros (audio fingerprint, per-season, May 2020); credits (ML/OCR, PMS 1.31+, Feb 2023) |
| **Emby** | ✅ `GET /Items/{Id}?Fields=Chapters` w/ `MarkerType` | ❌ No POST exists (Luke confirmed 2018, still unchanged) | ⚠️ `Chapters` table — risky: in-process cache, server rewrites on scan | ✅ Companion C# plugin → `IItemRepository.SaveChapters()`. **Reference impl: sydlexius/Segment_Reporting** exposes `POST /emby/segment_reporting/update_segment` | ✅ **Premiere 4.7.3+** — intros (chromaprint, ~80%); credits weak |
| **Jellyfin** | ✅ `GET /MediaSegments/{itemId}` | ❌ Controller is GET-only — **verified in master source** | ⚠️ `MediaSegments` table — risky: next plugin scan wipes (Jellyfin core uses delete-and-replace per `SegmentProviderId`) | ✅ Must ship a C# `IMediaSegmentProvider` plugin | ❌ **No native** — 10.10 shipped infrastructure only; detection is plugin-supplied (intro-skipper) |

### Consequences

1. **For all three servers, the only sustainable write path is the plugin path** (Plex doesn't have plugins, so it gets the SQLite path with provenance — but that's strictly worse).
2. **For Plex Pass / Emby Premiere users with native detection on, we should mostly read + trigger native**, not generate ourselves. Plex's `episode.analyze()` and Emby's "Detect Episode Intros" scheduled task are better than any local heuristic.
3. **Our detection is most valuable for**: (a) non-Plex-Pass / non-Premiere users, (b) all Jellyfin users (no native), (c) credits where native is weak/missing, (d) backfilling old libraries faster than native scheduled tasks can.

---

## 4. Architecture overview

### Pipeline phases

```
                  Existing pipeline                          New for this spec
┌────────────┐  ┌────────────┐  ┌─────────┐  ┌──────────┐  ┌─────────┐  ┌──────────────┐
│  Scan /    │  │ Generate   │  │ Publish │  │ Detect   │  │ Persist │  │  Publish     │
│  Webhook   │→ │ frames     │→ │ BIFs    │→ │ markers  │→ │ sidecar │→ │  markers     │
│  → items   │  │ + BIF      │  │ (BIF    │  │ (4-tier) │  │ (.json) │  │  (per-server)│
└────────────┘  └────────────┘  │ adapter)│  └──────────┘  └─────────┘  └──────────────┘
                                └─────────┘
```

The marker detection phase runs **after** BIF publish (which is unchanged) but **inside the same `process_item()` worker context**, so it shares the working frame cache and FFmpeg subprocess with the existing pipeline.

### New abstractions

```python
# media_preview_generator/markers/types.py

@dataclass(frozen=True)
class MarkerSegment:
    """A single intro/credits marker on a media file."""
    type: Literal["intro", "credits", "recap", "preview", "commercial"]
    start_ms: int                    # always milliseconds; publishers convert to vendor units
    end_ms: int
    confidence: float                # 0.0–1.0
    final: bool = False              # for credits only: this is the LAST credits segment
    source: str = "detected"         # "detected" | "native_plex" | "native_emby" | "theintrodb" | "manual"
    detector: str | None = None      # e.g. "chromaprint_v1", "blackdetect_binsearch_v1"


@dataclass(frozen=True)
class MarkerSet:
    """Canonical sidecar JSON content. Single source of truth for a file's markers."""
    canonical_path: str              # the source media file
    detection_run_id: str            # UUID for the run that produced this set
    detector_version: str            # semver of our detector
    detected_at: str                 # ISO timestamp
    segments: tuple[MarkerSegment, ...]
    season_context: dict | None = None  # for TV: {show_id, season_number, episodes_compared}
    notes: dict[str, Any] = field(default_factory=dict)


# media_preview_generator/markers/detector.py

class Detector(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def supports(self, item: ProcessableItem, season_context: SeasonContext | None) -> bool: ...

    @abstractmethod
    def detect(
        self,
        item: ProcessableItem,
        season_context: SeasonContext | None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> list[MarkerSegment]: ...
```

### MarkerPublisher (parallel to OutputAdapter)

```python
# media_preview_generator/markers/publisher.py

class WriteCapability(str, Enum):
    READ_ONLY = "read_only"            # we can read markers, can't write
    TRIGGER_NATIVE = "trigger_native"  # we can ask the server to detect
    PLUGIN = "plugin"                  # write via a companion plugin (Emby, Jellyfin)
    DIRECT_DB = "direct_db"            # write to SQLite (Plex non-Pass)


class MarkerPublisher(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def supports_write(self) -> WriteCapability: ...

    @abstractmethod
    def read_markers(self, item_id: str) -> list[MarkerSegment]: ...

    @abstractmethod
    def trigger_native_analysis(self, item_id: str) -> bool:
        """Ask the server to run its own detection. Returns True if triggered."""

    @abstractmethod
    def write_markers(
        self,
        item_id: str,
        markers: list[MarkerSegment],
    ) -> WriteResult: ...
```

### The canonical sidecar JSON

**Location:** `<canonical_path>.markers.json` (next to the media file, mirroring Jellyfin's trickplay-sidecar convention).

**Why a sidecar, not just a DB?** Single source of truth across publishers + survives our app rebuilds + visible to operators on disk + can be hand-edited + plays well with the `MediaPreviewBridge` Jellyfin plugin (extended in §7) which reads sidecars at scan time.

**Example:**
```json
{
  "canonical_path": "/data/media/tv/Show/S01E01.mkv",
  "detection_run_id": "5f9e2a01-1234-...",
  "detector_version": "1.0.0",
  "detected_at": "2026-05-17T20:42:18Z",
  "segments": [
    {"type": "intro", "start_ms": 0, "end_ms": 92000, "confidence": 0.91, "source": "detected", "detector": "chromaprint_v1"},
    {"type": "credits", "start_ms": 2580000, "end_ms": 2710000, "confidence": 0.78, "final": true, "source": "detected", "detector": "blackdetect_binsearch_v1"}
  ],
  "season_context": {"show_title": "Show", "season_number": 1, "episodes_compared": ["S01E02", "S01E03"]},
  "notes": {}
}
```

**Sidecar is read by:**
- Marker publishers when fanning out to servers
- `MediaPreviewBridge` Jellyfin plugin (`IMediaSegmentProvider`) at scan time
- Marker Inspector UI for display
- Future re-publish flows ("apply existing detections to a newly-added server")

---

## 5. Detection algorithms (the four-tier cascade)

### Tier 1 — TheIntroDB lookup (live-verified 2026-05-17)

**Endpoint:** `GET https://api.theintrodb.org/v1/media?tmdb_id={id}[&season={s}&episode={e}]` (also accepts `imdb_id=tt0000000`). OpenAPI 2.0.0 spec mirrored to `docs/design/theintrodb-openapi-v2.yaml`.

**Verified response shape** (Breaking Bad S1E1, live):
```json
{
  "tmdb_id": 1396, "type": "tv", "season": 1, "episode": 1,
  "intro":   {"start_ms": 228000, "end_ms": 244000},
  "credits": {"start_ms": 3431000, "end_ms": null},
  "recap":   {"start_ms": null, "end_ms": null, "submission_count": 0},
  "preview": {"start_ms": null, "end_ms": null, "submission_count": 0}
}
```
`credits.end_ms = null` means "goes to EOF" (our publishers translate this to `Duration - 1` when emitting to servers that require both endpoints).

**Real coverage (live `/v1/stats`):**
- 1,744 shows + 272 movies (2,014 media items)
- 45,274 episodes covered
- 323,404 total submissions, 81,435 accepted
- 295 contributors
- Empirical hit rate: ~40% on popular TV S1E1, ~18% on popular movies. Long tail is sparse.

**Rate limits (binding):**
- **Anonymous:** 100 lookups/day per IP, 30/10s burst.
- **Bearer-auth:** 500 lookups/day per user, 1000 submits/day. Burst 30/10s.
- No bulk-export endpoint exists — only `/media` (single lookup) and `/submit`.

**Operational consequence:** A 5,000-episode library at 500/day takes 10 days to fully scan via TheIntroDB. **Tier 1 is therefore designed as opportunistic, not bootstrap:**
- Webhook-driven new items hit Tier 1 first (low volume — fits in budget easily)
- Items rendered in the Inspector trigger an on-demand lookup
- A "fill cache slowly" background task progressively pulls TheIntroDB results for the existing library, respecting rate limits
- Tiers 2/3 carry the bulk of first-scan library bootstrap, not Tier 1

**Auth model:** Bearer JWT — users sign up at theintrodb.org for free API key, paste into our settings. Without a key the app uses anonymous 100/day quota.

**Identifier extraction:** new helper on each `MediaServer` subclass — `external_ids(item_id) -> {"tmdb": ..., "imdb": ...}`. Plex exposes via `Episode.guids` / `Movie.guids` (multi-value, format `tmdb://12345`). Emby/Jellyfin expose via `Items` response with `ProviderIds: {"Tmdb": "12345", "Imdb": "tt..."}`. Cached on `ProcessableItem` so we don't re-fetch.

**Caching:** 24-hour TTL on misses (`{tmdb_id, season, episode} → empty`); permanent on hits (sidecar gets written).

**Submit-back:** Default OFF. When enabled and detector confidence ≥0.85 AND two tiers agreed on boundary within 2 seconds, POST `/v1/submit` with the user's own API key. Their submissions get 10× weight in TheIntroDB's averaging, so a regular user's contributions visibly improve their own coverage.

**Failure mode:** API 5xx / rate-limit 429 → fall through to Tier 2/3. Never block the pipeline on TheIntroDB. Circuit-breaker pattern: after 5 consecutive failures within 60s, skip TheIntroDB for the next 5 minutes.

### Tier 2 — Chromaprint cross-episode (TV only)

**Algorithm (reimplemented from `intro-skipper/IntroSkipper/Analyzers/ChromaprintAnalyzer.cs` spec, NOT copy-pasted from the GPL-3 source):**

1. **Pass 1, per-worker:** Extract a chromaprint fingerprint from a fixed window using `ffmpeg -ss 0 -t 600 -i {file} -ac 1 -ar 22050 -f chromaprint -fp_format raw -`. For intros: window = first 10 min. For credits: window = `Duration - CreditsFingerprintStart` (default last 10 min) → EOF.
2. **Per-season fingerprint store:** Thread-safe dict keyed by `(show_id, season_number)`. Episodes deposit their fingerprint as they finish Pass 1.
3. **Pass 2, post-dispatch (sequential, runs after all workers idle for that season):** Build inverted index of fingerprint hashes per episode. For each pair of episodes in the season, find the largest contiguous matching subsequence (sliding window Hamming distance, threshold ≤8 bits out of 32). Use **numpy** for the Hamming popcount — `np.unpackbits(arr.view(np.uint8)).sum(axis=-1)` is ~100× faster than `bin().count("1")` (PR #191's hot-path mistake).
4. **Pairwise consensus:** Compare episode 0 vs episodes 1–5, then 1 vs 2–5, then 2 vs 3–5. Match candidates with ≥2 pairwise agreements within 4 seconds become the segment. Single-pair matches are discarded.
5. **Constraints:** Intro 15s–2min, in first 25% or 10min (whichever smaller). Credits 30s–4min. Reject matches exceeding `Duration - CreditsFingerprintStart - 1` (anti-duplicate-file safety from intro-skipper's spec).

**No GPU benefit** — chromaprint is integer DCT on a 22kHz mono stream. GPU round-trip costs more than the compute. Per-episode CPU: ~5–10s on a single core.

### Tier 3 — Adaptive binary-search blackdetect (the PR #191 fix)

**Algorithm (reimplemented from `BlackFrameAnalyzer.cs` spec):**

1. **Search window:** `[Duration - MaxCreditsDuration, Duration - MinCreditsDuration]` (e.g. `[Duration - 6min, Duration - 30s]`).
2. **Adaptive binary search backward** from EOF: probe a 2-second window at the midpoint using `ffmpeg -ss {mid} -t 2 -i {file} -vf "blackdetect=d=2:pix_th=0.10:pic_th=0.85" -f null -`. If `black_start/black_end` covers ≥85% of the window, we found sustained black — narrow toward EOF. If not, narrow toward middle. Converge to 4-second precision.
3. **Validation:** Reject if convergence lands outside `[Duration*0.85, Duration*0.99]` (sanity check) or if total black duration is <2s.
4. **Output:** `(black_start, end_of_file)` as the credits segment, `confidence = 0.7` (lower than chromaprint because no cross-episode signal).

**Why this fixes PR #191:**
- **Binary search, not linear scan** — Superman II's stray mid-scene black at minute 90 can't beat the search because the search converges toward the *latest* sustained black region near EOF, not the first one it stumbles on.
- **2-second sustain requirement** — NCIS:Sydney's mid-episode act-break single-frame blacks don't sustain across the 2s window, so they're filtered.
- **Sanity-clamped search window** — we don't even probe the first 85% of the file, so episodic act breaks can't trip the detector.

**GPU offload:** YES — `ffmpeg -hwaccel cuda -i {file}` decodes on GPU before `blackdetect` runs CPU-side. Per-file: ~5–10 binary search probes × ~1s GPU-decode + CPU filter = ~10–20s total (vs. PR #191's ~131s for a 4K HDR film).

### Tier 4 — PaddleOCR PP-OCRv5 (optional, GPU-accelerated)

**When it runs:** Tier 1 miss, Tier 3 produced no high-confidence boundary, AND user has `markers.ocr_fallback = true` enabled in settings.

**Algorithm:**
1. Extract 1-fps frame samples from the last 10 minutes via `ffmpeg -ss {start} -vf fps=1 -frames:v 600 {tmp}/frame_%05d.jpg`.
2. Batch-OCR with PaddleOCR PP-OCRv5 on GPU.
3. Keyword match against multilingual set: `directed by | produced by | starring | a film by | réalisé par | regie | 監督 | 导演 | cast | crew`.
4. First sustained match (≥3 consecutive frames) is the credits start.

**Why PaddleOCR not Tesseract:** PP-OCRv5 is GPU-capable (~12.7 FPS on benchmark, much higher with CUDA), Apache-2.0, handles rotated/curved/multilingual text where Tesseract collapses. Tesseract benchmarks at 75% on clean text dropping to 30–50% on stylized credits; PP-OCRv5 holds 85–90%.

**Cost:** ~5–15s per movie with GPU. Gated behind a setting because most users won't need it.

### Confidence floor &amp; multi-tier agreement

- A segment is written to a server only if `confidence ≥ markers.min_confidence_to_write` (default 0.7).
- For **credits** specifically, require agreement from ≥2 tiers (e.g. Tier 1 + Tier 3 within 5s of each other) OR a single tier with confidence ≥0.9. This is the "wrong markers shipped to clients is worse than missing markers" gate.

---

## 6. Per-server marker publishers

### `PlexMarkerPublisher`

```python
class PlexMarkerPublisher(MarkerPublisher):
    def supports_write(self) -> WriteCapability:
        if self._has_plex_pass() and self._native_detection_enabled():
            return WriteCapability.TRIGGER_NATIVE
        if self._direct_db_enabled():
            return WriteCapability.DIRECT_DB
        return WriteCapability.READ_ONLY
```

**Read:** `episode.markers` via plexapi. Returns `Marker(type, start, end, final, version)`. Fully supported.

**Trigger native:** `PUT /library/metadata/{ratingKey}/analyze` via `episode.analyze()`. Plex's full analysis pipeline runs, including markers if Pass + library setting on. Async; poll `episode.reload(); episode.hasIntroMarker` until populated.

**Direct DB write (when enabled):**
- Connect to `{plex_config}/Plug-in Support/Databases/com.plexapp.plugins.library.db` with `busy_timeout=5000`.
- Use **`fcntl.flock(LOCK_SH | LOCK_NB)`** probe first to detect Docker-on-Windows (CIFS/SMB doesn't honor advisory locking). On failure, surface as Setup Health "Plex DB write unsafe on this filesystem."
- Look up `tag_id` for marker type (12 = intro, 4 = credits) — never insert into `tags`.
- INSERT into `taggings` with `extra_data` JSON carrying `{"pv:version":"5","pv:source":"media_preview_generator","pv:run_id":"<uuid>"}` — our provenance marker.
- Single transaction per write, immediate close.
- **Restore loop** (danrahn pattern): periodic scan of `taggings` looking for our provenance flag; if missing, re-INSERT (Plex's re-analysis wiped it).

**Default behavior:** OFF. User must explicitly enable per-server in settings + click through a confirmation modal that quotes the failure modes.

### `EmbyMarkerPublisher`

```python
class EmbyMarkerPublisher(MarkerPublisher):
    def supports_write(self) -> WriteCapability:
        if self._segment_reporting_plugin_installed():
            return WriteCapability.PLUGIN
        if self._has_premiere():
            return WriteCapability.TRIGGER_NATIVE
        return WriteCapability.READ_ONLY
```

**Read:** `GET /Items/{id}?Fields=Chapters` returns chapters with `MarkerType` field. Ticks are 100ns (1s = 10_000_000).

**Trigger native:** `POST /ScheduledTasks/Running/{IntroSkipDetectionTaskId}` — kick off "Detect Episode Intros." Per-library opt-in via `EnableIntroSkipDetection` flag (new — to be added to `EmbyServer.check_settings_health()` alongside the existing chapter/trickplay flags).

**Write via plugin (Segment_Reporting):**
- `POST /emby/segment_reporting/update_segment?ItemId={id}&MarkerType={IntroStart|IntroEnd|CreditsStart}&Ticks={ticks}` with `X-Emby-Token` header.
- Plugin internally calls `IItemRepository.SaveChapters()` — the only sanctioned write path.
- For credits, we write `CreditsStart` (no `CreditsEnd` exists — Emby clients use it as a tail-end marker).

**Plugin install detection:** Probe `GET /emby/segment_reporting/version` on connection test. If 404, raise `HealthCheckIssue` with severity=recommended ("Install sydlexius/Segment_Reporting plugin to enable marker writes") and a link to install instructions.

**Default behavior:** OFF until the user installs Segment_Reporting and explicitly enables in our settings.

### `JellyfinMarkerPublisher`

```python
class JellyfinMarkerPublisher(MarkerPublisher):
    def supports_write(self) -> WriteCapability:
        if self._media_preview_bridge_supports_markers():
            return WriteCapability.PLUGIN
        return WriteCapability.READ_ONLY
```

**Read:** `GET /MediaSegments/{itemId}?includeSegmentTypes=Intro,Outro,Recap,Preview,Commercial`. Returns `MediaSegmentDto[]` with `StartTicks`/`EndTicks` (100ns).

**Trigger native:** N/A — Jellyfin has no native algorithm. Only intro-skipper plugin (which we don't own) can detect.

**Write via plugin (the existing `MediaPreviewBridge`, extended in §7):**
- Two write paths share the same underlying segment manager:
  1. **Push:** Publisher writes `<canonical_path>.markers.json` next to media, then calls `POST /MediaPreviewBridge/Markers/{itemId}` for immediate replace + UI feedback.
  2. **Pull:** The plugin's `IMediaSegmentProvider` implementation re-reads the same sidecar during Jellyfin's scheduled "Media segment scan" — covers cases where the immediate push 500s or the sidecar gets edited out-of-band.

**Plugin install/version detection:** Probe `GET /MediaPreviewBridge/Ping` (already exists). If the returned `version` is `>= 10.11.1.0` (the marker-capable bump), we can write markers; if older, raise a `HealthCheckIssue` (severity=recommended) "Update `MediaPreviewBridge` to ≥10.11.1.0 to enable marker writes." If the endpoint 404s, the plugin isn't installed at all — same `HealthCheckIssue` with a link to the existing manifest.

**Default behavior:** OFF until plugin installed AND user explicitly enables in settings.

---

## 7. Extending the existing `MediaPreviewBridge` Jellyfin plugin

We **already ship** a Jellyfin plugin at `jellyfin-plugin/` — `Jellyfin.Plugin.MediaPreviewBridge` (`net9.0`, targeting Jellyfin `10.11.0`). It currently exposes:

- `GET /MediaPreviewBridge/Ping` — anonymous, plugin-detection probe
- `GET /MediaPreviewBridge/ResolvePath?path=…` — admin, file path → item id
- `POST /MediaPreviewBridge/Trickplay/{itemId}` — admin, registers externally-written trickplay tiles via `ITrickplayManager.SaveTrickplayInfo`

CI workflow `.github/workflows/jellyfin-plugin.yml` already builds the DLL, releases via GitHub releases, and hosts a Jellyfin-compatible manifest at `https://stevezau.github.io/media_preview_generator/jellyfin-plugin/manifest.json`. Users who want trickplay already have it installed.

### Adding marker capability to the same plugin

We add **one new controller** + an `IMediaSegmentProvider` implementation to the existing project. No new plugin, no new manifest, no new install flow — users who already have `MediaPreviewBridge` for trickplay pick up the marker capability on their next plugin update.

**New REST endpoint** (parallels the trickplay registration shape):

```csharp
// jellyfin-plugin/Api/MarkerBridgeController.cs (new file)
[ApiController]
[Authorize(Policy = "RequiresElevation")]
[Route("MediaPreviewBridge")]
public class MarkerBridgeController : ControllerBase
{
    [HttpPost("Markers/{itemId:guid}")]
    public async Task<IActionResult> RegisterMarkers([FromRoute] Guid itemId, CancellationToken ct)
    {
        // Reads <item.Path>.markers.json, validates schema, hands off to
        // the segment manager's "replace segments by SegmentProviderId" path
        // with our provider id "MediaPreviewBridge".
    }

    [HttpDelete("Markers/{itemId:guid}")]
    public async Task<IActionResult> ClearMarkers([FromRoute] Guid itemId, CancellationToken ct)
    {
        // Lets the publisher invalidate stale markers on re-detection.
    }
}
```

**`IMediaSegmentProvider` implementation** (new file, same project):

```csharp
// jellyfin-plugin/Providers/MarkerSegmentProvider.cs (new file)
public class MarkerSegmentProvider : IMediaSegmentProvider
{
    public string Name => "Media Preview Bridge";
    // Stable id — matches what RegisterMarkers writes through.
    public string Id => "MediaPreviewBridge";

    public ValueTask<bool> Supports(BaseItem item, CancellationToken ct)
        => ValueTask.FromResult(item is Episode || item is Movie);

    public async Task<IReadOnlyList<MediaSegmentDto>> GetMediaSegments(
        MediaSegmentGenerationRequest request, CancellationToken ct)
    {
        // Read <item.Path>.markers.json, validate, return MediaSegmentDto[].
        // Falls back to empty list (not exception) on missing sidecar so
        // Jellyfin's scheduled scan stays clean on items we haven't
        // processed yet.
    }

    public Task CleanupExtractedData(Guid itemId, CancellationToken ct) => Task.CompletedTask;
}
```

### Why two redundant paths (REST `POST` + `IMediaSegmentProvider`)

`IMediaSegmentProvider` only runs during Jellyfin's scheduled "Media segment scan" task — fine for steady-state but laggy for "I just detected, show me the marker in the player now." The `POST /Markers/{itemId}` endpoint forces an immediate replace so the publisher gets confirm-on-write semantics. Both paths share the same underlying `IMediaSegmentManager` call and the same `SegmentProviderId`, so they're consistent.

### Coexistence with intro-skipper

Our `SegmentProviderId = "MediaPreviewBridge"` doesn't collide with intro-skipper's `"intro-skipper"`. Jellyfin's `MediaSegmentManager.RunSegmentPluginProviders()` uses delete-and-replace **scoped to each provider id**, so the two plugins coexist — users can run both and Jellyfin merges segments at query time.

### Version bump

Current plugin version is `10.11.0.2`. Marker addition bumps to `10.11.1.0`. Users with the existing plugin auto-upgrade via the catalog UI.

---

## 8. Unified Inspector UI (Frames + Markers + Audio tabs)

### Route refactor

The existing BIF Inspector at `/bif-viewer` becomes the **Frames** tab of a unified `/inspector`. The page-level layout (server picker, search box, results list) is shared; per-tab content swaps.

Route migration:
- `/bif-viewer` → 301 redirect to `/inspector?tab=frames` (back-compat)
- New `/inspector?tab=markers` — this section
- Future `/inspector?tab=audio` — placeholder (waveform inspector, post-Phase D)

### Page layout (text mockup)

```
┌─ Inspector ─────────────────────────────────────────────────────────┐
│ Server: [Plex (Home) ▼]  Search: [show name s01e02______] [Search] │
├ [Frames]  [Markers]  [Audio]  ─────────────────────────────────────┤
│ Show — S01E02                                                       │
│ /data/media/tv/Show/S01E02.mkv • 47:21 • intro+credits detected     │
│                                                                     │
│  0:00      5:00      10:00     ...      45:00     46:00     47:00  │
│ ┌──────────────────────────────────────────────────────────────┐    │
│ │   ▓▓▓▓░░░░░░░ audio waveform ░░░░░░░░░▓▓▓░░░░░▓▓▓▓░░░░░     │    │
│ │                                                              │    │
│ │ ██████ INTRO (server)         ████████ CREDITS (server)     │    │
│ │ █████  INTRO (detected, 0.91) ████████ CREDITS (det, 0.78)  │    │
│ └──────────────────────────────────────────────────────────────┘    │
│                                                                     │
│ Detected by: chromaprint_v1 (intro) + blackdetect_binsearch_v1     │
│ Season context: 4 episodes compared in S01                        │
│                                                                     │
│ [Apply to server]  [Manually edit]  [Re-detect]  [Submit to TheIntroDB] │
└─────────────────────────────────────────────────────────────────────┘
```

### Components

- **Shared search box** — reuses existing per-vendor search API (`/api/bif/servers/<id>/search`). Search results show which tabs have data (e.g. ⓕ Frames available, ⓜ Markers detected, ⓘ tooltips per spec).
- **Tab state** — preserved in URL query (`?tab=markers&server=plex-1&q=...`) so deep-linking works and back/forward navigation feels native.
- **Audio waveform** — generated once per file, cached as PNG via `ffmpeg -i {file} -filter_complex showwavespic=s=1200x80 -frames:v 1 {png}`. ~1s per file. Cached next to sidecar.
- **Timeline rows:**
  - "INTRO (server)" / "CREDITS (server)" — what the server currently reports.
  - "INTRO (detected)" / "CREDITS (detected)" — from our sidecar.
- **Manual edit modal** — drag handles to nudge start/end, plus a numeric input. Saves to sidecar with `source = "manual"`.
- **Re-detect button** — kicks off a single-file detection job for this item.
- **Apply to server** — calls the marker publisher; shows write capability (✅ via plugin / ⚠️ direct DB / ❌ read-only) before action.

### SocketIO events (incremental)

- `marker_detection_progress` — `{item_id, phase: "fingerprint"|"compare"|"blackdetect", pct}` during in-flight detection.
- `marker_published` — `{item_id, server_id, segments_written, errors}` after successful write.

### Bulk inspector view (Phase D++)

A second view at `/inspector/markers/library` shows a paginated table across the whole library — title, has-server-markers (✅/❌/⚠️ conflict), has-our-markers (✅/❌), confidence histogram. Lets ops users find "shows where our detection disagrees with the server" or "shows missing any markers entirely."

---

## 9. Job pipeline integration

### New `ProcessingResult` outcomes

```python
class ProcessingResult(str, Enum):
    # existing
    GENERATED = "generated"
    SKIPPED_BIF_EXISTS = "skipped_bif_exists"
    SKIPPED_NOT_INDEXED = "skipped_not_indexed"
    FAILED = "failed"
    # new — marker detection
    MARKERS_DETECTED = "markers_detected"          # sidecar written, marker(s) found
    MARKERS_DETECTED_EMPTY = "markers_empty"       # ran successfully, no segments
    MARKERS_DETECTION_FAILED = "markers_detect_failed"
    MARKERS_PUBLISHED = "markers_published"        # written to ≥1 server
    MARKERS_SKIPPED_NATIVE = "markers_skipped_native"  # native detection on, we deferred
    MARKERS_SKIPPED_DISABLED = "markers_skipped_disabled"
    MARKERS_PUBLISH_FAILED = "markers_publish_failed"
```

A single file can produce multiple result rows (BIF + markers each count separately). The `Job.outcome` dict accumulates per-result counts; the per-file `outcome_details` table tracks per-stage outcomes.

### Phases

```python
def process_item(item: ProcessableItem, config: Config, ...) -> list[ProcessingResult]:
    results = []

    # Phase 1: BIF (existing, unchanged)
    bif_result = run_bif_phase(item, config)
    results.append(bif_result)

    # Phase 2: Marker detection (new)
    if config.markers.enabled and (config.markers.detect_intros or config.markers.detect_credits):
        markers, det_result = run_marker_detection_phase(item, config, fingerprint_store)
        results.append(det_result)

        # Phase 3: Marker publish (new)
        if markers and config.markers.write_to_server:
            pub_results = run_marker_publish_phase(item, markers, config, registry)
            results.extend(pub_results)

    return results
```

### Retry semantics

- **Detection failure** — same retry cascade as BIF (GPU fallback to CPU, etc.). Doesn't block BIF.
- **Publish failure** — distinct from detection failure. Sidecar still exists; user can manually `Apply` from Inspector. No auto-retry — publisher errors are typically structural (plugin not installed, DB locked) and a blind retry won't help.

### Pass-2 (cross-episode) coordination

Chromaprint Tier 2 needs all episodes in a season fingerprinted before pairwise comparison. The dispatcher tracks `season_completion_signals` — when all episodes in a `(show_id, season_number)` group have completed Pass 1, a follow-up task runs Pass 2 on a single worker. This is the same shape as PR #191's post-dispatch sweep but threaded through the existing dispatcher instead of a global mutable store.

---

## 10. Settings schema

Per-server, under `output.markers`:

```json
{
  "markers": {
    "enabled": false,
    "detect_intros": true,
    "detect_credits": true,
    "use_native_when_available": true,
    "use_theintrodb": true,
    "theintrodb_submit_key": null,
    "credits_algorithm": "auto",
    "ocr_fallback": false,
    "min_confidence_to_write": 0.7,
    "season_min_episodes": 2,
    "intro_window_sec": 600,
    "credits_window_sec": 600,
    "plex_direct_db_write": false,
    "plex_direct_db_write_confirmed_at": null
  }
}
```

Global settings under `settings.markers`:

```json
{
  "markers": {
    "theintrodb_base_url": "https://api.theintrodb.org",
    "cache_dir": "/config/data/markers_cache",
    "max_concurrent_detections": 2
  }
}
```

### Migration

`upgrade.py` adds the `markers` dict to every existing server entry with `enabled: false`. No existing settings touched.

---

## 11. Testing strategy

### Unit tests

- **Detector algorithms** — golden-file tests with checked-in fingerprint vectors (small `.fpcalc` outputs). Verify chromaprint pairwise math against known matches/mismatches.
- **Blackdetect binary search** — synthetic test videos generated via `ffmpeg -f lavfi -i color=black:d=2:r=24` mixed with `color=white:d=1:r=24`. Cover Superman II (single mid-scene black) and NCIS:Sydney (mid-episode act-break cluster) failure cases as explicit regressions.
- **Marker publishers** — mock vendor APIs, assert specific endpoints called with correct payloads (per `.claude/rules/testing.md`: assert kwargs, not just call count — PR #191's bug-blind tests are the cautionary tale).
- **The write-path matrix** — for each `(server_type × write_capability × marker_type)` cell, write a test row. The matrix-coverage rule from CLAUDE.md applies here especially.

### Integration tests (`tests/integration/`)

- **Mocked TheIntroDB** — `responses` fixture stubbing the HTTP endpoints with known fixture data.
- **Plex SQLite write** — temp `.db` file with the actual `com.plexapp.plugins.library.db` schema (copied from a real PMS for the fixture).
- **Emby Segment_Reporting** — mock the plugin endpoint.
- **Jellyfin MediaSegments** — write a sidecar, instantiate the plugin (via Python mock of the C# logic), verify the emitted `MediaSegmentDto[]`.

### E2E tests (`tests/e2e/`)

- **Marker Inspector** — Playwright flow: search, view item, apply markers, verify the server's `read_markers()` returns them.

### Test media corpus

A small handful of checked-in test files (`tests/fixtures/markers/`):
- `synthetic_episode_with_intro.mkv` — 60s file, 10s synthetic chromaprint-stable "intro" at 0–10s, plus another at 30–40s of a "sibling episode" file with identical 0–10s.
- `synthetic_movie_with_blackend.mkv` — 90s file, 2s sustained black at 80s–82s (the correct credits boundary), plus a 1-frame stray black at 30s (the failure-mode test).
- `theintrodb_match_episode.mkv` — minimal file with a TMDb id in metadata; fixture HTTP server returns markers for it.

Total fixture size target: <50MB.

---

## 12. Phased rollout

Implementation lands in four PRs against `feat/markers-detection`:

### Phase A — Scaffolding + TheIntroDB (PR #1)

- `markers/` package skeleton: `types.py`, `detector.py`, `publisher.py`, `sidecar.py`
- TheIntroDB client + cache
- Read paths only — for each server type, implement `read_markers()`. No writes yet.
- Settings schema additions, migration in `upgrade.py`
- `MARKERS_*` `ProcessingResult` outcomes wired
- A minimal Inspector page that just shows server-reported markers + TheIntroDB lookups (no detection of our own yet)

**Estimated:** 1 week. Useful on its own — gives users a marker viewer for all 3 servers with TheIntroDB enrichment.

### Phase B — Detection algorithms (PR #2)

- Chromaprint Tier 2 with numpy popcount
- Adaptive binary-search blackdetect Tier 3 with GPU decode
- Per-season fingerprint store + Pass-2 dispatcher coordination
- Sidecar JSON writes
- Inspector starts showing "detected" timeline overlay

**Estimated:** 2 weeks.

### Phase C — Per-server marker publishers + Jellyfin plugin (PR #3)

- `PlexMarkerPublisher` — read, trigger native, direct DB write (with provenance/restore loop)
- `EmbyMarkerPublisher` — read, trigger native, write via Segment_Reporting
- `JellyfinMarkerPublisher` — read, write via sidecar consumed by our plugin
- **Extend `MediaPreviewBridge` plugin** — add `MarkerBridgeController` + `MarkerSegmentProvider` (see §7), bump version to `10.11.1.0`, ship via existing CI workflow (`.github/workflows/jellyfin-plugin.yml`) and manifest URL
- Setup Health checks for plugin presence
- Inspector "Apply to server" button works end-to-end

**Estimated:** 2 weeks (plus iteration time on the C# plugin).

### Phase D — Polish + optional OCR (PR #4)

- PaddleOCR Tier 4 (gated behind setting, optional Dockerfile extra)
- Bulk Inspector view (`/inspector/markers/library`)
- TheIntroDB submit-back flow
- Manual edit modal
- Webhook integration (Sonarr/Radarr → run detection alongside BIF)
- Documentation: setup guides for each server, troubleshooting

**Estimated:** 1.5 weeks.

**Total estimate:** ~6–7 weeks of focused work.

---

## 13. Risks &amp; open questions

### Risks

1. **TheIntroDB coverage is small.** Plex's cloud has years of head start. We may submit-back to grow it, but for v1 most files will fall through to local detection. Mitigation: design Tier 2/3 to stand on their own.
2. **Plex SQLite writes wiped by re-analysis.** Our provenance-restore loop helps but isn't perfect. For users without Plex Pass, this is the only path; for users with Plex Pass, native is strictly better. Mitigation: surface this clearly in Setup Health.
3. **C# plugin maintenance burden.** A net-new artifact in a Python repo, requiring `dotnet` toolchain in CI. Mitigation: keep the plugin scope minimal (read sidecar → emit DTO, nothing else). It should rarely need changes.
4. **Chromaprint false matches on shows with shared theme music** (e.g., MCU shows). The `Duration - CreditsFingerprintStart - 1` anti-duplicate check handles same-file re-encodes but not different files with the same outro theme. Mitigation: confidence floor + require pairwise consensus before publishing.
5. **GPU OCR Docker bloat.** PaddleOCR adds ~2GB. Mitigation (committed): ship via a separate `:with-ocr` Docker tag. Default image stays lean; OCR users opt in by image tag. The `markers.ocr_fallback` setting is runtime-detected so it only appears in the UI when `paddleocr` is importable.

### Resolved decisions (2026-05-17 review)

1. **TheIntroDB submit-back is opt-in only** — default off. User enables in settings + provides their per-user API key.
2. **Jellyfin plugin: extend the existing `MediaPreviewBridge`** — we already ship it at `jellyfin-plugin/` with CI + manifest at `stevezau.github.io/media_preview_generator/jellyfin-plugin/manifest.json`. Users who already have the plugin (for trickplay) get marker capability via version bump. See §7.
3. **OCR Tier 4** — see below, still open.
4. **Inspector unified with tabs** — `/bif-viewer` redirects to `/inspector?tab=frames`; markers live at `/inspector?tab=markers`. Shared search/server-picker shell. See §8.

3. **OCR Tier 4: ship via a separate `:with-ocr` Docker tag.** Decision rationale: Plex's native credits detection explicitly combines OCR-style scrolling-text recognition with black-frame detection (per Plex's 2023 blog). EmbyCredits — the de-facto Emby credits plugin — uses Tesseract OCR with keyword matching. Of the three servers, only Jellyfin's intro-skipper omits OCR, and movies are its acknowledged coverage gap. Adopting OCR brings non-Plex-Pass users to feature parity with Plex's gold standard. The `:with-ocr` tag pattern keeps the default image lean (~2GB smaller) for users who don't need OCR, while letting power users opt in via image tag.

   **Implementation notes for the tag:**
   - Main `Dockerfile` stays as-is.
   - New `Dockerfile.with-ocr` extends main image, adds `paddlepaddle-gpu`, `paddleocr`, and PP-OCRv5 model weights.
   - CI builds both tags on release; main is `plex-previews:latest`, OCR variant is `plex-previews:with-ocr`.
   - Settings flag `markers.ocr_fallback` is **detected at runtime**: if the Python `paddleocr` import succeeds, the flag is offered in the UI; otherwise the setting is greyed out with "available in `:with-ocr` image" tooltip.
   - Users can switch from `:latest` to `:with-ocr` by changing one line in their `docker-compose.yml` — settings persist via the volume mount.

---

## 14. References

### Codebase
- `media_preview_generator/servers/base.py` — `MediaServer` abstract interface, `ServerType` enum
- `media_preview_generator/output/base.py` — `OutputAdapter` interface (parallel to our `MarkerPublisher`)
- `media_preview_generator/processing/types.py` — `ProcessableItem`, `ScanOutcome`
- `media_preview_generator/jobs/worker.py` — `Worker._process_item()` per-item orchestrator (this is where the new detection phase hooks in)
- `media_preview_generator/processing/generator.py` — frame extraction + BIF packing primitives (`generate_images()`, `generate_bif()`)
- `media_preview_generator/web/routes/api_bif.py` — BIF Inspector routes (template for Marker Inspector)

### Prior art
- **intro-skipper/intro-skipper** — Jellyfin plugin, GPL-3. Reference algorithm for chromaprint (`IntroSkipper/Analyzers/ChromaprintAnalyzer.cs`) and adaptive blackdetect (`BlackFrameAnalyzer.cs`). **Algorithm reimplemented from spec, source not copied.**
- **danrahn/MarkerEditorForPlex** — reference for Plex SQLite write safety + provenance/restore pattern.
- **sydlexius/Segment_Reporting** — Emby plugin we leverage. `POST /emby/segment_reporting/update_segment` is our write path for Emby.
- **TheIntroDB/jellyfin-plugin** — reference for the TheIntroDB API surface and TMDb-keyed lookup.

### External APIs
- TheIntroDB: `https://api.theintrodb.org` — `GET /media?tmdbId=…&season=…&episode=…`
- Plex marker schema: `taggings` table in `com.plexapp.plugins.library.db`
- Emby chapter API: `GET /Items/{id}?Fields=Chapters` with `MarkerType` field (Ticks = 100ns)
- Jellyfin MediaSegments: `GET /MediaSegments/{itemId}`, `IMediaSegmentProvider` in-process interface

### Failed approaches documented for posterity
- **PR #191** (`feat/intro-credits-detection`, 2026-03-22) — `blackdetect+silencedetect` on last 25%. Failed on Superman II (single mid-scene black) and NCIS: Sydney (mid-episode act break). Status: stalled, will be superseded by this design.
- **Plex marker REST API for writes** — `POST /library/metadata/{id}/marker` returns 400 on `intro` and `credits` types (only `bookmark` works, no client honors it). Confirmed by Plex community devs over multiple years. Direct SQLite is the only write path.
- **Emby marker REST API for writes** — never existed. Luke (Emby Team) confirmed in 2018; status unchanged in 2026. Plugin path is mandatory.
- **Jellyfin MediaSegments POST** — controller is GET-only in `master`. Plugin path mandatory.
