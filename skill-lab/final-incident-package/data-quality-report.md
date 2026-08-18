# Data Quality Report — skill-lab/orders.csv

**Contract:** skill-lab/quality-contract.md
**Check time reference:** 2026-08-11 (session date)
**Rows evaluated:** 12 data rows (lines 2-13 of orders.csv, excluding header)

| Check | Evidence | Status | Recommended Action |
|---|---|---|---|
| Schema | Columns found: `order_id, customer_name, region, revenue, load_timestamp`. All contract-referenced fields (`order_id`, `region`, `revenue`, `load_timestamp`) are present. | PASS | None |
| Freshness | Row 8, `order_id=ORD-1007`, `load_timestamp=2026-08-08T05:00:00Z` — more than 24 hours old relative to the 2026-08-11 check date. All other 11 rows are timestamped 2026-08-11. | FAIL | Investigate why ORD-1007 was not refreshed in the latest load; exclude or backfill before publish. |
| Expected volume | 12 data rows vs. contract minimum of 10. | PASS | None |
| Key uniqueness | `order_id=ORD-1010` appears twice, on rows 11 and 12. | FAIL | Dedupe `ORD-1010` (rows 11-12) before publish. |
| Full-row duplicates | Rows 11 and 12 are identical across every field: `ORD-1010, Juniper Trading, West, 340.00, 2026-08-11T07:58:00Z`. | FAIL | Remove the duplicate row (row 12) before publish. |
| Required fields | `region` is blank on row 6, `order_id=ORD-1005` (Echo Retail). | FAIL | Backfill `region` on ORD-1005 before publish. |
| Nulls | `region`: 1/12 rows blank (8.3%) — ORD-1005. `order_id`, `revenue`, `load_timestamp`: 0/12 blank (0%). | WARN (also covered as hard-fail under Required Fields) | See Required Fields action for ORD-1005. |
| Numeric rules | `revenue` must be > 0. Row 7, `order_id=ORD-1006` (Foxtrot Supply), `revenue=-320.00` violates the rule. | FAIL | Investigate and correct negative revenue on ORD-1006 before publish. |

## Overall Verdict: **FAIL**

## Recommendation: **BLOCK**

Five hard-fail issues found: a stale row (ORD-1007), a duplicate key with an identical full-row duplicate (ORD-1010, rows 11-12), a missing required `region` (ORD-1005), and a negative `revenue` value (ORD-1006). Per the data-quality-gate verdict logic, any one hard-fail forces BLOCK; this dataset has four.