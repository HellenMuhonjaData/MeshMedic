# Common ETL Failure Reference

Evidence signatures for the failure classes this skill screens for. Read
this before ranking causes in `SKILL.md`. For each class: what it looks
like in a log, what evidence confirms it, how to tell it apart from
neighboring causes, and a safe (read-only) next diagnostic step.

## 1. Schema mismatch / drift

- **Looks like:** a source field that's missing, renamed, resized, or
  contains values outside the set the pipeline expects (an enum/domain
  that gained a new member, a column that changed type, a new column
  appearing unannounced).
- **Confirming evidence:** a schema-check or validation log line naming
  the specific field and the unexpected value(s) or missing column; a
  count of affected rows; sample record IDs.
- **Distinguish from:** a conversion/mapping failure (below) — schema
  drift is the upstream *cause*, conversion/mapping failure is often the
  downstream *symptom* that shows up a step later once the pipeline tries
  to process the drifted field. Both frequently appear together in the
  same incident; cite them as separate facts even when they share a root
  cause.
- **Safe next step:** compare the current source extract's schema/domain
  against the last known-good extract or the documented contract; do not
  alter the source or the pipeline's schema definition to "fix" it.

## 2. Conversion / mapping failure

- **Looks like:** a cast error, a NOT NULL constraint violation on a
  derived/mapped column, a lookup-table join that returns NULL for one or
  more input values, a type coercion exception.
- **Confirming evidence:** the specific error class (e.g.
  `MappingLookupError`, `ConversionError`, `NotNullConstraintViolation`),
  the target column/table named in the error, the lookup/mapping table
  and its version or last-updated timestamp, and the specific input
  value(s) that failed to map or convert.
- **Distinguish from:** a plain data-quality issue (missing/null source
  data) — a conversion/mapping failure happens on a value that *is*
  present in the source but has no valid target representation yet
  (typically because a mapping/lookup table hasn't been updated to include
  it). If the source value is itself null or blank, that's a data-quality
  fact, not a mapping fact — cite it separately.
- **Safe next step:** check whether the specific unmapped/unconvertible
  value exists in the current lookup/reference table; check when that
  table was last updated relative to when the new value started
  appearing in the source.

## 3. Retry that did not resolve the problem

- **Looks like:** two or more run attempts in the log with the same job
  name, the same failing step, and the same error class/message, separated
  by the configured retry backoff interval.
- **Confirming evidence:** attempt numbers, timestamps of each attempt,
  and confirmation that the error signature (error class + failing
  field/value) is identical across attempts.
- **Why it matters:** an identical failure across a retry is strong
  evidence the root cause is **not** transient (not a network blip, not a
  momentary resource contention) — it's a structural condition (schema,
  mapping table, code, or config) that a bare retry cannot fix. Rank this
  higher in confidence than a single-attempt failure would justify, but do
  not treat "retry failed" itself as the root cause — it's confirming
  evidence for whatever structural cause the earlier steps point to.
- **Safe next step:** confirm the retry used the same inputs/config as the
  first attempt (rule out a config or environment difference between
  attempts) before concluding the cause is structural rather than
  environmental.

## 4. Connectivity / timeout

- **Looks like:** connection refused/reset errors, DNS failures, TLS
  handshake failures, or a step that exceeds its configured timeout with
  no other error detail.
- **Confirming evidence:** the specific host/service the call was made
  to, the configured timeout value, and the elapsed duration at failure.
- **Distinguish from:** a downstream service returning a valid error
  response (e.g. HTTP 429/5xx) — that's an upstream-service failure, not
  connectivity, and should be classified by the status code and response
  body, not lumped in with timeouts.
- **Safe next step:** check whether other jobs hitting the same
  host/service in the same time window succeeded or failed, to isolate
  whether the issue is host-specific or pipeline-specific.

## 5. Resource / capacity limits

- **Looks like:** out-of-memory errors, disk-full errors, connection-pool
  exhaustion, or a step that slows dramatically before failing.
- **Confirming evidence:** the specific resource named in the error
  (memory, disk, connections), the limit configured, and the value
  reached at failure time.
- **Safe next step:** check whether row/data volume for this run was
  unusually large relative to recent runs (a volume spike is a common
  trigger for resource-limit failures and points at a different fix than
  a code defect would).

## 6. Upstream data anomaly (not a schema/mapping issue)

- **Looks like:** the pipeline runs to completion but produces a row
  count, value distribution, or aggregate that is implausible relative to
  history — no hard error, just suspicious output.
- **Confirming evidence:** the specific metric that's off (row count,
  sum, null rate) and the historical baseline it's being compared against.
- **Distinguish from:** the other classes above — this is a "no error
  thrown but the output looks wrong" case, so the evidence is comparative
  (this run vs. prior runs), not a log error line.
- **Safe next step:** compare this run's summary metrics (row count, key
  aggregates) against the last several successful runs from metadata, not
  just the immediately prior one — a single prior run can itself be
  anomalous.

## Confidence guidance

- **High confidence:** the log shows an explicit, specific error tied to
  one class above, and run metadata corroborates it (e.g. a mapping table
  version that predates a new source value).
- **Medium confidence:** the log is consistent with a class above but
  lacks a corroborating metadata fact, or multiple classes are plausible
  from the same evidence.
- **Low confidence:** the evidence is circumstantial or only the symptom
  (not the mechanism) is visible in the log. Say so explicitly rather than
  forcing a High/Medium rating.