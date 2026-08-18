---
name: etl-failure-triage
description: Use when the user asks why an ETL or ELT pipeline, scheduled load, SQL job, data refresh, or ingestion process failed or produced suspicious output. Reviews logs and run metadata, ranks likely causes, cites evidence, and recommends the next safe diagnostic steps.
---

# ETL Failure Triage

## Purpose

Diagnose why an ETL/ELT pipeline, scheduled load, SQL job, data refresh, or
ingestion process failed or produced suspicious output. Review the failure
log and any run metadata, separate confirmed facts from hypotheses, rank
likely causes with cited evidence, and recommend the next safe diagnostic
step for each — without changing pipeline code and without rerunning
anything.

## When to invoke (and when not to)

Invoke when the user asks why a pipeline, scheduled load, SQL job, data
refresh, or ingestion process failed or produced suspicious output, and a
log, run output, or failure description is available.

Do **not** invoke for:
- A request to fix, patch, or rewrite the failing pipeline code directly —
  that's a normal implementation change, not triage.
- A request to design new tests or monitoring with no actual failure to
  investigate.
- General "how does this pipeline work" questions with no failure signal.
- A request to validate the quality of data that already loaded
  successfully (that's `/data-quality-gate`).

If the request only matches a "do not invoke" case, skip this skill even if
a pipeline or job is named in the same sentence.

## Required input

- **A log, run output, or failure description is required.** If none has
  been supplied, ask for it before proceeding. Do not fabricate or guess at
  the content of a failure that hasn't been shown.
- **Read any supplied run metadata in full** (job config, schedule,
  source/target, prior run history, retry policy). Metadata often
  disambiguates between causes that look identical from the log text alone
  — read it before forming hypotheses, not after.

## Process

1. Read the full log/output supplied, end to end. Read the run metadata
   file if one was supplied.
2. **Before ranking causes, read `references/common-failures.md`** — it
   catalogs the evidence signatures for the failure classes this skill
   screens for (schema drift, type/mapping conversion errors,
   retry-without-fix patterns, connectivity/timeout issues, resource
   limits, upstream data anomalies). Do not rank causes from memory of the
   category names alone.
3. Separate every observation into:
   - **Facts** — directly quoted or derived from the log or metadata
     (a specific line, timestamp, error class, or field value).
   - **Hypotheses** — plausible explanations that are not yet confirmed by
     evidence in hand.
4. For each hypothesis, cite the specific log line(s), timestamp(s), or
   metadata field(s) that support it. A hypothesis with no cited evidence
   does not get ranked — it gets listed as an open question instead.
5. Rank hypotheses most-to-least likely given the evidence actually
   present.
6. For each ranked cause, name one concrete next diagnostic step that does
   **not** modify code and does **not** rerun the job (e.g. "inspect the
   source schema for the `region` field," "check the lookup/mapping table
   version active at `<timestamp>`," "confirm whether the target column
   type changed in the most recent migration").

## Output format

Return, in this order:

1. **Incident Summary** — 2-4 sentences: which job, which run(s), what
   happened, when.
2. **Evidence** — bullet list of Facts pulled directly from the log or
   metadata, each with a line reference or timestamp.
3. **Ranked Causes** — numbered list, most likely first. Each entry states
   the hypothesis, the evidence it rests on, and a confidence level
   (High / Medium / Low).
4. **Next Tests** — for each ranked cause, one specific, safe,
   non-destructive diagnostic step.
5. **Escalation Recommendation** — whether this needs human or on-call
   escalation now, and why (or why not), based on the causes found and
   their blast radius.

## Constraints

- **Do not change pipeline code.** This is a read-only diagnostic step.
- **Do not rerun jobs.** Reruns are a decision for the pipeline owner, not
  this skill.
- **Do not claim a root cause without evidence in hand.** If the available
  log/metadata is insufficient to rank causes with confidence, say so
  explicitly and list what additional evidence (a specific log excerpt,
  a metadata field, a source schema snapshot) would resolve the ambiguity.
- Keep the analysis procedural — report findings, don't narrate the process
  of reading the files.
- Do not run this skill without a log, run output, or failure description
  supplied.