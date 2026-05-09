"""Score candidate items against a parsed :class:`SearchQuery`.

The scorer's only job is to rank a handful of candidate names so the
best match floats to the top — Emby/Jellyfin's ``searchTerm`` returns
unranked substring hits, and Plex's ``searchHubs`` ranking is decent
but mixes types (Series + Movies + People). A small client-side rank
pass + a 0.3 floor turns the firehose into a usable result list.

Score scale (0.0 – 1.0):

* 1.0 — exact title match (case-insensitive).
* 0.8 — candidate name starts with the query title (the realistic
  ceiling for "the boys" → "The Boys").
* 0.5 — every query token appears somewhere in the candidate name.
* 0.2 — any single query token appears in the candidate.
* 0.0 — no overlap.

Bonuses (additive, capped at 1.0):

* +0.05 when the query carries a S##E## hint AND the candidate is a
  Series / Episode (so a Series hit beats a Movie hit when we know
  we're looking for an episode).
"""

from __future__ import annotations

from .query import SearchQuery

_RANK_FLOOR = 0.3


def rank_score(query: SearchQuery, candidate_name: str, candidate_type: str = "") -> float:
    """Return a 0.0–1.0 relevance score for ``candidate_name``.

    Args:
        query: Parsed :class:`SearchQuery`.
        candidate_name: The candidate item's display title.
        candidate_type: Lowercase vendor type string ("series", "movie",
            "episode", or empty if unknown). Used for the
            episode-context bonus.
    """
    if query.is_empty or not candidate_name:
        return 0.0

    name_lower = candidate_name.lower().strip()
    qtitle = query.title.strip()

    # Tier 1 — exact title match.
    if name_lower == qtitle:
        score = 1.0
    elif name_lower.startswith(qtitle):
        # Tier 2 — prefix match. Realistic top hit for "the boys" →
        # "The Boys" (1.0) vs "The Boys' Life" (0.8 since it starts
        # with the query but has trailing chars).
        score = 0.8 if name_lower != qtitle else 1.0
    else:
        # Tier 3/4 — token overlap.
        tokens_present = sum(1 for t in query.tokens if t in name_lower)
        if tokens_present == 0:
            return 0.0
        if tokens_present == len(query.tokens):
            score = 0.5
        else:
            score = 0.2

    # Episode-context handling: if the user typed S##E##, prefer
    # Series/Episode candidates over Movies. Movies that share a name
    # with a series ("The Boys" the show vs "The Boys" the 2008 movie)
    # would otherwise tie at 1.0 and pick the wrong row at random.
    # Penalise wrong-type matches multiplicatively rather than adding a
    # bonus to right-type matches — additive bonuses get clamped at the
    # 1.0 ceiling, so they couldn't differentiate two perfect matches.
    ctype = (candidate_type or "").lower().strip()
    if query.has_episode and ctype in ("movie", "film"):
        score *= 0.5

    return score


def filter_and_rank(
    query: SearchQuery,
    candidates: list[tuple[str, str, object]],
    *,
    floor: float = _RANK_FLOOR,
    limit: int = 50,
) -> list[object]:
    """Rank ``candidates`` and return the carriers above ``floor``.

    Args:
        query: Parsed :class:`SearchQuery`.
        candidates: Iterable of ``(name, type, carrier)`` tuples. The
            scorer only needs ``name`` + ``type``; ``carrier`` is the
            opaque object the caller wants back (a MediaItem, a raw
            vendor dict, whatever).
        floor: Drop candidates below this score. Default 0.3 keeps the
            "all tokens present" tier and above; rejects the noisy
            "any single token" hits like Wonder Boys for "the boys".
        limit: Cap the result list.

    Returns:
        Carriers in descending score order.
    """
    scored: list[tuple[float, object]] = []
    for name, ctype, carrier in candidates:
        score = rank_score(query, name, ctype)
        if score >= floor:
            scored.append((score, carrier))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [carrier for _, carrier in scored[:limit]]
