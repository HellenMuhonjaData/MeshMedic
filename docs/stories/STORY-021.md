# STORY-021 — End-of-day and after-hours reporting

As a clinician, I want end-of-day and after-hours reporting, so that MeshMedic prepares documentation in the background while I stay in control of every clinical decision.

**Release:** Async copilot · Phase 8
**Owner:** MeshMedic
**Blocked by:** STORY-014, STORY-015

## The requirement this satisfies

- **REQ-024** — A dashboard shows encounters processed, notes awaiting review, signed notes, outstanding orders and pending referrals, plus active review time and notes unsigned at the end of the clinic day.

## How to build it

Dashboard counts for the encounter pipeline, signed notes, outstanding orders and pending referrals; notes unsigned at end of clinic day; notes signed after hours; active review time from visible, active tabs only.

## Failure paths you must handle

- Time zones and the end-of-day boundary.
- A review tab left open in the background.

## Acceptance — your stop condition

- [ ] Every dashboard count matches a direct count over the audit trail.
- [ ] Unsigned at end of clinic day is correct across time zones and at the boundary.
- [ ] Active review time ignores hidden or idle time.
- [ ] The dashboard shows counts only, no patient information.
