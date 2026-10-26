"""
Referral-letter logic for STORY-011 / REQ-004: prepare follow-up
documentation from a completed encounter for clinician review.

Pure functions only -- no file I/O, no logging, no MCP wiring. server.py owns
those (its generate_referral_letter / approve_referral_letter tools call into
this module), which keeps this file testable in isolation and keeps
server.py's audit trail the single source of truth: every function here
takes the already-read audit entries as input and reconstructs letter state
by replaying them, exactly like server.py's _find_note/_find_order.

Lifecycle: draft -> ready_to_send (clinician approval) -> sent (clinician
records how they delivered it). Nothing here delivers anything: "sent"
records a delivery the clinician made outside this system (printed and
handed over, faxed, mailed...), so the audit trail shows the letter left.
A draft or ready_to_send letter can instead be voided (e.g. wrong patient):
it is withdrawn for good, with who/when/why recorded. A sent letter can't
be voided -- it has already left. Voiding never deletes the audit history.

Failure modes handled (raised as MissingEncounterData for the caller to
surface): the note was never generated, was never approved (an unreviewed
draft is not a completed encounter), was rejected, or its patient can't be
resolved. Not handled here: audit-write failures -- server.py's
_append_audit_entry raises ToolError for those, and since state is replayed
from the log, a failed write leaves the letter in its previous state.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field

GENERATE_ACTION = "generate_referral_letter"
APPROVE_ACTION = "approve_referral_letter"
SEND_ACTION = "mark_referral_letter_sent"
VOID_ACTION = "void_referral_letter"

MAX_LETTER_CHARS = 10000

LetterStatus = Literal["draft", "ready_to_send", "sent", "voided"]

# How the clinician got the letter out. This server never delivers a letter
# itself -- marking it sent records a delivery that happened outside it.
SendMethod = Literal[
    "handed_to_patient", "mail", "fax", "email", "patient_portal", "other"
]


class MissingEncounterData(Exception):
    """The encounter a letter would be built from is absent or incomplete."""


class ReferralLetter(BaseModel):
    letter_id: str
    note_id: str
    patient_id: str
    ehr_system: str
    status: LetterStatus
    specialty: str
    letter_text: str
    generated_at: str
    approved_at: str | None
    approved_by: str | None
    sent_at: str | None = None
    sent_by: str | None = None
    send_method: SendMethod | None = None
    voided_at: str | None = None
    voided_by: str | None = None
    void_reason: str | None = None


class ReferralLetterList(BaseModel):
    letters: list[ReferralLetter]
    count: int
    listed_at: str


SpecialtyField = Annotated[str, Field(min_length=1, max_length=100)]
ReasonField = Annotated[str, Field(min_length=1, max_length=1000)]
ClinicianField = Annotated[str, Field(min_length=1, max_length=200)]
VoidReasonField = Annotated[str, Field(min_length=1, max_length=500)]


def completed_encounter(note_record: dict | None, note_id: str) -> dict:
    """The approve_note decision entry for `note_id`, which carries the
    final, clinician-approved note text. Raises MissingEncounterData if the
    encounter isn't complete. `note_record` is server._find_note's shape."""
    if note_record is None:
        raise MissingEncounterData(
            f"No encounter note found for note_id={note_id!r}; a referral letter "
            "needs a generated and approved encounter note."
        )
    decision = note_record["decision"]
    if decision is None:
        raise MissingEncounterData(
            f"note_id={note_id!r} has not been approved yet; approve the encounter "
            "note before preparing follow-up documentation."
        )
    if decision["action"] != "approve_note":
        raise MissingEncounterData(
            f"note_id={note_id!r} was rejected; a rejected note cannot be the basis "
            "of a referral letter."
        )
    return decision


def render_letter(
    patient: dict,
    approved_note: dict,
    specialty: str,
    referral_reason: str,
    referring_clinician: str,
) -> str:
    """Deterministic letter text: same inputs always give the same letter.
    Dated from the note's approval timestamp, not the clock, for that reason."""
    letter_date = approved_note["timestamp"][:10]
    return (
        f"Date: {letter_date}\n"
        f"Re: Referral to {specialty}\n"
        f"Patient: {patient['first_name']} {patient['last_name']}  "
        f"DOB: {patient['date_of_birth']}  MRN: {patient['mrn']}\n"
        "\n"
        f"Dear {specialty} colleague,\n"
        "\n"
        f"I am referring the above patient for {specialty} evaluation.\n"
        "\n"
        "Reason for referral:\n"
        f"{referral_reason}\n"
        "\n"
        f"Summary of encounter ({letter_date}):\n"
        f"{approved_note['note_text']}\n"
        "\n"
        "Thank you for seeing this patient. Please send your findings to me "
        "at your earliest convenience.\n"
        "\n"
        "Sincerely,\n"
        f"{referring_clinician}\n"
    )


def _same_request(entry: dict, note_id: str, specialty: str, reason: str) -> bool:
    return (
        entry["note_id"] == note_id
        and entry["specialty"].strip().lower() == specialty.strip().lower()
        and entry["referral_reason"].strip() == reason.strip()
    )


