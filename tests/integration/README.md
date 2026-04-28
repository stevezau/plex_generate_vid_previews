# Integration test stack

End-to-end test environment for the multi-media-server refactor. Brings up
Emby, Jellyfin, and Plex against a shared synthetic media library so the
dispatcher's path-centric fan-out can be exercised against real servers.

This is **isolated from the maintainer's production Plex** — different
host ports (Emby 8096, Jellyfin 8097, Plex 32401), separate config
volumes, synthetic media generated locally.

## One-time setup

1. Generate synthetic test media:

   ```bash
   ./tests/integration/generate_test_media.sh
   ```

   Produces deterministic 30-second `.mkv` files (H.264 and HEVC) under
   `tests/integration/media/`, in Plex's expected library layout.

2. Get a Plex claim token from <https://plex.tv/claim> (4-minute validity).

3. Bring the stack up:

   ```bash
   PLEX_CLAIM=claim-XXXXXXXX docker compose \
       -f tests/integration/docker-compose.test.yml \
       up -d
   ```

4. Configure the servers via API:

   ```bash
   python tests/integration/setup_servers.py
   ```

   Writes `servers.env` next to the script with captured tokens.

## Running the integration suite

```bash
pytest -m integration --no-cov tests/integration/
```

The integration tests are excluded from the default `pytest` run (which
covers the fast unit suite) — they only fire when the marker is selected.

## Tear-down

```bash
docker compose -f tests/integration/docker-compose.test.yml down -v
```

The `-v` flag wipes the named volumes too; otherwise re-running the stack
will reuse the captured admin users / tokens from the previous run, which
is usually what you want during iteration.

## Phase status

| Phase | What lives here |
|---|---|
| Phase 1 | docker-compose, media generator, and `setup_servers.py` scaffold. The per-vendor setup helpers raise `NotImplementedError` until their clients land in Phases 2/3. |
| Phase 2 | `setup_emby()` filled in (POST `/Users/New` + `/Users/AuthenticateByName`); first integration scenarios for Plex + Emby. |
| Phase 3 | `setup_jellyfin()` filled in (`/Startup/*` wizard automation); Jellyfin scenarios; cross-vendor fan-out tests. |
| Phase 4 | Frame-cache assertions (FFmpeg ran exactly once for N publishers). |
