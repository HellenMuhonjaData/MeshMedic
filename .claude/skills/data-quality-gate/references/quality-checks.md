# Quality Checks Reference

Detailed pass/fail criteria and evidence expectations for each of the eight
checks run by the data-quality-gate skill. Read this before running the checks
listed in `SKILL.md`.

## 1. Schema

- Compare the dataset's actual columns against the contract's expected columns
  (or, with no contract, against the columns the user's request implies are needed).
- FAIL if a contract-required column is missing entirely.
- WARN if a column's apparent type doesn't match expectation (e.g. a numeric
  field containing text) but the column exists.
- Evidence: list the actual column names found, and name any missing/mismatched
  ones explicitly.

## 2. Freshness

- Compare the load/update timestamp field against the contract's max-age rule
  (default: 24 hours old at time of check).
- FAIL any row whose timestamp exceeds the allowed age.
- If no timestamp field exists and the contract requires freshness, FAIL with
  evidence "no timestamp field present to evaluate freshness."
- Evidence: cite the specific row/ID and its timestamp value, and the check-time
  reference used for the comparison.

## 3. Expected volume

- Compare total row count (excluding header) against the contract's minimum.
- FAIL if row count is below the minimum.
- WARN if row count is far above a sane upper bound implied by context (possible
  duplication injection or wrong source file) — note this even without an
  explicit contract ceiling.
- Evidence: state the exact row count and the contract's minimum/maximum.

## 4. Key uniqueness

- Identify the contract-designated key column(s) (or the column that best reads
  as a primary key/ID if no contract is supplied).
- FAIL if any key value appears more than once.
- Evidence: name every duplicated key value and the row numbers it appears on.

## 5. Full-row duplicates

- Independent of the key check: look for rows that are identical across every
  column (which may still have distinct keys, or may be the same key duplicated
  with identical values).
- FAIL if any full-row duplicate exists.
- Evidence: cite the row numbers and enough field values to make the duplication
  obvious to a reviewer without re-opening the file.

## 6. Required fields

- For each contract-designated required field, check every row for null/blank.
- FAIL any row where a required field is null or blank.
- Evidence: name the field, the row number, and the record's identifying key.

## 7. Nulls

- Report the null/blank rate (count and percentage) for every required and
  numeric field, even ones that already passed the required-fields check.
- This check is informational by default: WARN if any field's null rate is
  above 0% but the field isn't contract-required; FAIL only if the contract
  sets an explicit null-rate threshold and it's exceeded.
- Evidence: give the rate as "`n`/`total` rows (`x`%)" per field, not just a
  qualitative description.

## 8. Numeric rules

- Apply each contract-designated numeric rule (e.g. greater than zero, within a
  min/max range) to every row of the relevant field.
- FAIL any row that violates a hard numeric rule (e.g. negative revenue where
  the contract requires positive).
- Evidence: cite the row, ID, and the actual offending value next to the rule
  it violates.

## Hard-fail vs soft-issue classification

Used to compute the overall verdict in `SKILL.md`:

- **Hard-fail** (forces overall FAIL / BLOCK): duplicate keys, full-row
  duplicates, missing required fields, numeric rule violations, freshness
  violations, row count below contract minimum.
- **Soft issue** (drives WARN, not FAIL, unless the contract says otherwise):
  null rates on non-required fields, schema type mismatches where the column
  is still present and usable, row count above an implied (not contract-stated)
  ceiling.

When in doubt about which bucket a finding falls into, default to hard-fail —
it is safer to over-block a publish than to under-flag a real data issue.