# Vendor-API Contract Cassettes

Recorded HTTP request/response pairs for Plex, Emby, and Jellyfin API
boundary functions. Captured once with [pytest-recording] / [vcrpy]
against a live server, replayed forever in CI.

## Why these exist

The production "Plex 500s when `type=` is omitted" bug shipped because the
unit test mocked `plex.fetchItems` and asserted on a substring of the URL
— it didn't catch the missing required parameter because the mock
returned items regardless. Cassettes capture the exact API contract
(URL shape, headers, status codes, response shape) so any future change
that strays from the recorded shape fails fast.

## Layout

```
tests/cassettes/
├── README.md                          # this file
├── test_servers_plex_vcr/             # one dir per test module
│   ├── test_resolve_one_path_movie.yaml
│   ├── test_resolve_one_path_episode.yaml
│   └── test_get_bundle_metadata.yaml
├── test_servers_emby_vcr/
│   └── …
└── test_servers_jellyfin_vcr/
    └── …
```

## Running

Replay (default, no live server needed):

```
pytest tests/test_servers_plex_vcr.py
```

Re-record (requires live server + creds in env):

```
PLEX_URL=https://plex.local:32400 \
PLEX_TOKEN=xxx \
pytest tests/test_servers_plex_vcr.py --record-mode=once
```

Other useful modes: `--record-mode=new_episodes` (record only missing
interactions, keep existing), `--record-mode=all` (overwrite everything).

## Scrubbing

Sensitive data is scrubbed at record time via the `vcr_config` fixture in
`tests/conftest.py`. Specifically:

- `X-Plex-Token`, `X-Emby-Token`, `Authorization`, `Cookie`, `Set-Cookie`
  headers → replaced with `FAKE_*` placeholders.
- `X-Plex-Token`, `api_key` query parameters → replaced.

If you add a new vendor or auth mechanism, **extend the `filter_headers`
and `filter_query_parameters` lists in `conftest.py` BEFORE recording**.
A leaked token in a committed cassette is a credentials disclosure.

## Don't commit cassettes that contain

- Real auth tokens (verify by `grep -E '(X-Plex-Token|X-Emby-Token):'
  tests/cassettes/**` — should only show `FAKE_*` strings).
- User-identifying paths if they reveal real media organisation. (Mostly
  fine for media metadata; concern is more around server identifiers.)

## Re-recording when a vendor API changes

1. Delete the affected cassette file(s).
2. Run the test with `--record-mode=once` against a live server.
3. Verify the re-recorded YAML — should look similar in structure to the
   old one. Big differences (new headers, response-shape changes) are
   the API drift the cassette is designed to catch.
4. Commit the new cassette and the test changes together.

[pytest-recording]: https://pytest-recording.readthedocs.io/
[vcrpy]: https://vcrpy.readthedocs.io/
