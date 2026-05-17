# Intro/Credits Detection — Multi-Server Design Spec

**Date:** 2026-05-17
**Branch:** `feat/markers-detection`
**Status:** Draft for review
**Path:** `docs/design/2026-05-17-intro-credits-detection-design.md`
**Supersedes:** PR #191 (`feat/intro-credits-detection`, Plex-only, stalled 2026-04-23)

---

## 0. TL;DR

We add intro/credits ("marker") detection as a first-class processing stage alongside BIF generation. Detection runs through a **seven-step cascade — five primary tiers (0/1/2/3/4) with sub-tier fallbacks (1b, 1c, 2.5)**:

- **Tier 0** — Embedded chapter-name parsing via the existing `pymediainfo` Menu-track (free, frame-accurate, ~20% TV / 16% movies on user's library).
- **Tier 1** — TheIntroDB v2 cloud lookup (~57% TV / 35% movies; v1 sunsets 2026-08-16).
  - **Tier 1b** — IntroDB.app fallback (IMDb-keyed, single-submission noisier).
  - **Tier 1c** — anime-skip.com (AniList-keyed, only for `library.kind == "anime"`).
- **Tier 2** — Chromaprint cross-episode fingerprinting for TV (reimplemented from intro-skipper spec, numpy-accelerated).
  - **Tier 2.5** — Subtitle-keyword recap detection (closes the "holy-grail recap detector" gap nobody ships).
- **Tier 3** — Adaptive binary-search `blackdetect` for movies and chromaprint-missed TV (the structural fix for PR #191).
- **Tier 4** — PaddleOCR PP-OCRv5 for movies that fade-to-color (optional, GPU, separate `:with-ocr` Docker tag).

The output is a **canonical `.markers.json` sidecar** next to the media file, fanned out to three **per-server marker publishers**:

- **Plex** — read existing markers via plexapi; trigger native detection (`episode.analyze()`) for Plex Pass users; for non-Pass users, write directly to the `taggings` SQLite table with provenance/restore (off by default, big warnings).
- **Emby** — read via `GET /Items/{Id}?Fields=Chapters`; trigger native scheduled task; write via the existing community `sydlexius/Segment_Reporting` plugin's `POST /emby/segment_reporting/update_segment` endpoint (we require this plugin be installed; surfaced as a Setup Health check).
- **Jellyfin** — read via `GET /MediaSegments/{itemId}`; write by **extending our existing `Jellyfin.Plugin.MediaPreviewBridge` plugin** (already shipped for trickplay registration, distributed via `stevezau.github.io/media_preview_generator/jellyfin-plugin/manifest.json`). Add a new `MarkerBridgeController` (`POST /MediaPreviewBridge/Markers/{itemId}`) plus an `IMediaSegmentProvider` implementation so Jellyfin's scheduled scan also picks up our sidecars. No new plugin install for users — version bump only.

A new **Markers Inspector** page (standalone at `/markers` in Phase A; merges with BIF Inspector into a unified `/inspector?tab=…` in Phase D) lets users search, visualize the timeline against an audio waveform, compare our detected markers vs. the server's existing ones, manually nudge boundaries, and one-click apply.

**Community-validated UX features that turn this into a category-creating tool** (see §7.5):
- **Locked markers** — re-detection never overrides user edits (the 5+ year community pain across Plex/Emby/Jellyfin).
- **Apply-pattern-to-season** — user fixes S01E01's intro; tool uses that segment's audio as the canonical fingerprint to detect intros across the season (nurbles 2020, never built anywhere).
- **Per-show overrides** — auto-skip yes/ask/no, extended detection windows past 10-min server ceilings, per-show disable. (Plex literally cannot do this.)
- **Multi-OP anime mode** — detects N distinct intros per season for shows that change OPs mid-cour (intro-skipper dismissed as out-of-scope).
- **Subtitle-keyword recap detector (Tier 2.5)** — finds "Previously on" via subtitle parsing. Closes the holy-grail recap-detection gap every existing tool has punted on.
- **Per-client compatibility matrix** — Inspector surfaces which markers will render in which clients (Swiftfin/Roku/Tizen blind spots are unspoken nightmare; we tell users upfront).
- **Cross-server marker bridge** — read Plex Pass markers, push to Jellyfin/Emby. Solves the family/shared-user entitlement gap + migration story.
- **Cache sharding** from day one (50TB libraries need 256-way directory sharding — `ototos`'s 500,000-file projection).

Phased rollout in four PRs (A → D):
- **A** — Scaffolding + Tier 0 chapters + TheIntroDB read-only Inspector (~1 week, useful on its own)
- **B** — Tier 2 chromaprint + Tier 3 blackdetect detection algorithms + sidecar writes (~2 weeks)
- **C** — Per-server marker publishers + `MediaPreviewBridge` plugin extension (~2 weeks)
- **D** — Polish, Tier 4 OCR, bulk Inspector, TheIntroDB submit-back, webhook integration (~1.5 weeks)

Total: ~6–7 weeks. Each phase ships something useful on its own.

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
│  → items   │  │ + BIF      │  │ (BIF    │  │ (5-tier) │  │ (.json) │  │  (per-server)│
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
    source: str = "detected"         # "detected" | "native_plex" | "native_emby" | "theintrodb" | "introdb" | "anime_skip" | "skipmedb" | "manual" | "chapter" | "subtitle_recap"
    detector: str | None = None      # e.g. "chromaprint_v1", "blackdetect_binsearch_v1", "subtitle_keyword_v1"
    locked: bool = False             # if True, re-detection NEVER overrides this segment. User-set via Inspector. Closes the 5+ year "Plex re-analyze wipes my edits" community pain (Frugglehost, MarkerEditor #26).
    seed: bool = False               # if True, this is a USER-PROVIDED FINGERPRINT SEED — re-detection uses this segment's audio as the canonical pattern when applied to a show/season. Implements nurbles 2020 "use manually marked intros as patterns".


@dataclass(frozen=True)
class MarkerSet:
    """Canonical sidecar JSON content. Single source of truth for a file's markers."""
    canonical_path: str              # the source media file
    detection_run_id: str            # UUID for the run that produced this set
    detector_version: str            # semver of our detector
    detected_at: str                 # ISO timestamp
    segments: tuple[MarkerSegment, ...]
    season_context: dict | None = None  # for TV: {show_id, season_number, episodes_compared}
    schema_version: int = 1          # bump when shape changes
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

**Example (full schema with all fields, including the new `locked` / `seed` / `schema_version`):**
```json
{
  "schema_version": 1,
  "canonical_path": "/data/media/tv/Show/S01E01.mkv",
  "detection_run_id": "5f9e2a01-1234-...",
  "detector_version": "1.0.0",
  "detected_at": "2026-05-17T20:42:18Z",
  "segments": [
    {
      "type": "intro",
      "start_ms": 16000,
      "end_ms": 92000,
      "confidence": 0.91,
      "final": false,
      "source": "manual",
      "detector": null,
      "locked": true,
      "seed": true
    },
    {
      "type": "credits",
      "start_ms": 2580000,
      "end_ms": 2710000,
      "confidence": 0.78,
      "final": true,
      "source": "detected",
      "detector": "blackdetect_binsearch_v1",
      "locked": false,
      "seed": false
    }
  ],
  "season_context": {"show_title": "Show", "season_number": 1, "episodes_compared": ["S01E02", "S01E03"]},
  "notes": {}
}
```

**Consumer rules for the new fields:**
- **`locked: true`** — Marker publishers MUST treat this as authoritative; re-detection produces a parallel "candidate" segment but does NOT overwrite. Plex restore loop checks this before re-INSERTing.
- **`seed: true`** — Tier 2 chromaprint pass-2 uses this segment's audio as the canonical fingerprint anchor when matching across the season (instead of pairwise consensus). At most one `seed=true` segment per `type` per season; deduplicate at the season-store level.
- **`schema_version: 1`** — Bumped when the JSON shape changes. The C# plugin's deserializer uses `JsonExtensionData` for unknown fields (forward-compat); the Python publisher's loader bumps the schema gracefully.
- Unknown source values (e.g. a future `"source": "user_supplied_db"`) — consumers MUST NOT raise; treat as `"detected"` and log.

**Sidecar is read by:**
- Marker publishers when fanning out to servers
- `MediaPreviewBridge` Jellyfin plugin (`IMediaSegmentProvider`) at scan time
- Marker Inspector UI for display
- Future re-publish flows ("apply existing detections to a newly-added server")

---

## 5. Detection algorithms (the five-tier cascade)

The order is **cheapest first, then by precision, then by cost**. Each tier short-circuits the cascade only when it returns high-confidence markers; otherwise we accumulate evidence across tiers and apply the multi-tier-agreement gate at the end.

### Tier 0 — Embedded chapter-name parsing (FREE, frame-accurate)

**Why this is Tier 0:** The studio that authored the file *already told us* where the intro and credits are, via named chapters in the MKV/MP4 container. We just have to read them. Frame-accurate boundaries when the studio labeled them.

**Empirical hit rate on the user's real library** (160-file probe of `/data` on 2026-05-17):
- TV episodes: **20% have an explicit credit-marker chapter**, concentrated in Netflix/Amazon WEBDLs (80% of TV-WEBDLs *with chapters* have credit markers)
- Movies: **16%** with explicit credit-marker chapters
- Blu-ray rips rarely have semantically-labelled chapters (generic `Chapter 01`); Netflix WEBDLs almost always have at least an "End Credits" boundary

**Cost: piggyback on the existing `pymediainfo` parse** — the codebase already calls `MediaInfo.parse(video_file)` at `processing/generator.py:769` (inside the per-file pipeline; the `MediaInfo.can_parse()` at `:291` is just a startup probe). Chapter names are exposed on the `Menu` track of the parsed result; we just extend the existing parse-result handling. **No new subprocess, no new dependency.** Live-verified on a known chapter-bearing AMZN WEBDL.

**Algorithm:**
1. From the existing `MediaInfo.parse()` result, find the `Menu` track (`track.track_type == "Menu"`).
2. Iterate the menu attributes whose keys match `^[0-9]{2}_[0-9]{2}_[0-9]{5}$` — the **timestamp format is `HH_MM_SSmmm`** (5-digit padded milliseconds). Example: `00_25_49000` → 0h25m49.000s = 1549s.
3. Values are language-prefixed strings — `'en:Opening Credits'`, `'en:End Credits'`. Strip the `XX:` language prefix (any 2-letter ISO 639-1 code) before regex matching.
4. Apply Jellyfin's published regex defaults (`jellyfin/jellyfin-plugin-chapter-segments`, `PluginConfiguration.cs`) for cross-platform parity:
   - **Intro:** `(?i)\b(intro|opening)\b|^OP$`
   - **Credits (outro):** `(?i)\b(outro|closing|credits|ending)\b|^ED$`
   - **Recap:** `(?i)\b(recap|last time on|last on|previously on)\b`
   - **Preview:** `(?i)\b(preview|next time on|next on|sneak peek)\b`
   - **Commercial:** `(?i)\b(break|ad|advertisement|intermission|advert|commercial)\b`
5. Negative match `^[Cc]hapter\s*0?\d+$` to ignore generic numbered chapters.
6. Defensive parsing for mojibake-encoded chapter titles (seen in the probe: `绗?16 绔?` and `01:55:26.544` as chapter "names") — wrap regex match in try/except, skip and log on parse failure; never block the pipeline on a single bad chapter title.
7. Confidence = 0.95 (high — the studio said so).
8. End-of-segment computation: for chapters that match the *start* of a region (e.g. "Opening Credits"), use the next chapter's timestamp as the end. For trailing "End Credits", use the file duration.

**Killer example** (live pymediainfo output on user's `Sex and the City S02E09`):
```
Menu track, ~17 entries:
  00_00_00000: 'en:Studio Logo'
  00_00_16000: 'en:Opening Credits'      →  intro_start_ms = 16000
  00_00_58000: 'en:Men continue to check out other women'   →  intro_end_ms = 58000
  ...
  00_24_22000: 'en:Steve calls Miranda'
  00_25_49000: 'en:End Credits'          →  credits_start_ms = 1549000
                                         →  credits_end_ms = file_duration_ms
```
Both boundaries free, frame-accurate, no extra compute, no network. Phase A budget: ~½ day to write + test the Menu-track parser.

**Sonarr/Radarr routing hint:** when the publisher's manifest tells us a file is from `releaseGroup=NTb` or `quality.source=WEBDL`, run Tier 0 *first*. When the release group is `Hares`/`YAWNiX`/`-d3g` (typical Bluray P2P groups), skip Tier 0 entirely on the next-file shortcut — those releases almost never have useful chapter names. This is a per-show cache (one Sonarr call per series, not per episode).

**Why not write our own chapter-name database:** Jellyfin already maintains the canonical regex list and updates it as new conventions emerge. We mirror their config and inherit their updates.

### Tier 1 — TheIntroDB lookup (live-verified 2026-05-17)

**Endpoint:** `GET https://api.theintrodb.org/v2/media?tmdb_id={id}[&season={s}&episode={e}]` (also accepts `imdb_id=tt0000000`). OpenAPI 2.0.0 spec mirrored to `docs/design/theintrodb-openapi-v2.yaml`.

⚠️ **v1 sunsets 2026-08-16** — every `/v1/*` response carries `sunset: Sun, 16 August 2026 00:00:00 GMT`. We build directly on v2 from day one. The schema is identical; only the path differs.

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

**Failure mode:** API 5xx / rate-limit 429 → fall through to Tier 1b/2/3. Never block the pipeline on TheIntroDB. Circuit-breaker pattern: after 5 consecutive failures within 60s, skip TheIntroDB for the next 5 minutes.

**Sustainability + operational risk:** `Pasithea0` is the sole public org member of TheIntroDB on GitHub — no funding, no Patreon, no co-maintainer. The community side is healthy (295 contributors, 323,404 submissions, 1.79 accepted timestamps per media on average, 3.97 submissions per acceptance — real consensus voting). Release cadence is healthy too (three plugin releases in the last 10 weeks). But single-maintainer fragility means we **MUST cache aggressively**: positive hits cached *forever* (immutable once accepted); 404s cached 14 days. We also keep our pipeline graceful — if TheIntroDB disappears tomorrow, Tier 2/3 still works.

**Optional: self-hosted caching proxy.** No bulk-export endpoint exists; mirror by enumeration would require ~91 days under rate limits. If our user base grows large, we email `hello@theintrodb.org` to request either a bulk dump or higher rate-limit tier. For Phase A we ship just the per-user cache.

### Tier 1b — IntroDB (introdb.app) fallback

**Endpoint:** `GET https://api.introdb.app/segments?imdb_id={tt...}&season={s}&episode={e}` (TV only, no movies, IMDb ID only).

**Why a second source:** Separate project from TheIntroDB; ~73% hit rate on popular TV S1E1 (higher than TheIntroDB's 57%) — but most entries have `submission_count: 1` (single contributor, no consensus). Useful as a fallback for shows TheIntroDB misses, gated by a stricter confidence floor.

**Response shape** (verified live):
```json
{"imdb_id":"tt0944947","season":1,"episode":1,
 "intro":  {"start_sec":437, "end_sec":531, "start_ms":437000, "end_ms":531000,
            "confidence":1, "submission_count":1,
            "updated_at":"2026-04-08T18:48:07.297Z"},
 "outro":  {"start_sec":3631.5, "end_sec":3699.5, "submission_count":2, ...},
 "recap":  null}
```

**Rules:**
- Only consulted when TheIntroDB returns 404 (or we explicitly opt in for higher coverage).
- Reject single-`submission_count` entries by default — too noisy for auto-write. Surface in Inspector as a low-confidence suggestion the user can manually accept.
- Anonymous reads work; API key only required for submit.
- Smaller user base, no GitHub org, no funded sponsorship — **higher disappearance risk than TheIntroDB.** Cache the same way (positive → forever, 404 → 14 days).

### Tier 1c — anime-skip.com (anime libraries only)

**Endpoint:** GraphQL at `https://api.aniskip.com/v2/skip-times/{anilist_id}/{episode}` (REST wrapper; GraphQL also available).

**Why:** Anime is a known weak spot for chromaprint cross-episode matching — many shows have multiple openings per cour (OP1 for episodes 1–13, OP2 for 14–25). anime-skip.com is the de-facto anime intro/credits DB (AniList/MAL-keyed, separate ecosystem from TheIntroDB).

**Conditional activation:** This tier only fires when the user has explicitly marked a library as anime via a new per-library setting `markers.anime_mode_libraries: list[str]` (list of library IDs). Phase A adds this setting key (defaulted empty) + a Settings UI toggle "Treat this library as anime". Auto-detection from genre is a Phase D enhancement — not load-bearing for Phase A.

(The existing `Library.kind` field at `servers/base.py:118` is opaque per its docstring and is set by server enumeration to `"movie"`/`"show"` — we don't repurpose it. The anime mode flag lives in our app-side settings, keyed by `(server_id, library_id)`.)

**Coverage:** Reportedly strong for anime; verified live: yes, returns intro/outro for popular AniList IDs. Free; rate-limited (per their docs).

**Status:** Optional in Phase A — gated by a `markers.use_anime_skip` setting (default ON for users who have an anime library).

### Tier 2 — Chromaprint cross-episode (TV only)

**Algorithm (reimplemented from `intro-skipper/IntroSkipper/Analyzers/ChromaprintAnalyzer.cs` spec, NOT copy-pasted from the GPL-3 source):**

1. **Pass 1, per-worker:** Extract a chromaprint fingerprint from a fixed window using `ffmpeg -ss 0 -t 600 -i {file} -ac 1 -ar 22050 -f chromaprint -fp_format raw -`. For intros: window = first 10 min. For credits: window = `Duration - CreditsFingerprintStart` (default last 10 min) → EOF.
2. **Per-season fingerprint store:** Thread-safe dict keyed by `(show_id, season_number)`. Episodes deposit their fingerprint as they finish Pass 1.
3. **Pass 2, post-dispatch (sequential, runs after all workers idle for that season):** Build inverted index of fingerprint hashes per episode. For each pair of episodes in the season, find the largest contiguous matching subsequence (sliding window Hamming distance, threshold ≤8 bits out of 32). Use **numpy** for the Hamming popcount — `np.unpackbits(arr.view(np.uint8)).sum(axis=-1)` is ~100× faster than `bin().count("1")` (PR #191's hot-path mistake).
4. **Pairwise consensus:** Compare episode 0 vs episodes 1–5, then 1 vs 2–5, then 2 vs 3–5. Match candidates with ≥2 pairwise agreements within 4 seconds become the segment. Single-pair matches are discarded.
5. **Constraints (intro-skipper canonical defaults, verified from `PluginConfiguration.cs`):**
   - `MinimumIntroDuration = 15s`, `MaximumIntroDuration = 120s`
   - `MinimumCreditsDuration = 15s`, `MaximumCreditsDuration = 450s` (TV), `MaximumMovieCreditsDuration = 900s`
   - Intro search window: first `DefaultAnalysisPercent = 25%` of episode OR `DefaultAnalysisLengthLimit = 10 min`, whichever is shorter ⚠️ — **Emby's 10-min hardcoded ceiling problem is the same Plex has; our settable max gets us past intros that start later (NCIS:Sydney-style recap+cold-open episodes).**
   - **`MaximumFingerprintPointDifferences = 6`** (Hamming bits — corrected from earlier spec's "8")
   - `MaximumTimeSkip = 3.5s` (max gap between matched fingerprint points)
   - `InvertedIndexShift = 2`
   - `ChromaprintConstants.SampleDuration = 4096 / 11025 / 3 ≈ 0.124s` per fingerprint point
   - `BlackFrameMinimumPercentage = 85`, `BlackFrameThreshold = 28` (out of 255 ≈ 0.11)
6. Reject matches exceeding `Duration - CreditsFingerprintStart - 1` (anti-duplicate-file safety from intro-skipper's spec).

**Known failure modes from intro-skipper's own users (community research):**
- **Bob's Burgers S01** — concrete documented chromaprint failure (skipme.db #29). Mainstream sitcom, fails to match. Validates the multi-tier approach.
- **Single-episode seasons** — chromaprint needs ≥2 episodes; no fallback. Our Tier 0/3 still cover these.
- **Cold-open shows** — intro after a 2-min teaser (X-Files, Murder She Wrote). Plex's "ignore intros past 50% of file" rule kills these. Our window default is 600s but configurable per-show.
- **Anime mid-season OP change** — single-fingerprint-per-season model fails on cours (#658 was dismissed by intro-skipper team). We support **N fingerprints per season** when `library.kind == "anime"` (see Tier 1c + anime mode below).
- **Recap mis-classified as intro** — when ED audio plays as recap in episode 1 (Anime issue #595). Our subtitle-recap detector (Tier 2.5) provides an independent signal.

**No GPU benefit** — chromaprint is integer DCT on a 22kHz mono stream. GPU round-trip costs more than the compute. Per-episode CPU: ~5–10s on a single core.

### Tier 2.5 — Subtitle-keyword recap detection (NEW — the "holy grail" recap detector nobody ships)

**The unmet community ask:** intro-skipper #763 (open May 2026), #136 (open since 2024), Emby topic #120144, Plex thread #787447 — recap detection. Every maintainer punted. `Hellowlol/bw_plex` demonstrated subtitle-keyword "Previously on" matching works but isn't maintained. **Whoever ships polished recap detection first owns it.**

**Algorithm:**
1. Locate embedded subtitle track via the existing `pymediainfo` parse (same call as Tier 0 chapter extraction). Prefer English/native-language `.srt` or `.ass`; if absent, fall back to `ffmpeg -map 0:s:0 -f srt - < file`.
2. Scan first 10 minutes of subtitles for the keyword set (mirrored from Jellyfin's chapter-segments regex for consistency):
   ```
   (?i)\b(previously on|last time on|last on|recap)\b
   ```
   Plus localized variants per language: `réplay|au précédent` (FR), `bisher|zuletzt bei` (DE), `これまでの`/`前回` (JA), `이전 이야기` (KO), etc.
3. First subtitle line matching the keyword set = **recap_start**.
4. Recap end = next subtitle gap >5s (typical recap-to-cold-open transition) OR next chapter boundary (Tier 0 informs) OR fixed 90s upper-bound.
5. Confidence = 0.85 (high — text is a strong intent signal).

**Why this works where chromaprint fails:** recaps don't repeat across episodes (each is a different summary), so chromaprint sees nothing. But the host always says "Previously on Show X" at the start. Subtitle audio coverage on streaming releases is near-100%; for non-subtitled content, fall through.

**Cost:** Pure CPU regex scan of a text file. ~10ms per episode. No GPU. No FFmpeg subprocess. **Smallest tier in the cascade by 100×.**

**Edge cases handled:**
- No subtitle track → skip tier, no error.
- Subtitle in a non-supported language → keyword regex returns empty, fall through.
- "Previously on" appears in dialogue mid-episode → only match if it's in the first 10 min AND it's the first such match.
- Mojibake / encoding errors → defensive try/except, log + skip.

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

**REST API status (live-confirmed Feb–Apr 2026):** `POST /library/metadata/{id}/marker?type=intro` still returns **400 Bad Request**. Confirmed by a Plex admin's own forum post in dev-corner thread #931778 (Feb 2026), reiterated by multiple devs through April. Plex's Sept 2025 "API Unlocked" was a documentation release of `LukeHagar/plex-api-spec` — not a functionality change. The auto-generated `plexjs`/`plexruby`/`plexphp` SDKs will happily call `createMarker` but receive 400s. **Skip the REST API entirely for marker writes.**

```python
class PlexMarkerPublisher(MarkerPublisher):
    def supports_write(self) -> WriteCapability:
        # Native trigger doesn't write our markers — it asks Plex to detect.
        # Returned alongside direct-db so the publisher decides per-call which to use.
        if self._has_plex_pass() and self._native_detection_enabled():
            return WriteCapability.TRIGGER_NATIVE
        if self._direct_db_enabled():
            return WriteCapability.DIRECT_DB
        return WriteCapability.READ_ONLY
```

**Read:** `episode.markers` via plexapi. Returns `Marker(type, start, end, final, version)`. Fully supported and unchanged since `1e220eb` (credit markers added).

**Trigger native — use the NARROW endpoints, not `/analyze`:**

| Verb | Path | Params (BoolInt: `0`/`1` ints — Python `True`/`False` will 500) | Purpose |
|---|---|---|---|
| `PUT /library/metadata/{id}/intro` | `force=1` (re-detect even if marker exists), `threshold=80` (audio similarity, 0–100) | Intro-only re-analysis |
| `PUT /library/metadata/{id}/credits` | `force=1`, `manual=0` | Credits-only re-analysis |
| `PUT /library/metadata/{id}/analyze` | (no params commonly used) | Full pipeline — only when we want metadata + art + BIF + markers together |

plexapi has no helper for `/intro` and `/credits`. Use the documented `server.query()` idiom (note: `server._session` is technically private but is the stable access pattern across plexapi v4–v5):
```python
server.query(
    f"/library/metadata/{rating_key}/intro",
    method=server._session.put,
    params={"force": 1, "threshold": 80},  # MUST be ints, not bools — Plex BoolInt rejects True/False
)
```
All three endpoints are fire-and-forget (return 200 immediately, scheduler does the work async). Poll `episode.reload(); episode.hasIntroMarker` for completion. A test asserting `params` contains integer values (not bools) is mandatory — `assert isinstance(call_kwargs["params"]["force"], int) and not isinstance(call_kwargs["params"]["force"], bool)`.

**Direct DB write (when enabled) — the MarkerEditor pattern, not Casvt's incomplete pattern:**

This is critical. Two tables must be written for markers to survive Plex's re-analysis:

1. **`taggings` row** (movie-wide marker) — what Plex's metadata XML returns to clients.
2. **`media_parts.extra_data` JSON** (per-file denormalized copy) — what Plex's analyzer reads. **Without this, markers vanish on next analyze.** This is the load-bearing detail that Casvt's `intro_marker_editor.py` misses and MarkerEditorForPlex gets right.

Step-by-step:

```python
# 1. Resolve tag_id at write time — NEVER hard-code.
# All markers (intro/credits/commercial) share ONE tags row with tag_type=12.
# They are differentiated by taggings.text, NOT by separate tag_types.
tag_id = conn.execute("SELECT id FROM tags WHERE tag_type=12 LIMIT 1").fetchone()
if tag_id is None:
    # Brand-new install never had markers — bootstrap the row.
    tag_id = conn.execute(
        "INSERT INTO tags (tag_type, created_at, updated_at) "
        "VALUES (12, strftime('%s','now'), strftime('%s','now'))"
    ).lastrowid

# 2. For the metadata_item, enumerate ALL its media_parts
#    (multi-version movies have multiple — 4K + 1080p share one metadata_item_id).
parts = conn.execute("""
    SELECT mp.id, mp.extra_data
    FROM media_parts mp
    JOIN media_items mi ON mp.media_item_id = mi.id
    WHERE mi.metadata_item_id = ?
""", (metadata_item_id,)).fetchall()

# 3. INSERT into taggings (movie-level row).
#    extra_data JSON shape depends on PMS version:
#    PMS ≥1.40: {"pv:version":"5","url":"pv%3Aversion=5"}  for intros
#               {"pv:version":"4","pv:final":"1","url":"pv%3Afinal=1&pv%3Aversion=4"} for FINAL credits
#               {"pv:version":"4","url":"pv%3Aversion=4"}  for non-final credits
#    PMS <1.40: legacy URL-encoded form (Plex tolerates both on read).
next_index = conn.execute(
    "SELECT COALESCE(MAX([index]), -1) + 1 FROM taggings WHERE metadata_item_id=?",
    (metadata_item_id,)
).fetchone()[0]
conn.execute("""
    INSERT INTO taggings
    (metadata_item_id, tag_id, [index], text, time_offset, end_time_offset,
     thumb_url, created_at, extra_data)
    VALUES (?, ?, ?, ?, ?, ?, '', strftime('%s','now'), ?)
""", (metadata_item_id, tag_id, next_index, marker_type, start_ms, end_ms, extra_data_json))

# 4. UPDATE each media_part.extra_data with the denormalized JSON copy.
#    This is the MarkerEditor pattern. Without it, marker disappears on re-analyze.
for part_id, current_extra in parts:
    merged = merge_marker_into_part_extra(current_extra, marker_type, start_ms, end_ms, final, pms_version)
    conn.execute(
        "UPDATE media_parts SET extra_data=? WHERE id=?",
        (merged, part_id)
    )
```

**The `merge_marker_into_part_extra` function — exact spec (cribbed from MarkerEditorForPlex `MediaAnalysisWriter.js:208-258`):**

Input states for `current_extra`:
- `NULL` or `""` or `'{}'` → treat as empty object; build new structure.
- Already has `pv:intros` / `pv:credits` keys → parse, append new marker into the array, sort by `startTimeOffset`, re-serialize.
- Has the legacy URL-encoded-only form (PMS <1.40) → parse from URL params, rebuild as ≥1.40 JSON if we're writing on PMS ≥1.40 (Plex tolerates both forms on read), else preserve legacy shape.

**PMS ≥1.40 result shape (the form we always write):**
```json
{
  "pv:intros": "{\"MediaPartMarkersArray\":{\"attributeName\":\"intros\",\"version\":5,\"MediaPartMarker\":[{\"startTimeOffset\":12000,\"endTimeOffset\":45000}]}}",
  "pv:credits": "{\"MediaPartMarkersArray\":{\"attributeName\":\"credits\",\"version\":4,\"MediaPartMarker\":[{\"startTimeOffset\":5400000,\"endTimeOffset\":5460000,\"final\":true}]}}",
  "url": "&pv%3Aintros=%7B%22Media...%7D&pv%3Acredits=%7B%22Media...%7D"
}
```

**Key invariants** (test these explicitly):
1. **Inner `MediaPartMarker` is always an array**, even when only one marker exists. Single-marker case: `[{startTimeOffset, endTimeOffset}]`.
2. **`url` key contains URL-encoded copies of `pv:intros` and `pv:credits`** values. Plex reads this for non-JSON-aware client paths. Build from the JSON values using `urllib.parse.quote(value, safe="")`.
3. **`final: true` only present on credits markers**, never on intros. Omit the field entirely (don't write `final: false`) for non-final.
4. **JSON keys are sorted alphabetically** when re-serializing (Plex's own writes do this; mismatch can trigger re-write loops).
5. **`attributeName` matches the parent key** — `pv:intros` value has `"attributeName":"intros"`.
6. **`version: 5` for intros, `version: 4` for credits** (PMS ≥1.40). Older PMS uses `version: 4` for both; check `PMS-Version` HTTP response header at startup to pick.

**Worked example — merging a credits marker into a row that already has an intro:**

Existing `extra_data`:
```json
{"pv:intros":"{\"MediaPartMarkersArray\":{\"attributeName\":\"intros\",\"version\":5,\"MediaPartMarker\":[{\"startTimeOffset\":12000,\"endTimeOffset\":45000}]}}","url":"&pv%3Aintros=%7B%22MediaPartMarkersArray%22%3A%7B...%7D"}
```

New credits marker: `start_ms=5400000, end_ms=5460000, final=true`

After merge:
```json
{"pv:credits":"{\"MediaPartMarkersArray\":{\"attributeName\":\"credits\",\"version\":4,\"MediaPartMarker\":[{\"startTimeOffset\":5400000,\"endTimeOffset\":5460000,\"final\":true}]}}","pv:intros":"{\"MediaPartMarkersArray\":{\"attributeName\":\"intros\",\"version\":5,\"MediaPartMarker\":[{\"startTimeOffset\":12000,\"endTimeOffset\":45000}]}}","url":"&pv%3Acredits=%7B%22MediaPartMarkersArray%22%3A...&pv%3Aintros=%7B%22MediaPartMarkersArray%22%3A%7B..."}
```
(Keys alphabetized: `pv:credits` before `pv:intros`; `url` appended with both URL-encoded copies.)

Phase C tests must include `(NULL, "", "{}", legacy-URL-encoded, ≥1.40-with-intro-only, ≥1.40-with-both)` × `(adding intro, adding credits final, adding credits non-final)` = 18 test rows.

**Editions vs versions:**
- **Versions** (same movie, multiple files merged in Plex): one `metadata_item_id`, N `media_parts`. Write taggings once + every part's extra_data.
- **Editions** (Director's Cut vs Theatrical — separate metadata items): write twice, once per edition.

**Filesystem &amp; locking:**
- DB path: `{plex_config}/Plug-in Support/Databases/com.plexapp.plugins.library.db`
- `sqlite3.connect(path, timeout=30.0)` + `PRAGMA busy_timeout=30000` (MarkerEditor uses 30s; Casvt uses 20s). WAL mode is already on by PMS.
- `fcntl.flock(LOCK_SH | LOCK_NB)` probe first to detect Docker-on-Windows (CIFS/SMB doesn't honor advisory locking). On failure, surface as `HealthCheckIssue` "Plex DB write unsafe on this filesystem" with severity=critical.
- **Live writes are fine** — PMS keeps running. Markers become visible on next client metadata fetch; no PMS restart required.

**Provenance — side-car DB, not flag in `taggings`:**
- Create `markers_provenance.db` alongside our `jobs.db` (in `${CONFIG_DIR}`).
- Schema mirrors MarkerEditor's `actions` table (the load-bearing fields) **plus our `locked` / `seed` fields** so the restore loop respects user intent:
  ```sql
  CREATE TABLE provenance (
    plex_marker_id INTEGER PRIMARY KEY,    -- taggings.id
    plex_server_id TEXT,                   -- our server_id
    parent_guid TEXT,                      -- metadata_item.guid (stable across reinstalls)
    metadata_item_id INTEGER,              -- not stable; secondary key only
    marker_type TEXT,                      -- "intro" | "credits" | "commercial"
    start_ms INTEGER, end_ms INTEGER, final INTEGER,
    user_created INTEGER DEFAULT 1,        -- always 1 from our app
    locked INTEGER DEFAULT 0,              -- mirrors MarkerSegment.locked — restore loop checks this
    seed INTEGER DEFAULT 0,                -- mirrors MarkerSegment.seed — needed when re-seeding fingerprint pass-2
    detection_run_id TEXT,                 -- our MarkerSet.detection_run_id
    source TEXT DEFAULT 'detected',        -- mirrors MarkerSegment.source
    detector TEXT,                         -- e.g. "chromaprint_v1"
    written_at INTEGER
  );
  CREATE INDEX idx_provenance_parent_guid ON provenance(parent_guid);
  CREATE INDEX idx_provenance_locked ON provenance(locked) WHERE locked = 1;
  ```
- **Restore loop checks `locked`:** when re-INSERTing a wiped marker, if the latest-known `locked=1` value disagrees with the candidate detection, the user's value wins and the candidate goes to the Inspector's "pending review" queue.
- **Earlier hacks (now abandoned by MarkerEditor):** setting `taggings.thumb_url` to a sentinel string, appending `*` to `modified_at`. These were the V1–V5 patterns; we ship the V7 pattern (provenance side-car only) from day one.

**Restore loop:** periodic task (default 6h) scans `provenance` table for `plex_marker_id`s that no longer exist in `taggings` (re-analysis purged). For each missing row, re-INSERT taggings + re-merge `media_parts.extra_data` using the saved fields. Resolve by `parent_guid` (stable), not `metadata_item_id` (changes on re-add).

**Plex Pass requirement is on the PLAYER account:**
- Our SQLite writes succeed regardless of Pass status, but Plex clients (web, iOS, Android, tvOS, Roku, Smart TVs) only render the skip-intro / skip-credits buttons when the **playback account** has Plex Pass.
- Server-admin-Pass alone is enough to *write* markers (and for the timeline overlay in the admin's web UI), but family members without Pass watching from the same library won't see skip buttons. Surface this clearly in the Inspector tooltip when Pass status is missing.

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
        # Phase A: returns READ_ONLY unconditionally (no write capability yet
        # regardless of plugin version — avoid surfacing Setup Health warnings
        # for users who don't need marker writes).
        # Phase C: enables the version check below once MarkerBridgeController ships.
        if not _PHASE_C_ENABLED:
            return WriteCapability.READ_ONLY
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

## 7.5. Community-validated UX features (the differentiator)

Five rounds of community-sentiment research (r/PleX + Plex forums, r/jellyfin + intro-skipper repos, Emby community forums, cross-server homelab patterns, GitHub feature-request issues) converged on a clear pattern: the **detection** problem is mostly solved (intro-skipper, Plex Pass, EmbyCredits). The **UX around manual control, cross-server portability, and the algorithms' blind spots** is universally complained about. This section captures the load-bearing UX features that make our app a category-creating tool rather than another detector clone.

### 7.5.1. Locked markers + per-show overrides (Gap #2 from community synthesis)

**The pain (5.75 years on the Plex forum):** Frugglehost (Nov 2023, Plex #593807) and intro-skipper #685 (DavidSpivey Feb 2026) both ask for the same thing — once the user has manually fixed a marker, **NEVER overwrite it on re-detection**. MarkerEditorForPlex documents this as its #1 known limitation. No tool in the ecosystem handles this cleanly.

**Our model:**

- `MarkerSegment.locked: bool` — if true, the marker is read-only to detection. The detection pipeline produces a parallel "candidate" segment instead of overwriting. The Inspector shows both with a "your edit" vs "current detection" diff.
- **Per-show settings dict** in our settings.json keyed by canonical show id (Plex `ratingKey`, Jellyfin/Emby item id, or our internal show-fingerprint):
  ```json
  "per_show_overrides": {
    "show:plex:42891": {
      "auto_skip_intro": "yes",    // yes | ask | no | inherit
      "auto_skip_credits": "ask",
      "intro_window_sec": 1200,    // extend past 10-min default — fixes Emby ceiling + Plex 595750 anime cold-open
      "anime_multi_op_mode": true, // see 7.5.4
      "disabled": false
    }
  }
  ```
- Plex literally **cannot** disable skip-intro per show (forum mod OttoKerner confirmed in thread #903175); our per-show toggle is unique.

### 7.5.2. Apply-pattern-to-season (Gap #1 — the 5.75-year-old killer feature)

**The pain (nurbles, Plex 619135, July 2020):** *"use manually marked intros as patterns for season-wide detection."* The user manually nudges S01E01's intro boundary to where it should be; the tool should use that audio segment as the **canonical fingerprint** when running chromaprint comparison across the rest of the season. **Nobody has shipped this** in 5+ years.

**Our implementation:**
- User adjusts intro boundary in Inspector → `MarkerSegment.seed = true` is set on save.
- Next time Tier 2 chromaprint runs on any episode in the same season, the seeded segment's audio (sliced from the source file) is loaded as the **anchor fingerprint** — every other episode is matched against this anchor rather than pairwise consensus.
- Confidence floor relaxes when matching against a seed (the user has vouched for the audio).
- One seed per type per season is the cap.
- Phase B detection job emits a `MARKERS_SEEDED` outcome distinct from `MARKERS_DETECTED`.

### 7.5.3. Per-client compatibility matrix in the Inspector

**The pain (community-confirmed, dozens of issues):** Users write perfect markers, only to discover their TV doesn't render the skip button. The truth-table from research:

| Client | Plex intro | Plex credits | Emby intro | Emby credits | Jellyfin segments |
|---|---|---|---|---|---|
| Web | ✅ | ✅ | ✅ | ✅ | ✅ |
| Android TV / Fire TV | ✅ | ✅ | ✅ | ✅ | ✅ (0.18+) |
| Android mobile | ✅ | ✅ | ✅ | ✅ | partial |
| iOS Plex / Emby | ✅ | ✅ | ✅ | ✅ | — |
| Swiftfin (iOS/tvOS Jellyfin) | — | — | — | — | ❌ (#1525 since Apr 2025) |
| Apple TV native | ✅ (8.39+, Sept 2024) | ✅ | ❌ | ❌ (Infuse 8.1.9 fills gap, June 2025) | — |
| Infuse (Apple TV) | ✅ | ✅ | ✅ | ✅ (8.1.9+) | ❌ (no Jellyfin server-plugin support) |
| LG webOS | ✅ | ✅ | ✅ (1.0.37+) | partial | ⚠️ broken button, auto-skip works (#272) |
| Samsung Tizen | ✅ | ✅ | ⚠️ inconsistent | ❌ | ❌ broken since 10.10 (#305) |
| Roku | ✅ | ✅ | ❌ | ❌ | ❌ |
| Plex for Windows | ❌ no auto-skip (#917472) | ❌ | n/a | n/a | n/a |
| Kodi (jellyfin-kodi) | ⚠️ multi-marker breaks #1859 | ⚠️ | n/a | n/a | ⚠️ EDL-only |
| Findroid (Jellyfin) | — | — | — | — | ❌ |
| Streamyfin (Jellyfin) | — | — | — | — | ❌ broken in 10.11 (#994) |
| Plex for Windows credits | ❌ | ❌ | — | — | — |

**Inspector UI:** for each marker we display the per-client truth-table with green/yellow/red dots so users know whether writing this marker will actually surface in their playback environment. **No other tool surfaces this** — users find out by trial and error.

We maintain a `client_compatibility.json` file shipped in the app, updated on each release as bugs ship/regress.

### 7.5.4. Multi-OP anime mode (intro-skipper dismissed; we ship it)

**The pain:** Anime cours commonly swap OP between episodes 12 and 13. Plex's "one fingerprint per season" model picks the first OP and silently stops detecting after the change (Plex 595750, LrrrAc Aug 2020). intro-skipper #658 (28 comments, dismissed) — the maintainer concluded "more harm than good." **Anime is the heaviest skip-intro user demographic, completely unserved.**

**Our model:**
- When `library.kind == "anime"` OR per-show override `anime_multi_op_mode: true`, Tier 2 chromaprint switches modes:
  - **Cluster pass:** after Pass 1 fingerprints are collected, cluster episodes by fingerprint similarity (KMeans-style, but on Hamming-distance graph: episodes with mutual fingerprint matches form a cluster).
  - **Per-cluster matching:** run pairwise consensus within each cluster separately. The biggest two clusters typically correspond to OP1 (early cour) and OP2 (late cour).
  - **Output:** N distinct fingerprints per season, each tagged with the episode range it applies to.
- Settings: `markers.anime_max_op_clusters: int = 4` (configurable per library).
- Tier 1c (anime-skip.com) hits first for AniList-mapped libraries.
- We also handle Bollywood song detection (intro-skipper #547, dismissed as "out of scope") via a separate `markers.detect_song_segments` flag — uses the same chromaprint clustering approach to find recurring high-tempo audio segments mid-episode. Off by default; opt-in for Indian-cinema libraries.

### 7.5.5. Cross-server marker bridge (Plex → Jellyfin/Emby; THE killer feature)

**The pain (HN user theossuary, Plex 937108 shared-user issue):** *"haven't been able to move over to Jellyfin after the drama around the integration of skip intro"* — markers are a migration blocker. Plex Pass holders' SHARED users (not Home users) can't see skip-intro even though the markers exist on the server. **No tool bridges markers between servers.**

**Our model:**
- `PlexMarkerPublisher.read_markers()` → produces a `MarkerSet` with `source: "native_plex"`.
- "Push to other servers" action in the Inspector: select any combination of enabled servers; the publisher writes the same set of markers via each server's normal path.
- This solves THREE problems at once:
  1. **Family/shared users on Plex** — admin's Plex Pass markers, pushed to a Jellyfin instance the family also uses, render correctly because Jellyfin doesn't entitlement-gate.
  2. **Migration from Plex to Jellyfin** — preserves years of marker fixes the user has accumulated.
  3. **Backup against Plex re-analyze** — markers stored in our sidecar + Jellyfin MediaSegments are wipe-resistant.

- "Pull from server" reverse action: read markers from a server, write to sidecar (no destination = use as backup).

### 7.5.6. EDL sidecar export (free Kodi support)

EDL format is the de facto interchange standard Kodi reads natively. Output: `<canonical_path>.edl` from our `MarkerSet`. Lines:
```
0.0   92.0   3   # intro (action 3 = scene marker)
2580.0 2700.0 3  # credits
```
Free Kodi compatibility, also gives users a portable record outside our app's DB. **Phase D feature, ~50 lines of code.**

### 7.5.7. Submit-back to MULTIPLE community DBs (TheIntroDB + SkipMe.db)

Two parallel crowd-sourced DBs exist. Both want submissions; the ecosystem isn't winner-take-all yet. Our submit-back flow pushes to both (when each user opts in with their respective API keys). **Doubles user contribution leverage** at almost no cost.

### 7.5.8. Cache sharding from day one

**The pain (user `ototos`, Jellyfin forum):** 20,000 chromaprint files for 2TB of TV; **projected 500,000 files for a 50TB library**. Flat directory = inode pressure = filesystem death.

**Our cache layout:**
```
${CONFIG_DIR}/markers_cache/
  fingerprints/
    a3/
      a3f9b27c.../{file_hash}.chromaprint.npy
    b1/
      ...
  theintrodb/
    {tmdb_id}_s{s}_e{e}.json
  introdb/
    ...
```
First 2 hex chars of canonical path's SHA1 as a subdirectory. Caps any single directory at ~256 files even at 65k entries. Defensively cap at 16 levels deep if needed.

### 7.5.9. Bulk-edit grid view (Phase D)

**The pain (Plex #593807, kamhouse 2020):** *"Spreadsheet-style interface with unlimited skip zones and auto-skip dropdown options."* Existing tools edit one episode at a time.

Inspector's bulk view (`/inspector?tab=markers&view=library`):
- One row per episode in a season.
- Columns: title, intro_start, intro_end, credits_start, credits_end, confidence, locked, status (✅ pushed / ⚠️ pending / ❌ error).
- Inline cell editing with frame-thumbnail preview on hover.
- "Bulk shift" action: select N rows, apply ±N seconds to a chosen field, dry-run preview, then apply.
- "Bulk lock" action: lock N rows from detection overwriting.

### 7.5.10. Webhook on segment events

intro-skipper #508 (closed). Jellyfin-plugin-webhook doesn't fire on segment writes. We emit our own webhook events:
- `markers.detected` — new markers written to sidecar
- `markers.published` — successfully pushed to ≥1 server
- `markers.publish_failed` — push to server failed (per-server detail in payload)
- `markers.user_edited` — user manually adjusted in Inspector

Compatible with existing Jellyfin/Plex webhook receivers (Notifiarr, Discord bots).

### Feature gaps we explicitly skip

Honest about what doesn't make the cut for v1:
- **Sonarr/Radarr OnImport custom-script hook** — natural fit but ~1 week of polish work; defer to Phase D++ once core stabilizes.
- **CLIP+Multihead transformer detector** (Korolkov 2025) — F1 91%, beats chromaprint, but no published weights. Future Phase F if we want to push past chromaprint's ceiling.
- **Mobile iOS marker editor** — community gap (Android-only segment-editor-mobile exists). Our Inspector is mobile-web-responsive but not native. Defer indefinitely.
- **MKV chapter-name write** — would give free Jellyfin chapter-segments-plugin support, but `CLAUDE.md` rule "media paths read-only" rules out file mutations on user's library. Document as alternative architecture for users who don't have that constraint.

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

Hook point: detection runs inside `Worker._process_item` (`jobs/worker.py:340`) **AFTER** the existing BIF phase returns its `ProcessingResult`. The new code calls a new module function `run_marker_detection_phase()` that orchestrates the tiers:

```python
# Inside Worker._process_item, after the existing BIF call:
def _run_marker_pipeline(item: ProcessableItem, config: Config, ...) -> list[ProcessingResult]:
    results = []

    if not config.markers.enabled or not (config.markers.detect_intros or config.markers.detect_credits):
        return [ProcessingResult.MARKERS_SKIPPED_DISABLED]

    # Detection cascade — early-exit when a tier returns sufficient-confidence markers
    markers, det_result = run_marker_detection_phase(item, config, fingerprint_store)
    results.append(det_result)

    # Publish phase — gated by per-server write_to_server flag, NOT skipped on bridged-marker mode
    if markers and any(s.write_to_server for s in registry.servers if config.markers_for(s).enabled):
        pub_results = run_marker_publish_phase(item, markers, config, registry)
        results.extend(pub_results)

    return results
```

**Detection sub-phases inside `run_marker_detection_phase()`** (run sequentially, gated by per-tier setting flags; accumulate evidence rather than always early-exit):

| Sub-phase | Tier | When skipped | Output |
|---|---|---|---|
| **2a — Chapter parse** | Tier 0 | `tier0_chapter_parsing == false` | List of `MarkerSegment(source="chapter")` |
| **2b — Cloud lookup** | Tier 1/1b/1c | `tier1_theintrodb == false` and `tier1b == false` and not anime+1c-enabled | List of `MarkerSegment(source="theintrodb"/"introdb"/"anime_skip")` |
| **2c — Subtitle extract** (Tier 2.5 prep) | — | `tier2_5_subtitle_recap == false` OR no English/native subs | Path to extracted `.srt` in /tmp, cleaned up at end of phase |
| **2d — Subtitle scan** | Tier 2.5 | Sub-phase 2c yielded no subs | `MarkerSegment(type="recap", source="subtitle_recap")` |
| **2e — Chromaprint pass 1** (per-episode) | Tier 2 | not TV OR `tier2_chromaprint == false` OR fewer than `season_min_episodes` in this season | Fingerprint deposited in season store; no segment yet |
| **2f — Adaptive blackdetect** | Tier 3 | not (movie OR Tier 2 missed credits for this item) | `MarkerSegment(type="credits", source="detected", detector="blackdetect_binsearch_v1")` |
| **2g — OCR fallback** | Tier 4 | `tier4_ocr == false` OR `paddleocr` module not importable | `MarkerSegment(type="credits", source="detected", detector="paddleocr_v1")` |

**Important:** Tier 2 (chromaprint) doesn't produce segments inside per-item processing — it just deposits the fingerprint. The post-dispatch pass-2 (cross-episode pairwise match) runs **after** all workers complete their pass-1 for a given season; that's the "season completion" hook in §5 Tier 2. Pass-2 results emit `MARKERS_DETECTED` against the affected episodes via a follow-up dispatcher job (a new low-priority job type `marker_post_detection`).

### Retry semantics

- **Detection failure within a single tier** — log + move to next tier. Never block the pipeline.
- **Whole detection-phase failure** (rare — e.g. corrupted pymediainfo parse) — surfaces as `MARKERS_DETECTION_FAILED` outcome. BIF was already successful so the item isn't fully failed.
- **Publish failure** — distinct from detection failure. Sidecar still exists; user can manually `Apply` from Inspector. No auto-retry — publisher errors are typically structural (plugin not installed, DB locked) and a blind retry won't help. Falls into the same `retry_reason` field the BIF pipeline already uses.
- **Locked markers** — re-detection still RUNS but produces a parallel "candidate" segment with `source="detected"` and `locked=false`; the existing locked segment is preserved. Inspector shows the diff.

### Cross-server bridge phase (separate from per-item pipeline)

The cross-server bridge described in §7.5.5 is **NOT** part of the per-item `_process_item` flow — it's a separate user-initiated action from the Inspector (and a scheduled background task in Phase D). Flow:

1. User clicks "Push to other servers" in Inspector for a single item OR triggers a "Sync from Plex → all enabled servers" library-wide task.
2. Background dispatcher creates a new job (`source="bridge"`). For each item:
   - `PlexMarkerPublisher.read_markers()` → `list[MarkerSegment]` with `source="native_plex"`.
   - For each target server: `target_publisher.write_markers(item_id, segments)`.
   - Bridged markers **bypass** the `min_confidence_to_write` gate because they came from a server's native detection (already vetted).
3. Failure semantics: each per-server publish wrapped in its own try/except so one server's failure doesn't block the others. Surfaces in job summary as per-server publish counts.

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
    "tier0_chapter_parsing": true,
    "tier1_theintrodb": true,
    "tier1b_introdb_fallback": false,
    "tier1c_anime_skip": true,
    "tier2_chromaprint": true,
    "tier3_blackdetect": true,
    "tier4_ocr": false,
    "theintrodb_api_key": null,
    "theintrodb_submit_back": false,
    "min_confidence_to_write": 0.7,
    "season_min_episodes": 2,
    "intro_window_sec": 600,
    "credits_window_sec": 600,
    "plex_direct_db_write": false,
    "plex_direct_db_write_confirmed_at": null,
    "use_release_group_hints": true
  }
}
```

**Global settings — top-level `markers` key in `settings.json`**, sibling of `gpu_config`, `media_servers`, etc. (settings.json has a flat root; no `settings.` namespace exists):

```json
{
  "media_servers": [ ... ],
  "gpu_config": [ ... ],
  "markers": {
    "theintrodb_base_url": "https://api.theintrodb.org/v2",
    "introdb_base_url": "https://api.introdb.app",
    "anime_skip_base_url": "https://api.aniskip.com/v2",
    "skipmedb_base_url": "https://db.skipme.workers.dev",
    "skipmedb_submit": false,
    "cache_dir": "/config/data/markers_cache",
    "cache_shard_depth": 2,
    "max_concurrent_detections": 2,
    "chapter_regex": {
      "intro": "(?i)\\b(intro|opening)\\b|^OP$",
      "credits": "(?i)\\b(outro|closing|credits|ending)\\b|^ED$",
      "recap": "(?i)\\b(recap|last time on|last on|previously on)\\b",
      "preview": "(?i)\\b(preview|next time on|next on|sneak peek)\\b"
    },
    "subtitle_recap_keywords": {
      "en": "(?i)\\b(previously on|last time on|last on|recap)\\b",
      "fr": "(?i)\\b(précédemment|au précédent|résumé)\\b",
      "de": "(?i)\\b(bisher|zuletzt bei|was bisher)\\b",
      "ja": "(これまでの|前回)",
      "ko": "(이전 이야기)",
      "es": "(?i)\\b(anteriormente en|en el episodio anterior)\\b"
    },
    "per_show_overrides": {},
    "client_compatibility_path": "/config/data/markers_cache/client_compatibility.json",
    "anime_max_op_clusters": 4,
    "edl_export": false,
    "webhook_events": ["markers.detected", "markers.published", "markers.publish_failed", "markers.user_edited"]
  }
}
```

The `chapter_regex` and `subtitle_recap_keywords` are user-overridable; chapter defaults mirror `jellyfin/jellyfin-plugin-chapter-segments/PluginConfiguration.cs` for cross-platform parity.

**`per_show_overrides`** is the killer feature key (see §7.5.1) — keyed by canonical show id (`"show:{server_id}:{vendor_item_id}"`), value is a dict of per-show flags:
```json
"per_show_overrides": {
  "show:plex-default:42891": {
    "auto_skip_intro": "yes",
    "auto_skip_credits": "ask",
    "intro_window_sec": 1200,
    "anime_multi_op_mode": true,
    "disabled": false,
    "locked_segments_count": 3
  }
}
```

### Migration

`upgrade.py` adds the `markers` dict to every existing server entry with `enabled: false`. Adds the global `markers` dict to root. Bumps schema version from v13 to v14. No existing settings touched.

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

### Phase A — Minimum read-only viewer (PR #1, ~1 week)

**Scope tightened after architecture review.** The previous Phase A draft was ~2-3 weeks of work labeled "1 week" (TheIntroDB + anime-skip + 3 publisher reads + sharded cache + 10 settings keys + Inspector). Phase A now ships **only the minimum that's useful on its own**; Tier 1b/1c, sidecar writes, cache sharding, and Emby/Jellyfin reads move to Phase B.

**Phase A deliverables:**
1. `markers/` package skeleton: `types.py` (`MarkerSegment`, `MarkerSet`), `detector.py` (interfaces only), `publisher.py` (interfaces only), `sidecar.py` (read/write helpers). ~½ day.
2. **Tier 0 `ChapterDetector`** — pymediainfo Menu-track parser at `generator.py:769` piggyback + regex defaults + mojibake-defensive parsing. ~½ day.
3. **Tier 1 TheIntroDB v2 client** (lookup only, no submit-back) — auth, per-user cache (positive forever, 404 14 days), circuit breaker after 5 consecutive 5xx. ~1.5 days.
4. **`external_ids(item_id)`** helper on each `MediaServer` subclass — Plex `guids`, Emby/Jellyfin `ProviderIds`. ~½ day.
5. **`PlexMarkerPublisher.read_markers()` only** — read existing markers via plexapi. Emby/Jellyfin read paths defer to Phase B (Plex has years of existing markers worth viewing; non-Plex value is less in Phase A).
6. Settings migration v13→v14 — adds **only Phase A keys**: `enabled`, `detect_intros`, `detect_credits`, `tier0_chapter_parsing`, `tier1_theintrodb`, `theintrodb_api_key`, `min_confidence_to_write`, `cache_dir`, `chapter_regex`. Other §10 keys land in their consuming phase to avoid hard-coding feature flags whose code hasn't shipped.
7. `MARKERS_DETECTED`, `MARKERS_DETECTED_EMPTY`, `MARKERS_SKIPPED_DISABLED` `ProcessingResult` outcomes (no `MARKERS_PUBLISHED` yet — no writes).
8. Standalone `/markers` Inspector page (NOT yet unified with BIF viewer) — search box, server picker, displays for a selected item: server-reported markers (Plex only in Phase A), Tier 0 chapter markers, Tier 1 TheIntroDB hits. **No** "apply to server" button yet, **no** manual edit yet, **no** waveform yet — those land in B/C/D.

**Deferred from earlier Phase A draft (moved to Phase B):**
- Tier 1c anime-skip.com client
- Tier 1b IntroDB.app fallback
- Sidecar JSON writes (no detection-of-our-own yet means no sidecars to write)
- Cache sharding directory layout (no significant cache volume yet at Phase A)
- Emby + Jellyfin `read_markers()`
- Audio waveform generation in Inspector

**Estimated:** 5-7 days. Useful on its own — gives users a Plex marker viewer + frame-accurate chapter-based detection + TheIntroDB community-DB enrichment.

### Phase A.5 — Sidecar + writes scaffolding (if Phase A took less than estimated)

Tier 1b/1c clients, Emby/Jellyfin `read_markers()`, sidecar JSON write helpers, cache sharding. ~½ week. Folded back into Phase B if Phase A runs long.

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

### Phase D — Polish + Inspector unification + optional OCR (PR #4)

- **Unified Inspector at `/inspector`** — port the standalone `/bif-viewer` (Frames) and `/markers` (Markers) pages into a single tabbed shell. Add 301 redirects from the old routes. Shared search/server-picker shell. ~2 days.
- PaddleOCR Tier 4 (gated behind setting, requires `:with-ocr` Docker tag)
- Bulk Inspector view (`/inspector?tab=markers&view=library`)
- TheIntroDB submit-back flow (per-user API key, opt-in toggle)
- Manual edit modal (drag handles + numeric input)
- Webhook integration polish (Sonarr/Radarr → run detection alongside BIF on new items)
- Documentation: setup guides for each server, troubleshooting, FAQ
- ChapterDB.org opportunistic lookup (Tier 3b — movies only, requires user API key, off by default)

**Estimated:** 1.5–2 weeks.

**Total estimate:** ~6–7 weeks of focused work.

---

## 13. Risks &amp; open questions

### Risks

1. **TheIntroDB sustainability — MED-risk single-maintainer.** `Pasithea0` is the sole org member on GitHub; no funding, no Patreon, no co-maintainer. Community side is healthy (295 contributors, 323,404 submissions, weekly releases) but operationally the API is one person's project. Mitigation:
   - Cache positive hits *forever* (accepted timestamps are immutable).
   - Cache 404s for 14 days only (DB grows).
   - Tier 0 + Tier 2 + Tier 3 stand on their own — if TheIntroDB disappears tomorrow, our cascade still works for free (chapters) + TV (chromaprint) + most movies (blackdetect).
   - Self-hosted caching proxy as an escape hatch for scale.
2. **TheIntroDB coverage is partial.** Live-verified hit rates: ~57% popular TV S1E1, ~35% popular movies — long tail much sparser. Tier 0 + Tier 2/3 are the primary coverage; TheIntroDB is opportunistic enrichment. **Movies hit Tier 0 (16%) + Tier 1 (35%) + Tier 3 (blackdetect, near 100% on titles with sustained-black credits) — combined coverage near 95% on mainstream movies.**
3. **Plex SQLite writes wiped by re-analysis.** Our provenance-restore loop (Phase C) handles this but isn't perfect. For users with Plex Pass, native is strictly better and we prefer the trigger-native path. For non-Pass users this is the only path. Mitigation:
   - Write to BOTH `taggings` AND `media_parts.extra_data` (the MarkerEditor pattern, not Casvt's incomplete pattern) so markers survive Plex's analyzer pass-through.
   - Side-car provenance DB with periodic restore loop.
   - Surface "this Plex install lacks Plex Pass, marker writes may be wiped on re-analyze" as a `HealthCheckIssue` in Setup Health.
4. **C# plugin maintenance burden.** We already ship `MediaPreviewBridge` for trickplay so the build toolchain (`dotnet 9.0`) is already in CI. Phase C extends it with `MarkerBridgeController` + `MarkerSegmentProvider` — net add ~250 lines C#. Mitigation: scope tightly (read sidecar → emit DTO + REST replace endpoint, nothing else). The plugin should rarely need changes.
5. **Chromaprint false matches on shows with shared theme music** (e.g., MCU shows, Marvel TV catalog). The `Duration - CreditsFingerprintStart - 1` anti-duplicate check handles same-file re-encodes but not different files with the same outro theme. Mitigation:
   - Confidence floor + ≥2 pairwise-agreement requirement before publishing.
   - Tier 0 wins when present — its 0.95 confidence beats Tier 2's 0.80 on agreement, so chapter-labeled shows are safe.
6. **GPU OCR Docker bloat.** PaddleOCR adds ~2GB. Mitigation (committed): ship via a separate `:with-ocr` Docker tag. Default image stays lean; OCR users opt in by image tag. The `markers.ocr_fallback` setting is runtime-detected so it only appears in the UI when `paddleocr` is importable.
7. **`media_parts.extra_data` JSON shape differs between PMS versions.** PMS ≥1.40 uses structured `{"pv:version":"5","url":...}`; older PMS uses URL-encoded-only form. Mitigation: detect PMS version on connection and emit the matching shape. Plex itself tolerates both forms on read, so a wrong choice at write time degrades cleanly (the marker exists but lacks the `final` flag).
8. **No file-hash-based marker lookup exists in 2026.** Director's cuts and theatrical-vs-extended editions all share the same TMDb ID, so TheIntroDB and IntroDB return the same timestamps for both — wrong for at least one of the two. Mitigation: detect duration mismatch (returned `credits.start_ms > file_duration_ms`) and reject; flag in Inspector as "marker length mismatch — possible alternative edition."
9. **Chapter-name regex false positives** on a tiny number of files with mojibake / shell-encoded chapter names. Mitigation: defensive `try/except` around regex match; log + skip on parse failure; never block the pipeline on a single bad chapter title.

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
