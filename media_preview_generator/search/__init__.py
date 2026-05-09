"""Shared search abstraction for the Preview Inspector.

Pre-fix every vendor's ``search_items()`` did its own thing, with three
distinct broken behaviours observed in production 2026-05-10:

* **Plex** returned 0 results because ``library.search()`` filters by
  the user's enabled-library config; multi-server installs that pin
  ``plex_library_ids = []`` got nothing back across the entire library.
* **Emby** returned junk for ``"the boys s01e01"`` — the bare
  ``searchTerm`` substring matcher ranks every item containing the
  token "boys" equally (Wonder Boys, Nickel Boys, Jersey Boys, Bad
  Boys, Boys State, Good Boys), with no relevance ordering.
* **Jellyfin** returned 0 results for the same query (same code path
  as Emby but plugin/scope issues silently swallowed).

The fix lives here:

* :class:`~media_preview_generator.search.query.SearchQuery` parses the
  raw input once — extracts the show/movie title, season number, and
  episode number — so every vendor adapter sees the same normalised
  shape.
* :func:`~media_preview_generator.search.rank.rank_score` ranks
  candidate names against the parsed query so a good Emby match (The
  Boys) beats a coincidental token hit (Wonder Boys).
* Per-vendor ``search_items()`` overrides build on top: Plex via
  ``searchHubs()`` (cross-library), Emby/Jellyfin via two-pass
  ``NameStartsWith`` then ``searchTerm``-with-rank fallback.
"""

from .query import SearchQuery
from .rank import rank_score

__all__ = ["SearchQuery", "rank_score"]
