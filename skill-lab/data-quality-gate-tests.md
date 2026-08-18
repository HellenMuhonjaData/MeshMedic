# data-quality-gate — Trigger Test Cases

Manual test prompts for verifying the skill's trigger boundary after hardening.
Run each prompt in a fresh session and confirm the observed behavior matches
the expectation.

## Should trigger the skill

1. "Validate `skill-lab/orders.csv` against `skill-lab/quality-contract.md`
   before we publish it to the executive dashboard."
2. "Can you run the quality gate on this ETL output? I need to know if it's
   safe to load into prod."
3. "Is `skill-lab/orders.csv` ready to publish? Check it for duplicates,
   nulls, and stale rows first."

## Should NOT trigger the skill

1. "Write a SQL query that sums `revenue` by `region` from the `orders` table."
2. "Design a dashboard layout showing weekly revenue trend and top 5 regions
   by order count."
3. "How do I calculate month-over-month growth rate from this revenue column?"

## Expected output requirements

**When triggered:**
- Skill asks for the dataset path if one wasn't given, rather than guessing.
- If no quality contract is supplied, the response states plainly that default
  thresholds were used.
- Output includes a table with exactly these columns: `Check | Evidence |
  Status | Recommended Action`.
- Evidence cites concrete values (row numbers, IDs, counts, or timestamps) —
  not vague descriptions like "some rows have issues."
- Every row's `Status` is one of `PASS`, `WARN`, `FAIL`.
- Response ends with an overall verdict (`PASS`/`WARN`/`FAIL`) and a
  recommendation (`PUBLISH`/`BLOCK`), consistent with the hard-fail vs
  soft-issue logic in `references/quality-checks.md`.
- Source data is not modified.

**When not triggered:**
- No PASS/WARN/FAIL table, no PUBLISH/BLOCK recommendation, and no read-only
  quality contract framing anywhere in the response.
- The response directly answers the SQL / dashboard-design / metric question
  asked, without first routing through a data-quality validation step.
- If the dataset named in a SQL/metric/dashboard request happens to also have
  an available quality contract, the skill still does not activate unless the
  user separately asks for validation or publish-readiness.