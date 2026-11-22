"""AI encounter drafting: the output contract for /api/draft-note (REQ-017, REQ-008).

One model call drafts everything the clinician then reviews: the note, its
billing codes, care gaps, test orders and a referral, each with the AI's own
confidence where the server's tools expect one. Nothing here is a clinical
record -- the UI records the AI's versions through the MCP tools
(generate_encounter_note, suggest_codes, record_ai_suggestions) and the
clinician approves, edits or drops each item, which the server tracks.

Kept separate from ui_bridge.py so the parsing/validation is a pure function
that can be unit tested without starting the bridge or calling the API.
"""

import json
from dataclasses import dataclass, field

# Same threshold server.py enforces; below it a reason is required (REQ-008).
CONFIDENCE_THRESHOLD = 0.7

# Appended to the shared drafting prompt (prompts/generate-clinical-note/) so
# that prompt stays reusable for plain-text drafting elsewhere.
CONFIDENCE_INSTRUCTIONS = """

Output format for this request: return JSON matching the provided schema.
Every suggestion is reviewed by the clinician, who can edit or drop it.
Ground all of it in the transcript; suggest nothing the transcript does not support.

- note_text: the note itself, following every rule above.
- confidence: 0.0-1.0, how well the transcript supports this note. Lower it
  when the transcript is short, ambiguous, contradictory, or missing an
  assessment or plan.
- confidence_reason: one or two plain sentences naming what limits your
  confidence (required when confidence is below 0.7; otherwise say briefly
  why the transcript supports the note well).
- codes: ICD-10 codes for problems documented in this visit. Code an
  unconfirmed diagnosis by its symptom or finding, not the suspected
  disease. Add a CPT code only if the transcript makes the service clear;
  otherwise leave CPT out. Empty list if nothing can be coded.
- codes_confidence / codes_confidence_reason: one score for the whole code
  set, same rules as the note's confidence.
- care_gaps: preventive or follow-up care the transcript says is overdue or
  missing (e.g. an overdue screening the clinician mentions). Each with its
  own confidence and reason. Empty list if none are mentioned.
- test_orders: tests the clinician said they would order in this visit, one
  entry per test, each with the clinical reason. Empty list if none.
- referral: needed=true only if the clinician said they are referring the
  patient; then the specialty and the reason. Otherwise needed=false with
  empty strings.
"""

_CONFIDENCE_PAIR = {"confidence": {"type": "number"}, "confidence_reason": {"type": "string"}}

DRAFT_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "note_text": {"type": "string"},
        **_CONFIDENCE_PAIR,
        "codes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "code_system": {"type": "string", "enum": ["icd-10", "cpt"]},
                    "description": {"type": "string"},
                },
                "required": ["code", "code_system", "description"],
                "additionalProperties": False,
            },
        },
        "codes_confidence": {"type": "number"},
        "codes_confidence_reason": {"type": "string"},
        "care_gaps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"description": {"type": "string"}, **_CONFIDENCE_PAIR},
                "required": ["description", "confidence", "confidence_reason"],
                "additionalProperties": False,
            },
        },
        "test_orders": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"test_name": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["test_name", "reason"],
                "additionalProperties": False,
            },
        },
        "referral": {
            "type": "object",
            "properties": {
                "needed": {"type": "boolean"},
                "specialty": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["needed", "specialty", "reason"],
            "additionalProperties": False,
        },
    },
    "required": [
        "note_text", "confidence", "confidence_reason", "codes", "codes_confidence",
        "codes_confidence_reason", "care_gaps", "test_orders", "referral",
    ],
    "additionalProperties": False,
}

# Field limits mirror the MCP tools' own schemas, so a draft that passes here
# is never rejected later when the UI records it.
_MAX = {"code": 20, "description": 300, "test_name": 200, "reason": 500, "specialty": 100, "referral_reason": 1000}


class DraftContractError(ValueError):
    """The model's output did not match the draft contract."""


