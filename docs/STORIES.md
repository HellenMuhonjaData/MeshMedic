# MeshMedic — Stories

12 stories across 5 releases, walking-skeleton first:
the earliest release proves the thinnest end-to-end path including the trust
spine, and later releases stack features on top of something already working.

## Before the releases — start here

- **[STORY-000](stories/STORY-000.md)** — Build your Command Center

The first thing you build, on day one, before any part of the system itself. It is
the page you keep open for the rest of the programme and demo from. It belongs to no
release and fulfils none of your requirements, because it is the window onto your
system rather than a part of it.

## r0 · Initial Skeleton — weeks 0–1

**Goal:** Establish end-to-end functionality with audit trail and clinician control.
**Done when you can show:** Demonstrate AI-generated notes with clinician approval and audit trail holding.

- **[STORY-001](stories/STORY-001.md)** — Generate and approve AI clinical notes
- **[STORY-002](stories/STORY-002.md)** — Maintain auditable history of actions
- **[STORY-003](stories/STORY-003.md)** — Flag low-confidence AI recommendations

## r1 · Advanced Coding and Care Gap — weeks 2–3

**Goal:** Enhance coding suggestions and care gap identification.
**Done when you can show:** Show ICD-10/CPT code suggestions and care gap flags with clinician review.

- **[STORY-004](stories/STORY-004.md)** — Suggest ICD-10 and CPT codes _(waits on STORY-001)_
- **[STORY-005](stories/STORY-005.md)** — Identify and flag care gaps _(waits on STORY-001)_

## r2 · EHR Integration — weeks 4–5

**Goal:** Integrate with EHR systems using FHIR APIs.
**Done when you can show:** Retrieve and display patient data from EHRs using FHIR APIs.

- **[STORY-006](stories/STORY-006.md)** — Integrate with EHR systems using FHIR APIs _(waits on STORY-004, STORY-005)_

## r3 · Recommendation Transparency — weeks 6–7

**Goal:** Implement source citation and confidence indication features.
**Done when you can show:** Display source citations and confidence warnings in AI recommendations.

- **[STORY-007](stories/STORY-007.md)** — Provide source citations for AI recommendations _(waits on STORY-006)_
- **[STORY-008](stories/STORY-008.md)** — Indicate AI recommendation confidence _(waits on STORY-006)_

## r4 · Efficiency and Compliance — weeks 8–9

**Goal:** Optimize documentation time and ensure compliance.
**Done when you can show:** Show reduced documentation time and compliance features in action.

- **[STORY-009](stories/STORY-009.md)** — Optimize documentation time _(waits on STORY-007, STORY-008)_
- **[STORY-010](stories/STORY-010.md)** — Ensure compliance with healthcare standards _(waits on STORY-007, STORY-008)_
- **[STORY-011](stories/STORY-011.md)** — Prepare follow-up documentation for clinician review _(waits on STORY-010)_
- **[STORY-012](stories/STORY-012.md)** — Distinguish between encounter-supported and AI-generated information _(waits on STORY-010)_
