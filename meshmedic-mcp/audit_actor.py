"""Who performed an audited action.

Every audit entry written from Phase 0 on carries an `actor`:

- {"type": "ai"}         -- content the AI produced (drafts, suggestions)
- {"type": "system"}     -- MeshMedic itself (background jobs, retention purges)
- {"type": "clinician", "clinician_id": ...} -- an authenticated clinician (Phase 1)
- {"type": "unverified"} -- a person acted, but no authenticated identity exists
                            yet (every clinician decision before Phase 1)

Entries written before this field existed have no actor; entry_actor()
reports them as {"type": "legacy"}. Old entries are never rewritten.
"""

from typing import Literal

ActorType = Literal["ai", "system", "clinician", "unverified", "legacy"]

AI = {"type": "ai"}
SYSTEM = {"type": "system"}
UNVERIFIED = {"type": "unverified"}
LEGACY = {"type": "legacy"}


def clinician(clinician_id: str) -> dict:
    if not clinician_id:
        raise ValueError("clinician_id is required for a clinician actor")
    return {"type": "clinician", "clinician_id": clinician_id}


def for_source(source: str) -> dict:
    """Actor for content recorded with a source flag ("ai" or "clinician").

    Clinician-entered content stays "unverified" until Phase 1 adds
    authenticated identity -- the server cannot prove who typed it.
    """
    return dict(AI) if source == "ai" else dict(UNVERIFIED)


def entry_actor(entry: dict) -> dict:
    """The actor of an audit entry; entries from before Phase 0 are legacy."""
    actor = entry.get("actor")
    return actor if isinstance(actor, dict) and actor.get("type") else dict(LEGACY)
