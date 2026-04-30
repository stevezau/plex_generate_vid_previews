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
