"""Tests for the per-output journal (smart dedup)."""

from __future__ import annotations

import json

from media_preview_generator.output.journal import (
    JOURNAL_SCHEMA_VERSION,
    _meta_path_for,
    clear_meta,
    outputs_fresh_for_source,
    write_meta,
)


class TestMetaPath:
    def test_meta_path_appends_meta_suffix(self, tmp_path):
        bif = tmp_path / "movie-320-10.bif"
        assert _meta_path_for(bif).name == "movie-320-10.bif.meta"

    def test_meta_path_handles_no_extension(self, tmp_path):
        f = tmp_path / "noext"
        assert _meta_path_for(f).name == "noext.meta"


class TestWriteMeta:
    def test_writes_one_meta_per_output(self, tmp_path):
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"x" * 1234)
        out_a = tmp_path / "a.bif"
        out_b = tmp_path / "b.bif"
        out_a.write_bytes(b"a")
        out_b.write_bytes(b"b")

        write_meta([out_a, out_b], str(source), publisher="emby_sidecar")

        meta_a = json.loads(_meta_path_for(out_a).read_text())
        meta_b = json.loads(_meta_path_for(out_b).read_text())
        assert meta_a["source_size"] == 1234
        assert meta_a["source_path"] == str(source)
        assert meta_a["publisher"] == "emby_sidecar"
        assert meta_a["schema"] == JOURNAL_SCHEMA_VERSION
        assert meta_b["source_size"] == 1234

    def test_silently_skips_when_source_missing(self, tmp_path):
        out = tmp_path / "a.bif"
        out.write_bytes(b"")
        write_meta([out], str(tmp_path / "ghost.mkv"))
        assert not _meta_path_for(out).exists()

    def test_failure_to_write_one_meta_does_not_block_others(self, tmp_path):
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"data")
        good = tmp_path / "good.bif"
        good.write_bytes(b"")

        # Make a path whose parent doesn't exist so .meta write fails.
        bad = tmp_path / "subdir-not-created" / "bad.bif"

        write_meta([good, bad], str(source))

        assert _meta_path_for(good).exists()
        assert not _meta_path_for(bad).exists()


