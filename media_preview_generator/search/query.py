"""Parse Preview Inspector search strings into normalised query objects.

The user types things like:

* ``"the boys s01e01"`` — show name + season/episode hint
* ``"the.boys.s01e01.1080p.web.h264-rarbg"`` — release-style filename paste
* ``"the boys 1x01"`` — alternate season/episode notation
* ``"wonder boys"`` — movie title only

Pre-fix, vendors received the raw string and either:

* substring-matched the whole thing (Emby returned every "Boys" item
  for "the boys s01e01"), or
* tried to look up a literal title that had release tags glued to it
  (Plex's ``library.search(title="the.boys.s01e01.1080p.…")`` scored
  zero hits).

:class:`SearchQuery` does the parsing once. Every vendor adapter sees
``query.title``, ``query.season``, ``query.episode``, and
``query.tokens`` instead of the raw mess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Matches S01E01, s1e1, S01E001, S00E00, etc. Captures season + episode.
_SEASON_EPISODE_RE = re.compile(r"\b[Ss](\d{1,3})[Ee](\d{1,4})\b")
# Matches 1x01, 12x345 — alternate notation common in older release names.
_ALT_SEASON_EPISODE_RE = re.compile(r"\b(\d{1,3})[xX](\d{1,4})\b")
# Release-style separators that should be treated as spaces when
# cleaning the title (so "the.boys" → "the boys").
_TITLE_SEPARATORS = re.compile(r"[._]+")
# Junk tokens the user typically leaves at the end of a release-style
# filename paste — strip them once the title is isolated. This list is
# intentionally conservative; we drop ONLY the ones we're certain are
# release metadata, not English words that might appear in a real title.
_JUNK_TOKENS = frozenset(
    {
        "1080p",
        "2160p",
        "720p",
        "480p",
        "web",
        "webrip",
        "webdl",
        "web-dl",
        "bluray",
        "blu-ray",
        "bdrip",
        "brrip",
        "hdrip",
        "hdtv",
        "dvdrip",
        "x264",
        "x265",
        "h264",
        "h265",
        "h.264",
        "h.265",
        "hevc",
        "avc",
        "aac",
        "ac3",
        "dts",
        "eac3",
        "atmos",
        "ddp5",
        "ddp7",
        "remux",
        "proper",
        "repack",
        "extended",
        "uncut",
        "rarbg",
        "yify",
        "yts",
        "ettv",
        "eztv",
        "10bit",
        "8bit",
        "hdr",
        "hdr10",
        "dv",
        "sdr",
        "5.1",
        "7.1",
        "2.0",
    }
)


@dataclass(frozen=True)
class SearchQuery:
    """Normalised representation of a Preview Inspector search string.

    Attributes:
        raw: The input string verbatim (for logging/echo).
        title: Cleaned title — separators normalised to spaces,
            release-junk tokens stripped, lowercased. e.g. for
            ``"the.boys.s01e01.1080p.web.h264-rarbg"`` this is
            ``"the boys"``.
        season: Season number if a S##E## or NxN pattern was present.
        episode: Episode number if a S##E## or NxN pattern was present.
        tokens: Tuple of lowercase title tokens for ranking. e.g.
            ``("the", "boys")``.
    """

    raw: str
    title: str
    season: int | None
    episode: int | None
    tokens: tuple[str, ...]

    @classmethod
    def parse(cls, text: str | None) -> SearchQuery:
        """Parse ``text`` into a :class:`SearchQuery`.

        Empty / whitespace-only input yields a query with empty
        ``title`` and ``tokens`` — callers should treat that as "no
        search to run" and return early.
        """
        raw = text or ""
        # Normalise separators FIRST so the regex picks up "S01E01"
        # whether it arrived as "the.boys.s01e01" or "the boys s01e01".
        normalised = _TITLE_SEPARATORS.sub(" ", raw).strip()

        season: int | None = None
        episode: int | None = None
        # Try canonical S##E## notation first; fall back to NxN.
        m = _SEASON_EPISODE_RE.search(normalised)
        if not m:
            m = _ALT_SEASON_EPISODE_RE.search(normalised)
        if m:
            try:
                season = int(m.group(1))
                episode = int(m.group(2))
            except (ValueError, IndexError):
                season = None
                episode = None
            # Strip the season/episode marker out of the title so the
            # downstream search isn't passed "the boys s01e01" as a
            # literal title — Plex's library.search would never match it.
            normalised = (normalised[: m.start()] + normalised[m.end() :]).strip()

        # Now drop release-junk tokens at the END of the string. We
        # don't strip them anywhere — "Boys" is a perfectly valid title
        # token, but "1080p" and "rarbg" are not.
        # Also pre-split tokens on "-" because release-group suffixes
        # commonly arrive glued to a codec ("h264-rarbg", "x265-NTb").
        # Without the split, the junk-detector sees "h264-rarbg" as a
        # single non-junk token and stops, leaving the entire codec +
        # release-group tail in the title.
        raw_tokens = normalised.split()
        words: list[str] = []
        for tok in raw_tokens:
            words.extend(p for p in tok.split("-") if p)
        # Strip from the right edge: as soon as we hit a token that
        # ISN'T in the junk list, stop. Preserves real titles like
        # "Boys State" while killing ".1080p.web.h264-rarbg" tails.
        while words and _is_junk(words[-1]):
            words.pop()

        cleaned = " ".join(words).lower().strip()
        # Normalise whitespace again in case stripping junk left a
        # double-space.
        cleaned = re.sub(r"\s+", " ", cleaned)

        return cls(
            raw=raw,
            title=cleaned,
            season=season,
            episode=episode,
            tokens=tuple(t for t in cleaned.split(" ") if t),
        )

    @property
    def has_episode(self) -> bool:
        """True when the user expressed an interest in a specific episode."""
        return self.season is not None and self.episode is not None

    @property
    def is_empty(self) -> bool:
        """True when there's nothing meaningful to search for."""
        return not self.title and not self.tokens