@dataclass(frozen=True)
class NoteDraft:
    note_text: str
    confidence: float
    confidence_reason: str
    codes: list[dict] = field(default_factory=list)
    codes_confidence: float | None = None
    codes_confidence_reason: str = ""
    care_gaps: list[dict] = field(default_factory=list)
    test_orders: list[dict] = field(default_factory=list)
    referral: dict | None = None

    @property
    def low_confidence(self) -> bool:
        return self.confidence < CONFIDENCE_THRESHOLD


def _text(value, label: str, max_len: int, required: bool = True) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if required and not text:
        raise DraftContractError(f"draft has an empty {label}")
    if len(text) > max_len:
        raise DraftContractError(f"draft {label} is longer than {max_len} characters")
    return text


def _confidence(value, reason, label: str) -> tuple[float, str]:
    # bool is an int subclass in Python; reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DraftContractError(f"draft {label} confidence is not a number")
    if not 0.0 <= float(value) <= 1.0:
        raise DraftContractError(f"draft {label} confidence {value} is outside 0-1")
    reason = reason.strip() if isinstance(reason, str) else ""
    if float(value) < CONFIDENCE_THRESHOLD and not reason:
        raise DraftContractError(f"low-confidence {label} has no confidence_reason")
    return round(float(value), 2), reason


def _list(data: dict, key: str) -> list:
    value = data.get(key, [])
    if not isinstance(value, list):
        raise DraftContractError(f"draft {key} is not a list")
    return value


def parse_draft(raw_text: str) -> NoteDraft:
    """Parse and validate the model's JSON draft. Raises DraftContractError.

    The note fields are required. The suggestion fields are optional so an
    older, note-only draft still parses; when present they are validated.
    """
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise DraftContractError(f"draft was not valid JSON: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise DraftContractError("draft JSON was not an object")

    note_text = _text(data.get("note_text"), "note_text", 5000)
    confidence, reason = _confidence(data.get("confidence"), data.get("confidence_reason"), "note")

    codes = []
    for c in _list(data, "codes"):
        if not isinstance(c, dict) or c.get("code_system") not in ("icd-10", "cpt"):
            raise DraftContractError("draft code is malformed")
        codes.append({
            "code": _text(c.get("code"), "code", _MAX["code"]),
            "code_system": c["code_system"],
            "description": _text(c.get("description"), "code description", _MAX["description"]),
        })
    codes_confidence, codes_reason = (None, "")
    if codes:
        codes_confidence, codes_reason = _confidence(
            data.get("codes_confidence"), data.get("codes_confidence_reason"), "code set"
        )

    gaps = []
    for g in _list(data, "care_gaps"):
        if not isinstance(g, dict):
            raise DraftContractError("draft care gap is malformed")
        g_conf, g_reason = _confidence(g.get("confidence"), g.get("confidence_reason"), "care gap")
        gaps.append({
            "description": _text(g.get("description"), "care gap description", _MAX["description"]),
            "confidence": g_conf,
            "confidence_reason": g_reason or None,
        })

    orders = []
    for o in _list(data, "test_orders"):
        if not isinstance(o, dict):
            raise DraftContractError("draft test order is malformed")
        orders.append({
            "test_name": _text(o.get("test_name"), "test name", _MAX["test_name"]),
            "reason": _text(o.get("reason"), "test reason", _MAX["reason"]),
        })

    referral = None
    r = data.get("referral")
    if isinstance(r, dict) and r.get("needed") is True:
        referral = {
            "specialty": _text(r.get("specialty"), "referral specialty", _MAX["specialty"]),
            "reason": _text(r.get("reason"), "referral reason", _MAX["referral_reason"]),
        }

    return NoteDraft(
        note_text=note_text,
        confidence=confidence,
        confidence_reason=reason,
        codes=codes,
        codes_confidence=codes_confidence,
        codes_confidence_reason=codes_reason,
        care_gaps=gaps,
        test_orders=orders,
        referral=referral,
    )
