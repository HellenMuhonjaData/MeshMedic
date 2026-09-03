"""Manual, opt-in smoke test against the live Epic sandbox -- NOT part of the
automated `pytest tests/` suite, since unit tests must stay fast, deterministic,
and I/O-free. Run by hand with `uv run python verify_epic_sandbox.py` to
confirm the JWT/OAuth2 token exchange and a real Patient.Read actually work
end-to-end against Epic's non-production sandbox before wiring this into the
MCP tools.
"""

import json

from epic_fhir_client import EpicFHIRError, fetch_patient

# Epic's own pre-filled sandbox test patient ID (from the "Try It" page for
# Patient.Read (R4)) -- synthetic data, safe to use.
TEST_PATIENT_ID = "e63wRTbPfr1p8UW81d8Seiw3"

if __name__ == "__main__":
    print(f"Fetching Patient/{TEST_PATIENT_ID} from Epic sandbox...")
    try:
        patient = fetch_patient(TEST_PATIENT_ID)
    except EpicFHIRError as e:
        print(f"FAILED: {e}")
        raise SystemExit(1)

    if patient is None:
        print("FAILED: Epic reported no such patient (404) -- unexpected for this known test ID.")
        raise SystemExit(1)

    print("SUCCESS. Patient resource:")
    print(json.dumps(patient, indent=2)[:2000])
