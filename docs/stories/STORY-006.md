# STORY-006 — Integrate with EHR systems using FHIR APIs

As a system integrator, I want to integrate with EHR systems using FHIR APIs, so that I can access patient data seamlessly.

**Release:** r2 · EHR Integration (weeks 4–5)
**Owner:** System Integrator
**Blocked by:** STORY-004, STORY-005

## The requirement this satisfies

- **REQ-009** (Functional, must) — The system must retrieve and return authorized patient information through EHR systems using FHIR APIs.
- **REQ-010** (Constraint, must) — The system must remain EHR-agnostic while integrating with Epic and Oracle Health/Cerner.

## How to build it

Develop FHIR API integration for patient data retrieval and ensure EHR-agnostic operation.

## Failure paths you must handle

- FHIR API not accessible
- Data retrieval errors
- EHR-specific issues

## Acceptance — your stop condition

Tick each box as it genuinely passes. This file is yours — the platform reads
the same criteria out of `.colaberry/progress.json`, which Claude Code keeps in
step (see the managed block in CLAUDE.md). Ticking something you have not
actually met only misleads you.

- [ ] Given a FHIR API endpoint, when the system requests patient data, then it retrieves and displays the data.
- [ ] Given an EHR system, when the system integrates with it, then it remains EHR-agnostic.
- [ ] Trust: The system must log all data retrieval actions from EHRs.

When every box above is ticked, stop and show the demo.
