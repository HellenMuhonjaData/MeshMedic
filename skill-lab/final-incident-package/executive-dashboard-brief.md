# Executive Dashboard Brief — Orders Dashboard

**Date:** 2026-08-11
**Source report(s):** skill-lab/final-incident-package/data-quality-report.md; skill-lab/final-incident-package/etl-triage-report.md

## Status

Blocked

## Business Impact

The orders dashboard cannot be published in its current state. The underlying data failed quality validation, and the pipeline that refreshes it did not load any new data today after both of its scheduled attempts failed. Business impact has not been quantified in dollar or customer terms in the source reports.

## What We Know

- The orders data failed quality review and is not safe to publish: it contains a duplicate order record, an order missing its sales region, an order with a negative revenue value, and one order that is several days out of date.
- Today's scheduled data refresh did not complete. It failed twice and was automatically retried once, but hit the same error both times, so no new data was loaded.
- The refresh failure has a known, specific cause: a batch of orders came in tagged with a new sales region ("APAC") that the system's region lookup reference isn't yet set up to recognize, so those records couldn't be processed and the entire load was rejected.
- This is a new condition, not a recurring one — the same daily refresh completed successfully on each of the prior 4 days.
- Because the fix requires a data/reference update to the pipeline's region lookup, the failed load has been set aside for manual follow-up rather than retried further.

## What We Do Not Know

- The source reports could not confirm that the specific data file reviewed for quality is the direct output of today's failed refresh — the row identifiers don't line up between the two. The quality issues (duplicate record, missing region, negative revenue) should not be assumed to share the same root cause as the refresh failure until that's confirmed.
- No financial or customer impact has been estimated.
- No resolution timeline has been set.

## Decision or Action Needed

The orders dashboard should remain **BLOCKED** from publishing until: (1) the region lookup reference is updated to recognize the new region and the refresh is re-run successfully, and (2) the specific record-level issues (duplicate order, missing region, negative revenue, stale record) are corrected or excluded. No leadership action is required to keep the block in place; leadership sign-off would be needed only if there's a request to publish before both are resolved.

## Owner

Data Platform team (named as the owning team for the `orders_pipeline_daily` job in the source triage report). No individual owner is named in the source reports for the data-quality record-level fixes.

## Next Update

Next update time not yet set — confirm with Data Platform team.