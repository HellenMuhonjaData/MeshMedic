# STORY-017 — Explainable ICD-10 codes with per-code decisions

As a clinician, I want explainable ICD-10 codes with per-code decisions, so that MeshMedic prepares documentation in the background while I stay in control of every clinical decision.

**Release:** Async copilot · Phase 4
**Owner:** MeshMedic
**Blocked by:** STORY-016

## The requirement this satisfies

- **REQ-027** — Each suggested ICD-10 code shows its rationale, verified supporting evidence and the model's self-reported confidence, and can be approved, edited or rejected individually.
- **REQ-031** — Model-generated confidence is never treated as validated clinical accuracy and never removes or shortens clinician review.

## How to build it

Per-code rationale, evidence quotes and self-reported confidence; evidence verified against the transcript or chart; per-code approve, edit or reject; attested final decision.

## Failure paths you must handle

- An evidence quote is not in the transcript.
- A malformed code.
- Reject without a reason.

## Acceptance — your stop condition

- [ ] A code whose evidence is not found is marked and blocks final approval until resolved.
- [ ] Mixed approve, edit and reject decisions are recorded per code with clinician ID and reasons; reject without a reason is refused.
- [ ] Final code approval without an attestation is refused, including from an AI client.
- [ ] A malformed code is refused at drafting time.
- [ ] Whole-set approve and reject still work.
