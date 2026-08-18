# ETL Failure Triage — orders_pipeline_daily

## Incident Summary

The `orders_pipeline_daily` job (scheduled 04:00 UTC, source `orders_raw_export` → target `stg_orders_fact`) failed both of its 2 configured attempts on 2026-08-11. Both attempts extracted 1,842 rows successfully but failed in the transform step because 63 rows carry a new `region` value, `APAC`, that has no entry in the `region_map` lookup table (last updated 2026-05-02). The failed join produced NULL `region_id` for those 63 rows, which violated a NOT NULL constraint on `stg_orders_fact.region_id`. Because the load runs as a single transaction, the entire 1,842-row batch was rolled back — 0 rows loaded on either attempt. The job is now in `orders_pipeline_dlq` awaiting manual intervention.

## Evidence

**Facts (log):**
- Attempt 1 (`c4f1a9e2…`) extracted 1,842 rows successfully at `04:00:06.611Z`, including region value `APAC` (log line 3).
- Schema check flagged 63 rows with `region=APAC`, outside the expected domain `[West, East, South, North]`, sample IDs `ORD-58231, ORD-58244, ORD-58309` (log line 4).
- `orders_region_mapping_failed` — `MappingLookupError`: "No entry in region_map for region value 'APAC' - region_id resolved to NULL for 63 rows" (log line 7).
- `orders_transform_failed` — `NotNullConstraintViolation` on `stg_orders_fact.region_id`, 63 rows, first failed row `ORD-58231` (log line 8).
- Load rolled back: 0 rows loaded, 1,842 rolled back, "single-transaction load, no partial commit" (log line 9).
- Attempt 1 ended `04:00:07.470Z`, exit code 1, `rows_loaded: 0` (log line 10).
- Attempt 2 (`9b27de51…`, `retry_of` attempt 1) ran `04:05:07.611Z`–`04:05:08.790Z` and failed with the **identical** error class, message, and 63-row set (log lines 13-18).
- Retries exhausted after 2 attempts, `identical_error_across_attempts: true`, `next_action: manual_intervention_required`, routed to `orders_pipeline_dlq` (log line 19).

**Facts (metadata):**
- `region_map` known entries: `West, East, South, North`. No `APAC` entry. Table last updated `2026-05-02`.
- Source-system change log: Sales Ops enabled the `APAC` region in the upstream CRM on `2026-08-10`; first `APAC`-tagged records appeared in the `2026-08-11` extract window.
- No changes recorded to the job code, `region_map` contents, or `stg_orders_fact` schema/constraints in this window.
- Attempt 1 and attempt 2 used the same source snapshot, same `region_map` version, and same job config (no diff between attempts).
- No infrastructure incidents (network, database, compute) recorded during either attempt.
- Run history: 2026-08-11 FAILED, 0 rows loaded, vs. 1,764–1,801 rows loaded on each of the prior 4 successful daily runs.

**Open question (not resolved by the supplied log/metadata):** `skill-lab/orders.csv` uses order IDs `ORD-1001`–`ORD-1011` and shows no `APAC` values, whereas the failing rows in this log use IDs in the `ORD-58xxx` range with `region=APAC`. The supplied log and metadata do not establish that `orders.csv` is the literal output of this specific failed run — they explain why `stg_orders_fact` received 0 new rows on 2026-08-11 (consistent with the freshness gap on `ORD-1007` found in the data-quality report), but they do not speak to the separate row-level issues found in `orders.csv` (duplicate key/full-row duplicate on `ORD-1010`, blank `region` on `ORD-1005`, negative `revenue` on `ORD-1006`). Those should not be attributed to this pipeline failure without further evidence.

## Ranked Causes

1. **Missing `region_map` entry for the newly-enabled `APAC` domain value** — High confidence.
   Evidence: schema check flags `APAC` as unexpected (log line 4); `MappingLookupError` names the exact missing value and row count (log line 7); `region_map` metadata confirms `APAC` is absent and the table hasn't been updated since `2026-05-02`; CRM change log confirms `APAC` went live `2026-08-10`, one day before it started appearing in the extract.

2. **Retry did not — and could not — resolve the failure because the cause is structural, not transient** — High confidence (confirming evidence, not an independent root cause).
   Evidence: identical error class, message, and affected-row set on both attempts (log lines 7-8 vs. 15-16); metadata confirms no config/snapshot difference between attempts and no infra incidents. Per the reference guide, an identical failure across a retry rules out transient causes and points at a structural gap in the mapping table.

## Next Tests

1. For cause 1: Inspect the current `region_map` reference table (read-only) to confirm `APAC` is still absent, and check whether a pending update/ticket to add `APAC` already exists in the Data Platform team's backlog. Do not edit `region_map` or the pipeline as part of this step.
2. For cause 2: No further test needed — metadata already corroborates that attempts 1 and 2 ran with identical inputs/config, which is sufficient to rule out an environmental difference between attempts.

## Escalation Recommendation

**Escalate now.** The log itself marks `next_action: manual_intervention_required` and the job is sitting in the dead-letter queue with 0 rows loaded — this is a hard stop, not a self-healing condition. Blast radius: a full day's `orders_pipeline_daily` load is blocked (0 of ~1,800 expected rows landed vs. a 4-day trailing average of ~1,760–1,800), and the fix (adding `APAC` to `region_map`) is a data/config change outside this read-only skill's scope and outside Claude's autonomous-implementation lane per this repo's governance rules — it should go to the Data Platform team (job owner per metadata).