# Data Quality Report - skill-lab/orders.csv

**Checked against:** `skill-lab/quality-contract.md`
**Check date:** 2026-08-11

| Check | Evidence | Status | Recommended Action |
|---|---|---|---|
| Schema | Columns present: `order_id, customer_name, region, revenue, load_timestamp`. All contract-referenced fields (`order_id`, `region`, `revenue`, `load_timestamp`) exist. | PASS | None. |
| Freshness (<24h) | `load_timestamp` for `ORD-1007` (row 8) is `2026-08-08T05:00:00Z` — ~3 days before the 2026-08-11 check date, far outside the 24-hour window. All other rows carry `2026-08-11` timestamps within range. | FAIL | Re-extract/refresh `ORD-1007` (or drop it) before publish; investigate why it wasn't picked up in the latest load. |
| Expected volume | 12 data rows present (order_ids `ORD-1001`–`ORD-1011`, with `ORD-1010` duplicated). Contract minimum is 10. | PASS | None. |
| Key uniqueness (`order_id`) | `ORD-1010` appears twice, at rows 11 and 12 (`Juniper Trading, West, 340.00, 2026-08-11T07:58:00Z` on both). | FAIL | Dedupe `order_id ORD-1010` — remove one of the two identical rows before publish. |
| Full-row duplicates | Rows 11 and 12 are fully identical (`ORD-1010,Juniper Trading,West,340.00,2026-08-11T07:58:00Z`). | FAIL | Same fix as key-uniqueness above; a single dedupe resolves both. |
| Required fields (`region`) | Row 6, `ORD-1005` (Echo Retail), has a blank `region` value. | FAIL | Backfill `region` for `ORD-1005` before publish. |
| Nulls | `region`: 1/12 rows blank (8.3%) — `ORD-1005`. All other required/numeric fields fully populated (0% null). | WARN | Track down source of the blank region for `ORD-1005`; monitor if rate grows. |
| Numeric rules (`revenue` > 0) | Row 7, `ORD-1006` (Foxtrot Supply), has `revenue = -320.00`. | FAIL | Investigate `ORD-1006` — likely a refund/return miscoded as an order; correct or exclude before publish. |

## Overall Verdict: **FAIL**

Four hard-fail conditions were found: a duplicate primary key (`ORD-1010`), a negative value in a required-positive numeric field (`ORD-1006`, revenue -320.00), a missing required field (`ORD-1005`, region), and a stale record outside the 24-hour freshness window (`ORD-1007`).

## Recommendation: **BLOCK**

Do not publish to the executive revenue dashboard until `ORD-1010` is deduped, `ORD-1006`'s negative revenue is corrected/excluded, `ORD-1005`'s region is backfilled, and `ORD-1007` is refreshed or removed. Re-run this check after remediation.