def existing_letter_id(
    entries: list[dict], note_id: str, specialty: str, referral_reason: str
) -> str | None:
    """Idempotency key: one live letter per (note, specialty, reason). A
    repeated generate call returns the existing letter instead of creating a
    duplicate. Voided letters don't count, so the same request can be
    prepared again after a void."""
    voided = {e["letter_id"] for e in entries if e.get("action") == VOID_ACTION}
    for entry in entries:
        if (
            entry.get("action") == GENERATE_ACTION
            and entry["letter_id"] not in voided
            and _same_request(entry, note_id, specialty, referral_reason)
        ):
            return entry["letter_id"]
    return None


def _build_letter(
    generated: dict, approval: dict | None, sent: dict | None, void: dict | None
) -> ReferralLetter:
    if void:
        status = "voided"
    elif sent and approval:
        status = "sent"
    elif approval:
        status = "ready_to_send"
    else:
        status = "draft"
    return ReferralLetter(
        letter_id=generated["letter_id"],
        note_id=generated["note_id"],
        patient_id=generated["patient_id"],
        ehr_system=generated["ehr_system"],
        status=status,
        specialty=generated["specialty"],
        letter_text=approval["letter_text"] if approval else generated["letter_text"],
        generated_at=generated["timestamp"],
        approved_at=approval["timestamp"] if approval else None,
        approved_by=approval["approved_by"] if approval else None,
        sent_at=sent["timestamp"] if sent and approval else None,
        sent_by=sent["sent_by"] if sent and approval else None,
        send_method=sent["send_method"] if sent and approval else None,
        voided_at=void["timestamp"] if void else None,
        voided_by=void["voided_by"] if void else None,
        void_reason=void["void_reason"] if void else None,
    )


def _replay(entries: list[dict]) -> dict[str, ReferralLetter]:
    """Every letter in the audit entries, keyed by letter_id, oldest first."""
    generated: dict[str, dict] = {}
    by_action: dict[str, dict[str, dict]] = {
        APPROVE_ACTION: {},
        SEND_ACTION: {},
        VOID_ACTION: {},
    }
    for entry in entries:
        action = entry.get("action")
        if action == GENERATE_ACTION:
            generated[entry["letter_id"]] = entry
        elif action in by_action:
            by_action[action][entry["letter_id"]] = entry
    return {
        letter_id: _build_letter(
            gen,
            by_action[APPROVE_ACTION].get(letter_id),
            by_action[SEND_ACTION].get(letter_id),
            by_action[VOID_ACTION].get(letter_id),
        )
        for letter_id, gen in generated.items()
    }


def find_letter(entries: list[dict], letter_id: str) -> ReferralLetter | None:
    """Replay the audit entries into the letter's current state."""
    return _replay(entries).get(letter_id)


def list_letters(
    entries: list[dict],
    status: LetterStatus | None = None,
    patient_id: str | None = None,
) -> list[ReferralLetter]:
    """All letters, optionally filtered, oldest first -- e.g. status=
    "ready_to_send" is the queue of approved letters not yet sent."""
    return [
        letter
        for letter in _replay(entries).values()
        if (status is None or letter.status == status)
        and (patient_id is None or letter.patient_id == patient_id)
    ]


def generate_entry(
    letter_id: str,
    timestamp: str,
    approved_note: dict,
    specialty: str,
    referral_reason: str,
    referring_clinician: str,
    letter_text: str,
) -> dict:
    return {
        "timestamp": timestamp,
        "action": GENERATE_ACTION,
        "letter_id": letter_id,
        "note_id": approved_note["note_id"],
        "ehr_system": approved_note["ehr_system"],
        "patient_id": approved_note["patient_id"],
        "specialty": specialty,
        "referral_reason": referral_reason,
        "referring_clinician": referring_clinician,
        "letter_text": letter_text,
        "status": "draft",
    }


def approve_entry(
    letter: ReferralLetter, timestamp: str, approved_by: str, letter_text: str
) -> dict:
    return {
        "timestamp": timestamp,
        "action": APPROVE_ACTION,
        "letter_id": letter.letter_id,
        "note_id": letter.note_id,
        "ehr_system": letter.ehr_system,
        "patient_id": letter.patient_id,
        "approved_by": approved_by,
        "letter_text": letter_text,
        "edited": letter_text != letter.letter_text,
        "status": "ready_to_send",
    }


def send_entry(
    letter: ReferralLetter, timestamp: str, sent_by: str, send_method: str
) -> dict:
    return {
        "timestamp": timestamp,
        "action": SEND_ACTION,
        "letter_id": letter.letter_id,
        "note_id": letter.note_id,
        "ehr_system": letter.ehr_system,
        "patient_id": letter.patient_id,
        "sent_by": sent_by,
        "send_method": send_method,
        "status": "sent",
    }


def void_entry(
    letter: ReferralLetter, timestamp: str, voided_by: str, void_reason: str
) -> dict:
    return {
        "timestamp": timestamp,
        "action": VOID_ACTION,
        "letter_id": letter.letter_id,
        "note_id": letter.note_id,
        "ehr_system": letter.ehr_system,
        "patient_id": letter.patient_id,
        "voided_by": voided_by,
        "void_reason": void_reason,
        "status_before": letter.status,
        "status": "voided",
    }
