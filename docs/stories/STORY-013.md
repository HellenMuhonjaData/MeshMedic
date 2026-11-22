# STORY-013 — Foundations for the asynchronous copilot

As a clinician, I want foundations for the asynchronous copilot, so that MeshMedic prepares documentation in the background while I stay in control of every clinical decision.

**Release:** Async copilot · Phase 0
**Owner:** MeshMedic
**Blocked by:** nothing

## The requirement this satisfies

- **REQ-006** — existing requirement
- **REQ-019** — Only an authenticated clinician can sign a clinical note, authorize an order, finalize code decisions or approve a referral for sending. AI clients and background processing can never perform these actions, and the server enforces this.
- **REQ-025** — Real patient data is refused until the PHI readiness sign-off is complete. Raw audio is encrypted, access-restricted, deleted on a configurable retention schedule separate from clinical notes, and every deletion is logged.

## How to build it

Split server.py into modules with no behaviour change; record who acted on every audit entry; restrict the browser's pass-through route to an allowlist; define one status vocabulary and random encounter IDs; refuse real patient data until the readiness sign-off is complete.

## Failure paths you must handle

- A module split breaks a tool's name or schema.
- A clinician-only tool becomes reachable through the pass-through route.
- Real-data mode starts without a retention policy or sign-off.

## Acceptance — your stop condition

- [x] Every existing tool keeps the same name, inputs and outputs (tests/test_tool_contract.py).
- [x] An attested or unlisted tool called through the pass-through route is refused with 403 and the audit trail is unchanged.
- [x] New audit entries carry an actor; older entries read as legacy.
- [x] Starting with MESHMEDIC_DATA_MODE=real and no policy or readiness file fails with a clear message; synthetic mode starts normally.
- [x] No new module is over 500 lines.
