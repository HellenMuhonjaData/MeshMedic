# STORY-015 — Background encounter processing and review queue

As a clinician, I want background encounter processing and review queue, so that MeshMedic prepares documentation in the background while I stay in control of every clinical decision.

**Release:** Async copilot · Phase 2
**Owner:** MeshMedic
**Blocked by:** STORY-014

## The requirement this satisfies

- **REQ-020** — After a consultation ends, the system processes the recording in the background (transcript, note, codes, care gaps, follow-up) so the clinician can move to the next patient without waiting.
- **REQ-021** — Each patient encounter is an independent session; recordings, notes and recommendations are never mixed between patients.
- **REQ-023** — Drafts and review progress are saved on the server so a clinician can leave an encounter and return without losing work.
- **REQ-025** — Real patient data is refused until the PHI readiness sign-off is complete. Raw audio is encrypted, access-restricted, deleted on a configurable retention schedule separate from clinical notes, and every deletion is logged.

## How to build it

Encounters with saved, encrypted audio; a background worker (transcribe, draft, record, check) with retries, resume and duplicate prevention; a SQLite job store holding no patient data behind an interface; a review queue; server-side saved work; encryption at rest for the audit trail, keeping it append-only and fully verifiable.

## Failure paths you must handle

- Transcription or AI service times out, rate-limits or errors.
- The process stops mid-job.
- Two encounters are processed at once.
- The audit-trail encryption key is missing or wrong.
- An encrypted audit entry is altered, removed or reordered.

## Acceptance — your stop condition

- [ ] A clinician can end a consultation and start the next immediately; the first reaches ready for review with no further clicks.
- [ ] Two encounters for different patients processed at once never share content; a mismatched record is refused.
- [ ] Stopping the worker after each stage and restarting yields exactly one transcript, note and set of suggestions.
- [ ] External failures retry 3 times then reach needs attention, keeping the audio and creating no partial note.
- [ ] Job-store records contain no patient name, MRN, date of birth, transcript or note text.
- [ ] Audio files cannot be read without the key; requests without sign-in or for another encounter are refused.
- [ ] Expired audio is deleted with an audit entry and the note is unchanged.
- [x] The compliance check fails if any sign-off, authorization, approval or sending was done by the AI or system.
- [ ] Edits survive a browser reload and show the same in a second browser.
- [ ] Only the job-store module refers to SQLite.
- [x] Records written before encryption stay in place, unchanged and readable; nothing already in the audit trail is rewritten.
- [x] New audit entries are encrypted at rest one entry at a time with authenticated encryption; no clinical text appears in the file in plain form.
- [x] The audit trail stays append-only: each new entry carries a hash of the previous line, so altering, removing or reordering any entry is detected, and this check works without the decryption key.
- [x] The compliance check decrypts and verifies every entry and fails on a tampered entry or a broken chain; a missing key fails loudly instead of skipping entries.
- [x] Every existing reader (notes, codes, gaps, orders, letters, dashboard, compliance) returns the same results on an encrypted trail as on the plain one.
