"""Transfer-pair exclusion interface for the engine.

The engine is purely a math library — it doesn't query a database.
The caller (CrowTax server, dashboard tools, third-party consumers)
loads transfer-pair data from wherever it stores it, builds an
``ExclusionSet``, and passes it to whichever entrypoint walks events.
Events whose ids appear in the set are skipped during disposal
computation.

For the v0.4.3 release the engine surface is just the data class and
a tiny membership predicate; the engine's existing pipeline today is
DB-driven (``staging.promote_confirmed`` -> ``engine.rematch_all``)
and the server enforces exclusion at the translate-to-staging boundary.
A future release can wire the predicate into the staging promotion
filter directly so engine consumers that don't go through the server
get the same behavior.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExclusionSet:
    """Event ids to skip during disposal computation.

    Both halves of an internal-transfer pair are tracked so callers
    can answer "is this event excluded?" without knowing which leg
    they're holding.

    Attributes:
        out_event_ids: out-legs of confirmed internal transfers.
        in_event_ids: in-legs of confirmed internal transfers.
    """

    out_event_ids: frozenset[str]
    in_event_ids: frozenset[str]


def is_excluded_event(event_id: str, exclusions: ExclusionSet) -> bool:
    """True iff ``event_id`` appears in either leg of the exclusion set."""
    return (
        event_id in exclusions.out_event_ids
        or event_id in exclusions.in_event_ids
    )
