---
name: executive-dashboard-brief
description: Use when the user asks to turn a data-quality result, failed refresh, pipeline incident, KPI variance, or technical investigation into an executive dashboard update. Produces a concise leadership brief containing status, business impact, verified evidence, decision needed, owner, and next update time.
---

# Executive Dashboard Brief

## Purpose

Translate a technical finding — a data-quality gate result, an ETL/pipeline
failure triage, a KPI variance, or another technical investigation — into a
short, non-technical brief that a leadership dashboard or exec audience can
consume in under a minute. This skill synthesizes and reformats; it does not
investigate, diagnose, or re-derive findings.

## When to invoke (and when not to)

Invoke when the user asks to:
- Turn a data-quality report, triage result, or investigation into an
  executive/leadership update.
- Summarize a pipeline incident, KPI variance, or dashboard block for a
  non-technical audience.
- Produce a status brief suitable for a leadership dashboard.

Do **not** invoke for:
- Running the underlying quality check itself — that's `/data-quality-gate`.
- Diagnosing why a pipeline failed — that's `/etl-failure-triage`.
- A request to just explain or summarize a technical report for another
  engineer (still technical audience, not executive-facing).

If the underlying quality check or triage hasn't been run yet, invoke the
relevant skill first (or ask the user to supply its output) before writing
the brief.

## Required input

At least one of the following must be supplied:
- A data-quality gate report (verdict, evidence, PUBLISH/BLOCK
  recommendation).
- An ETL failure triage result (incident summary, evidence, ranked causes,
  escalation recommendation).
- Equivalent verified technical findings the user pastes in directly.

If none of these is available, ask for it before proceeding. Do not
fabricate a brief from an unstated or assumed investigation.

## Process

1. Read the supplied quality-gate report and/or triage report in full.
   Treat these as the sole source of truth — do not re-run checks, re-read
   raw logs, or form new hypotheses.
2. Pull out only what the source report(s) already established as fact:
   verdicts, cited evidence, confirmed causes, named owners, stated
   timestamps or deadlines.
3. Sort every item into exactly one of two buckets:
   - **What We Know** — directly supported by evidence already cited in the
     source report(s) (a verdict, a specific check result, a fact/evidence
     line, a High-confidence ranked cause).
   - **What We Do Not Know** — open questions, unconfirmed or Low/Medium-
     confidence hypotheses, or anything the source report flagged as
     unresolved.
4. Determine **Status** by carrying forward the source verdict, not by
   inventing a new one (e.g. a quality-gate FAIL/BLOCK becomes "Blocked"; a
   triage result with retries exhausted and DLQ routing becomes "Failed —
   pending manual intervention"; an escalation "not needed" becomes
   "Resolved" or "Monitoring" as appropriate).
5. Decide whether the dashboard should remain blocked by mirroring the
   source report's own recommendation (PUBLISH/BLOCK, or escalation
   guidance). If the source gave no such recommendation, say so explicitly
   and default to treating the dashboard as blocked/at-risk rather than
   assuming it is safe.
6. Write **Business Impact** using only impact explicitly stated in the
   source material or supplied by the user (e.g., "revenue dashboard cannot
   be published," "data is 3 days stale"). Never estimate dollar figures,
   customer counts, SLA penalties, or other consequences that are not
   explicitly given.
7. Write **Owner** using only a name/team explicitly present in the source
   metadata (e.g., a job's listed owning team). If none is named, write
   "Owner not yet assigned" — never guess a person or team.
8. Write **Next Update** using only a time explicitly given by the user or
   source docs. If none is given, write "Next update time not yet set —
   confirm with [owner]" rather than inventing a cadence.
9. Populate `template.md`'s section structure exactly, in order, and return
   the filled-in brief.

## Output format

Use `template.md` as the exact structure for the final brief: **Status**,
**Business Impact**, **What We Know**, **What We Do Not Know**, **Decision
or Action Needed**, **Owner**, **Next Update** — in that order, with those
exact section headers.

- Plain business language throughout. No stack traces, correlation IDs,
  JSON payloads, SQL, raw log lines, or internal error-class names.
- Each bullet under "What We Know" should be a plain-language fact, not a
  copy-pasted technical line — translate, don't transcribe.
- "What We Do Not Know" must not be left empty by omission — if the source
  report(s) leave nothing unresolved, state that explicitly rather than
  dropping the section.

## Constraints

- **Never invent** financial impact, root cause, owner, or timing that is
  not already present in the supplied source report(s) or stated by the
  user.
- Never modify, re-run, or re-check the underlying source reports — this is
  a read-only synthesis step.
- Do not include raw logs or unnecessary technical detail in the final
  brief — that detail belongs in the source report, not the executive
  version.
- Always state plainly whether the dashboard/report should remain blocked;
  do not leave this ambiguous.
- Do not run this skill without a supplied quality-gate report, triage
  result, or equivalent verified findings.