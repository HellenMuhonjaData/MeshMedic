# STORY-019 — Chart-aware care gaps with source attribution

As a clinician, I want chart-aware care gaps with source attribution, so that MeshMedic prepares documentation in the background while I stay in control of every clinical decision.

**Release:** Async copilot · Phase 6
**Owner:** MeshMedic
**Blocked by:** STORY-016

## The requirement this satisfies

- **REQ-028** — Care gaps are identified from chart data and the encounter, each with its source (rule and chart evidence, or transcript quote), and every accept or dismiss is audited.

## How to build it

Read-only Epic Observation, Immunization, Procedure and Condition fetches alongside the existing Patient fetch; versioned screening rules; every gap shows its source. Needs decisions on rule ownership and Epic permissions.

## Failure paths you must handle

- Epic times out or returns 401.
- A resource type is missing.
- Rule boundary ages and dates.

## Acceptance — your stop condition

- [ ] Each rule fires and stays silent correctly at age and date boundaries.
- [ ] Every gap shows its source: rule plus chart evidence, or a transcript quote.
- [ ] Epic failures show 'chart data unavailable', never 'no gaps'.
- [ ] The existing Epic Patient fetch and its tests are unchanged.
- [ ] Chart clinical values never appear in structured logs.
