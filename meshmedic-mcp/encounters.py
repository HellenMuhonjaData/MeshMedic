"""Encounter identifiers.

From Phase 2, every recording, transcript, note and suggestion is filed
under an encounter. The ID is random and carries nothing about the patient
(no name, MRN, date of birth or date), so it is safe to keep in job
metadata and logs.
"""

import re
import uuid

_ENCOUNTER_ID = re.compile(r"^enc_[0-9a-f]{32}$")


def new_encounter_id() -> str:
    return f"enc_{uuid.uuid4().hex}"


def is_encounter_id(value: object) -> bool:
    return isinstance(value, str) and bool(_ENCOUNTER_ID.match(value))
