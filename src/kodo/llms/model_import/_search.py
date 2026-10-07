"""Rank Hugging Face GGUF search hits for the "add a local LLM" search box.

A Hub search sorts purely by downloads, which puts fine-tunes and merges next
to the original repos. The settings dialog wants the opposite emphasis, so
hits are split into three publisher tiers and shown tier by tier, each tier
sorted by downloads:

- **top** — the labs that train the models and publish their own GGUFs, plus
  ``ggml-org`` (llama.cpp's own conversions);
- **known** — established quantizers that republish many labs' models;
- **other** — everyone else.

Publishers are matched on the repo id's owner, ignoring case.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ._hub import RepoSearchHit

__all__ = [
    "HF_KNOWN_PUBLISHERS",
    "HF_TOP_PUBLISHERS",
    "SEARCH_MIN_QUERY_LENGTH",
    "RankedSearchHit",
    "rank_search_hits",
]

#: Original authors and big contributors: ranked first.
HF_TOP_PUBLISHERS: frozenset[str] = frozenset(
    {
        "Qwen",
        "ornith-ai",
        "ggml-org",
        "google",
        "mistralai",
        "microsoft",
        "ibm-granite",
        "LiquidAI",
        "nvidia",
        "allenai",
        "HuggingFaceTB",
        "tiiuae",
        "zai-org",
        "poolside",
        "NousResearch",
    }
)

#: Established quantizers: ranked after the top publishers.
HF_KNOWN_PUBLISHERS: frozenset[str] = frozenset(
    {
        "unsloth",
        "bartowski",
        "huihui-ai",
        "lmstudio-community",
        "mradermacher",
        "MaziyarPanahi",
        "QuantFactory",
        "second-state",
    }
)

#: Shorter queries match too much of the Hub to be useful and are not sent.
SEARCH_MIN_QUERY_LENGTH = 2

_TIERS = ("top", "known", "other")
_TOP = frozenset(name.lower() for name in HF_TOP_PUBLISHERS)
_KNOWN = frozenset(name.lower() for name in HF_KNOWN_PUBLISHERS)


@dataclass(frozen=True)
class RankedSearchHit:
    """A search hit with its publisher tier.

    Attributes:
        hit: The hit as the Hub returned it.
        author: The repo id's owner.
        tier: ``"top"``, ``"known"`` or ``"other"``.
    """

    hit: RepoSearchHit
    author: str
    tier: str


def _tier(author: str) -> str:
    folded = author.lower()
    if folded in _TOP:
        return "top"
    if folded in _KNOWN:
        return "known"
    return "other"


def rank_search_hits(hits: Iterable[RepoSearchHit], limit: int) -> tuple[RankedSearchHit, ...]:
    """Order hits top publishers first, then known publishers, then the rest.

    Within a tier, hits are sorted by downloads (descending), ties by repo id.

    Args:
        hits (Iterable[RepoSearchHit]): Hits in any order.
        limit (int): Most hits to return; the cut is made after ranking, so a
            top-tier hit outranks any number of more downloaded others.

    Returns:
        tuple[RankedSearchHit, ...]: At most *limit* ranked hits.
    """
    ranked = [
        RankedSearchHit(hit=hit, author=author, tier=_tier(author))
        for hit in hits
        for author in (hit.repo_id.split("/", 1)[0],)
    ]
    ranked.sort(key=lambda r: (_TIERS.index(r.tier), -r.hit.downloads, r.hit.repo_id.lower()))
    return tuple(ranked[:limit])