def _is_junk(token: str) -> bool:
    """Return True if ``token`` looks like release metadata (not a real title word).

    The caller pre-splits on hyphens so a release-group tail like
    ``h264-rarbg`` arrives as two separate tokens (``h264`` + ``rarbg``).
    Both need to register as junk for the right-edge stripper to peel
    them off.

    For unknown tokens we apply a conservative heuristic: lone uppercase
    release-group acronyms (``RARBG``, ``YIFY``, ``CAKES``, ``NTb``,
    ``EDITH``) are treated as junk ONLY when called from the right-edge
    loop. We can't reliably tell them apart from real proper nouns in a
    title, so we lean on three signals:
      * length 3-12 (release groups don't have spaces)
      * mostly-alphabetic
      * mostly-uppercase OR starts with uppercase letter
    Combined with the right-edge-only application this rarely fires on
    real titles ("STATE", "BOYS" wouldn't be stripped because they sit
    AFTER a known good token like "Boys" or "State").
    """
    t = token.lower().strip("[]()")
    if not t:
        return True
    # Direct match against the curated junk list.
    if t in _JUNK_TOKENS:
        return True
    # Release-group suffix pattern. The caller pre-split the original
    # token on hyphens, so by the time we see ``rarbg`` it stands alone.
    # Heuristic: 3-12 chars, mostly alphabetic, original (pre-lower)
    # token was mostly uppercase. Catches RARBG/YIFY/EDITH/NTb without
    # eating real title words like "boys" or "the".
    raw = token.strip("-[]()")
    if 3 <= len(raw) <= 12 and raw.isalpha():
        upper_ratio = sum(1 for c in raw if c.isupper()) / len(raw)
        if upper_ratio >= 0.5:
            return True
    return False
