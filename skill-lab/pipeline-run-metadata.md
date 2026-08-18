# Pipeline Run Metadata - orders_pipeline_daily

## Job identity

| Field | Value |
|---|---|
| Job name | `orders_pipeline_daily` |
| Schedule | Daily, `04:00 UTC` |
| Source | `orders_raw_export` (upstream CRM order export) |
| Target | `stg_orders_fact` (warehouse staging table) |
| Owner | Data Platform team |
| Retry policy | Max 2 attempts, fixed 5-minute backoff, no jitter |
| Load strategy | Single transaction per run; no partial commit on failure |

## This incident

| Field | Value |
|---|---|
| Run date | 2026-08-11 |
| Attempt 1 correlation ID | `c4f1a9e2-6b3d-4a11-9f27-1d8e6a2b7c40` |
| Attempt 1 start / end | `04:00:02.118Z` / `04:00:07.470Z` |
| Attempt 2 correlation ID | `9b27de51-3a04-4c9e-8f1b-5e0a2d7c1193` (`retry_of` attempt 1) |
| Attempt 2 start / end | `04:05:07.611Z` / `04:05:08.790Z` |
| Final status | FAILED - retries exhausted, routed to `orders_pipeline_dlq` |
| Rows extracted (both attempts) | 1,842 |
| Rows loaded (both attempts) | 0 (transaction rolled back on transform failure) |

## Run history (last 5 scheduled runs)

| Run date | Status | Rows loaded | Notes |
|---|---|---|---|
| 2026-08-11 | FAILED (2 attempts) | 0 | This incident |
| 2026-08-10 | SUCCESS | 1,779 | Last known-good run |
| 2026-08-09 | SUCCESS | 1,801 | |
| 2026-08-08 | SUCCESS | 1,764 | |
| 2026-08-07 | SUCCESS | 1,690 | |

## Reference table: `region_map`

| Field | Value |
|---|---|
| Table | `region_map` |
| Current version / last updated | `2026-05-02` |
| Known entries | `West`, `East`, `South`, `North` |
| Entry for `APAC` | **Not present** |

## Recent upstream changes (from source-system change log)

- `2026-08-10`: Sales Ops enabled the `APAC` region in the upstream CRM as
  part of a new territory rollout. First `APAC`-tagged order records began
  appearing in `orders_raw_export` starting with the `2026-08-11` extract
  window (`2026-08-10T04:00:00Z`–`2026-08-11T04:00:00Z`).
- No changes recorded to `orders_pipeline_daily` job code, `region_map`
  contents, or the `stg_orders_fact` schema/constraints in this window.

## Environment

- Attempt 1 and attempt 2 ran against the same source snapshot, same
  `region_map` version, and same job configuration (no config diff between
  attempts).
- No infrastructure incidents (network, database, compute) recorded for
  the warehouse or CRM export service during either attempt window.