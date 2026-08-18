---
name: data-quality-gate
description: Use when the user explicitly asks to validate a dataset, CSV, query result, or ETL output for quality issues, or asks whether a dataset/dashboard/report is ready to publish (e.g. "validate this data", "run the quality gate", "is this safe to publish", "check this ETL output before it ships"). Checks the data against a quality contract and returns PASS, WARN, or FAIL with evidence and a PUBLISH or BLOCK recommendation. Do NOT use for merely writing/optimizing SQL, calculating or explaining a metric, or designing a dashboard/chart layout — a dataset being mentioned or referenced in those requests is not by itself a reason to invoke this skill; the user must be asking for a validation or publish-readiness check.
---

# Data Quality Gate

## Purpose

Validate a dataset against a quality contract before it is published, and produce
an evidence-backed PASS / WARN / FAIL verdict with a PUBLISH or BLOCK recommendation.

## When to invoke (and when not to)

Invoke only when the request is one of:
- Validate a dataset / CSV / query result / ETL output for data-quality issues.
- Determine whether a dataset, dashboard, or report is ready to publish.

Do **not** invoke for:
- A request to write, fix, or optimize SQL — even if it queries the same dataset.
- A request to calculate, define, or explain a metric.
- A request to design, lay out, or style a dashboard or chart (that's `/dataviz`).
- General data exploration ("what's in this file", "summarize this table") with no
  validation or publish-readiness ask.

If the request only matches a "do not invoke" case, skip this skill even if a
dataset, CSV, or dashboard is named in the same sentence. Only trigger when the
user is actually asking for a quality/validation/publish-readiness check.

## Required input

- **Dataset path is required.** If the user has not supplied a path to the dataset
  (CSV, query result, ETL output, dashboard source), ask for it before proceeding.
  Do not guess or substitute a different file.

## Quality contract

- If the user supplies a quality contract (a file or inline rules), use it as the
  source of truth for thresholds and required checks.
- If no contract is supplied, fall back to a reasonable default contract (uniqueness
  of the primary key, no missing required fields, no negative values in numeric
  amount fields, data no older than 24 hours, row count above a sane floor) and
  state clearly that defaults were used.

## Checks to run

For the supplied dataset, evaluate: schema, freshness, expected volume, key
uniqueness, full-row duplicates, required fields, nulls, and numeric rules.

**Before running the checks, read `references/quality-checks.md`** — it defines
the pass/fail criteria, evidence expectations, and edge cases for each of the
eight checks above. Do not run this skill from memory of the check names alone.

## Output format

Return a table with these exact columns:

| Check | Evidence | Status | Recommended Action |
|---|---|---|---|

- **Evidence** must cite concrete values: row numbers, offending IDs, counts, or
  timestamps - not vague descriptions.
- **Status** is PASS, WARN, or FAIL per row.
- **Recommended Action** is a specific next step (e.g. "dedupe order_id 10231
  before publish", "backfill missing region on row 7").

After the table, finish with:

1. An overall verdict: **PASS**, **WARN**, or **FAIL**.
2. A recommendation: **PUBLISH** or **BLOCK**.

Verdict logic: any FAIL-level check (contract-hard-fail, e.g. duplicate keys,
negative required-positive values, stale data past the freshness limit) makes the
overall verdict FAIL and the recommendation BLOCK. If only soft issues are present
(e.g. minor null rates, missing non-key optional fields) the verdict is WARN and
the recommendation may still be PUBLISH with noted caveats, at the user's
discretion. No hard-fail issues means PASS and PUBLISH.

## Constraints

- Never modify the source data. This is a read-only validation step.
- Keep the analysis concise and procedural - report findings, do not narrate the
  process of checking them.
- Do not run this check on a dataset the user has not named.