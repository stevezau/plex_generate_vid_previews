# Integration test stack

End-to-end test environment for the multi-media-server refactor. Brings up
Emby, Jellyfin, and Plex against a shared synthetic media library so the
dispatcher's path-centric fan-out can be exercised against real servers.

This is **isolated from the maintainer's production Plex** — different
host ports (Emby 8096, Jellyfin 8097, Plex 32401), separate config
volumes, synthetic media generated locally.

## TL;DR — the four scripts

```
./tests/integration/up.sh                # bring stack up (idempotent)
./tests/integration/record-cassettes.sh  # re-record VCR cassettes
./tests/integration/down.sh              # stop, KEEP volumes
./tests/integration/wipe.sh              # stop, WIPE volumes (full reset)
```

`up.sh` is safe to re-run any time. It generates media if missing,
brings up containers, waits for health, and captures credentials into
`servers.env`. Plex's claim token is consumed once on first start and
persisted in the `plex_config` volume — subsequent `./up.sh` runs need
no claim.

## First-time setup

1. Get a Plex claim token from <https://plex.tv/claim> (4-min validity).

2. Bring the stack up:

   ```bash
   PLEX_CLAIM=claim-XXXXXXXX ./tests/integration/up.sh
   ```

   This runs `generate_test_media.sh`, brings up the three containers,
   waits for health, then runs `setup_servers.py` to capture
   credentials. Output: `tests/integration/servers.env`.

3. The stack is now ready for cassette recording or live integration tests.

## Subsequent runs (no claim needed)

```bash
./tests/integration/up.sh
```

The persisted `plex_config` volume keeps the admin token across
restarts. Same for Emby's auto-seeded admin and Jellyfin's injected API
key.

## Recording cassettes

```bash
./tests/integration/record-cassettes.sh           # only missing
./tests/integration/record-cassettes.sh --clean   # drop all + re-record
```

Reads `servers.env`, exports the env vars the cassette tests expect,
runs `pytest --record-mode=once` against the three `test_servers_*_vcr`
modules. Cassettes are scrubbed at record time (see
`tests/conftest.py::_scrub_request_uri` / `_scrub_response_body`) and
land under `tests/cassettes/`.

After recording, verify no auth tokens leaked:

```bash
grep -rE '(X-Plex-Token|X-Emby-Token):' tests/cassettes | grep -v FAKE_ || echo OK
```

Then commit the cassettes alongside the test changes that needed them.

## Running the integration suite

```bash
pytest -m integration --no-cov tests/integration/
```

The integration tests are excluded from the default `pytest` run
(which covers the fast unit suite) — they only fire when the marker is
selected.

## Tear-down

```bash
./tests/integration/down.sh   # stops containers, KEEPS volumes (next ./up.sh is fast)
./tests/integration/wipe.sh   # stops containers AND wipes volumes (next ./up.sh needs PLEX_CLAIM)
```

Use `wipe.sh` only when you want a clean slate (testing the bootstrap
flow itself, or recovering from a corrupt volume). Otherwise prefer
`down.sh` — the captured tokens persist so iteration is fast.

## Phase status

| Phase | What lives here |
|---|---|
| Phase 1 | docker-compose, media generator, and `setup_servers.py` scaffold. |
| Phase 2 | `setup_emby()` filled in; first integration scenarios for Plex + Emby. |
| Phase 3 | `setup_jellyfin_via_api_key_injection()` (sqlite injection workaround for the upstream wizard bug); Jellyfin scenarios; cross-vendor fan-out tests. |
| Phase 4 | Frame-cache assertions (FFmpeg ran exactly once for N publishers). |
