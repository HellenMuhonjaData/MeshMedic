# STORY-018 — Clinician-authorized tests and follow-up

As a clinician, I want clinician-authorized tests and follow-up, so that MeshMedic prepares documentation in the background while I stay in control of every clinical decision.

**Release:** Async copilot · Phase 5
**Owner:** MeshMedic
**Blocked by:** STORY-014, STORY-015

## The requirement this satisfies

- **REQ-029** — AI may propose tests and follow-ups, but no order takes effect until an authenticated clinician authorizes it with an indication; identity, indication and time are recorded, and urgent orders can always be authorized directly.
- **REQ-019** — Only an authenticated clinician can sign a clinical note, authorize an order, finalize code decisions or approve a referral for sending. AI clients and background processing can never perform these actions, and the server enforces this.

## How to build it

Proposed tests and follow-ups never block the schedule; orders move proposed -> authorized -> resulted; authorization records clinician, indication and time; urgent orders can always be authorized directly; dismissals are audited.

## Failure paths you must handle

- Authorization without an indication.
- An AI client tries to authorize.
- Dismissing an already accepted item.

## Acceptance — your stop condition

- [ ] No proposed order appears as pending or authorized until authorize_orders succeeds.
- [ ] Authorizing without an attestation or an indication is refused, including from an AI client.
- [ ] An authorized order records clinician ID, indication, authorization and time.
- [ ] A manually entered order can be authorized while background processing is still running.
- [ ] Authorizing twice does nothing the second time; dismissing an accepted item is refused; dismissals are recorded with a reason.
- [ ] Older orders show as authorized (legacy, no identity) and their results still work.