class TestOutputsFreshForSource:
    def test_fresh_when_meta_matches(self, tmp_path):
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"x" * 100)
        out = tmp_path / "out.bif"
        out.write_bytes(b"")
        write_meta([out], str(source))

        assert outputs_fresh_for_source([out], str(source)) is True

    def test_stale_when_source_replaced(self, tmp_path):
        """Sonarr quality upgrade: file replaced in place → not fresh."""
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"x" * 100)
        out = tmp_path / "out.bif"
        out.write_bytes(b"")
        write_meta([out], str(source))

        # Replace source with different size + new mtime.
        source.write_bytes(b"y" * 99999)

        assert outputs_fresh_for_source([out], str(source)) is False

    def test_stale_when_source_grew(self, tmp_path):
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"x" * 100)
        out = tmp_path / "out.bif"
        out.write_bytes(b"")
        write_meta([out], str(source))

        with source.open("ab") as f:
            f.write(b"y" * 50)

        assert outputs_fresh_for_source([out], str(source)) is False

    def test_legacy_outputs_with_no_meta_treated_as_fresh(self, tmp_path):
        """Pre-journal outputs: outputs exist but no .meta sidecar.

        Upgrade migration: don't force regen on the first webhook after
        the journal feature ships. Stamp on the next publish so future
        calls go through the strict check.
        """
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"x" * 100)
        out = tmp_path / "out.bif"
        out.write_bytes(b"")
        # No write_meta call.
        assert outputs_fresh_for_source([out], str(source)) is True

    def test_not_fresh_when_output_missing(self, tmp_path):
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"x" * 100)
        out = tmp_path / "out.bif"
        # output never created
        assert outputs_fresh_for_source([out], str(source)) is False

    def test_not_fresh_when_source_missing(self, tmp_path):
        out = tmp_path / "out.bif"
        out.write_bytes(b"")
        assert outputs_fresh_for_source([out], str(tmp_path / "ghost.mkv")) is False

    def test_handles_corrupt_meta_as_legacy(self, tmp_path):
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"x" * 100)
        out = tmp_path / "out.bif"
        out.write_bytes(b"")
        _meta_path_for(out).write_text("not json {")
        # Corrupt meta is ignored entirely; behaves as legacy.
        assert outputs_fresh_for_source([out], str(source)) is True

    def test_one_match_is_enough_when_others_have_no_meta(self, tmp_path):
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"x" * 100)
        out_a = tmp_path / "a.bif"
        out_b = tmp_path / "b.bif"
        out_a.write_bytes(b"")
        out_b.write_bytes(b"")
        # Only out_a stamped.
        write_meta([out_a], str(source))
        assert outputs_fresh_for_source([out_a, out_b], str(source)) is True

    def test_mismatch_on_one_meta_invalidates_freshness(self, tmp_path):
        """If even one .meta says source changed, treat as stale.

        Conservative: avoids a corner case where a publisher updated
        once with an old source version, the source then changed, and
        another publisher hasn't yet run.
        """
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"x" * 100)
        out_a = tmp_path / "a.bif"
        out_b = tmp_path / "b.bif"
        out_a.write_bytes(b"")
        out_b.write_bytes(b"")
        write_meta([out_a, out_b], str(source))
        # Source replaced after stamping.
        source.write_bytes(b"y" * 200)
        assert outputs_fresh_for_source([out_a, out_b], str(source)) is False

    def test_not_fresh_when_no_outputs(self, tmp_path):
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"")
        assert outputs_fresh_for_source([], str(source)) is False

    def test_match_beats_mismatch_for_same_source(self, tmp_path):
        """Mutation-testing closer (journal.py:142 — `saw_match = True`).

        When ONE output's .meta matches and ANOTHER output's .meta mismatches
        for the same source, ``saw_match`` must short-circuit to ``True``
        (production policy: match wins). The inverse-only existing test
        ``test_mismatch_on_one_meta_invalidates_freshness`` uses an
        all-mismatches scenario and so does not exercise the asymmetric
        case. Without this test, mutating ``saw_match = True`` to
        ``saw_match = False`` survives because the only saw_match-True path
        is silently broken.
        """
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"x" * 100)
        out_a = tmp_path / "a.bif"
        out_b = tmp_path / "b.bif"
        out_a.write_bytes(b"")
        out_b.write_bytes(b"")
        # out_a: stamp matches the live source.
        write_meta([out_a], str(source))
        # out_b: hand-write a mismatching .meta with a clearly-wrong
        # fingerprint (same schema so the schema-guard at L139 is passed).
        _meta_path_for(out_b).write_text(
            json.dumps(
                {
                    "schema": JOURNAL_SCHEMA_VERSION,
                    "source_mtime": 1,
                    "source_size": 1,
                }
            )
        )

        # Match wins: True even though out_b records a stale fingerprint.
        assert outputs_fresh_for_source([out_a, out_b], str(source)) is True, (
            "Production policy: when one .meta matches and another mismatches for the SAME source, "
            "saw_match must short-circuit to True. A regression that flipped `saw_match = True` to `False` "
            "would let saw_mismatch dominate and return False here."
        )

    def test_outputs_with_old_schema_treated_as_legacy(self, tmp_path):
        """Mutation-testing closer (journal.py:38 — JOURNAL_SCHEMA_VERSION constant).

        The schema constant is read in two places — write_meta writes it,
        outputs_fresh_for_source compares it. Every existing test sets up
        data via write_meta, so writer and reader move in lockstep.
        Mutating the constant globally (e.g. ``1 → 2``) just shifts both
        ends symmetrically and the round-trip still works.

        Pin the *literal* schema number ``1`` in the .meta data so a
        regression that bumps the constant to 2 (without a real migration)
        falls into the schema-mismatch branch and is treated as legacy
        (returns True because the .meta is unreadable for freshness).
        """
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"x" * 100)
        out = tmp_path / "out.bif"
        out.write_bytes(b"")
        # Hand-write a .meta with literal schema=0 (old / unknown version).
        # Production: ``int(data.get("schema", 0)) != JOURNAL_SCHEMA_VERSION
        # → continue`` → no readable .meta → legacy fallback returns True.
        _meta_path_for(out).write_text(
            json.dumps(
                {
                    "schema": 0,  # literal — NOT JOURNAL_SCHEMA_VERSION
                    "source_mtime": int(source.stat().st_mtime),
                    "source_size": 100,
                }
            )
        )

        # The schema mismatch makes this .meta invisible to the freshness
        # check → falls through to the legacy branch → True.
        assert outputs_fresh_for_source([out], str(source)) is True, (
            "A .meta with schema != current must be ignored (treated as legacy). "
            "If the constant were silently bumped, write_meta would also bump and "
            "tests pass — pinning a literal schema=0 here catches the regression."
        )

    def test_meta_missing_size_field_treated_as_mismatch(self, tmp_path):
        """Mutation-testing closer (journal.py:141 — `data.get('source_size', -1)` default).

        A partially-valid JSON .meta missing the ``source_size`` key (e.g. a
        future schema upgrade or write-corruption that left a dangling
        record) must be treated as a fingerprint mismatch — i.e. the
        ``-1`` default value branch is the freshness-fail path. Production
        path:

            int(data.get("source_size", -1)) == src_size   # -1 != 100 → mismatch

        Without this test, mutations like ``-1 → 0`` survive because no
        existing test triggers a partial-key meta — every ``.meta`` written
        by ``write_meta`` always carries every key, so the default is dead
        code in the happy path.
        """
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"x" * 100)
        out = tmp_path / "out.bif"
        out.write_bytes(b"")
        # Hand-write a partial .meta — has schema + source_mtime, MISSING
        # source_size. The dict.get() default fires → -1 → mismatch.
        _meta_path_for(out).write_text(
            json.dumps(
                {
                    "schema": JOURNAL_SCHEMA_VERSION,
                    "source_mtime": int(source.stat().st_mtime),
                    # source_size intentionally absent
                }
            )
        )

        # The single .meta records a mismatch → saw_mismatch=True →
        # outputs_fresh_for_source returns False (NOT the legacy True
        # fallback, because at least one .meta WAS readable but mismatched).
        assert outputs_fresh_for_source([out], str(source)) is False, (
            "A .meta missing source_size must be treated as a mismatch (defaults to -1, "
            "compared against real source_size, so any non--1 size triggers the mismatch branch). "
            "A regression that changed the default to 0 would falsely match a 0-byte source."
        )


class TestClearMeta:
    def test_removes_existing_metas(self, tmp_path):
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"x" * 100)
        out = tmp_path / "out.bif"
        out.write_bytes(b"")
        write_meta([out], str(source))
        assert _meta_path_for(out).exists()

        clear_meta([out])
        assert not _meta_path_for(out).exists()
        # Output itself untouched.
        assert out.exists()

    def test_silent_on_missing_metas(self, tmp_path):
        out = tmp_path / "ghost.bif"
        # Never created.
        clear_meta([out])  # no exception
