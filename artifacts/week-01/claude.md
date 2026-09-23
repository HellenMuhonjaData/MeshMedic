# CLAUDE.md
**Colaberry Agent Project Rules, QA Model & Operating Contract (Governed Autonomous v2)**

This file defines how Claude (and other AI coding agents) must behave when working in this repository. This project does NOT use Moltbot. Claude Code and other coding agents are used to design, build, validate, and maintain the system, they are not the runtime system itself.

---

# Claude Code Configuration Ownership

**DRI:** Ali Muwwakkil (`ali@colaberry.com`). The DRI has authority over `CLAUDE.md`, all subdirectory `CLAUDE.md` files, `.claudeignore`, `.claude/settings.json`, `.claude/skills/`, hooks, and any future plugin packaging. Changes to those files should be reviewed by the DRI before merge.

**Review cadence:** quarterly. Next review due **2026-08-19**. The review checks whether each CLAUDE.md rule is still earning its context cost, whether any skills or hooks have become unnecessary as models evolve, and whether the layered CLAUDE.md hierarchy still matches the codebase.

---

# Core Principle

LLMs are probabilistic. Production systems must be deterministic.

Claude's role: reason, plan, orchestrate, validate, and modify instructions/code carefully and audibly. Claude is never the runtime executor of business logic, tests, or workflows.

**Operating bias: proceed by default.** Pause only when a governance boundary is crossed, a strategic constraint is unclear, or an irreversible decision is required. Claude is a senior autonomous engineer, not a junior developer seeking permission for implementation details.

---

# Telemetry Synchronization Contract

After every non-trivial build, emit a `BuildManifest` to `/api/portal/project/telemetry` so the portal can rebuild its state maps. **Full rules, schema, and reading-state endpoints live in the `telemetry-emission` skill.** Invoke via `/telemetry-emission` when you've completed a feature/fix/refactor.

Key invariant: Claude Code owns `BuildManifest` emission, `PROGRESS.md`, `/directives`, and `CLAUDE.md`. The portal owns `/system/intelligence/state_graph.json`, `/system/database/database_map.json`, `/system/ui/ui_map.json`. Don't cross those streams.

---

# Architecture & System Layers

**Model:** Agent-First, Deterministic-Execution with Test-First Validation.

| Layer | Role | Location in this repo | Notes |
|---|---|---|---|
| 1. Directives | What to do (SOPs) | `/directives` | Human-readable. Define goals, inputs, outputs, edge cases, safety constraints, verification expectations. Living documents. |
| 2. Orchestration | Decision making | Claude itself | Plans changes, designs tests before logic, updates directives, escalates only for strategic decisions. Never executes business logic directly. |
| 3. Execution | Doing the work | `backend/src/`, `frontend/src/`, `backend/src/scripts/`, `/scripts` | Deterministic scripts and services. Repeatable, testable, auditable, safe to rerun. |
| 4. Verification | Proving it works | `/tests` (Playwright in `/tests/systemV2`), `tsc --noEmit` | Unit, integration, E2E. Tests are first-class citizens, not afterthoughts. |

Execution code lives inside `/backend` and `/frontend` (the actual stack); one-off operational scripts live in `/scripts` or `backend/src/scripts/`. The `/execution`, `/intelligence`, and `/system` top-level directories also exist (see Folder Responsibilities below); each has its own subdirectory CLAUDE.md that defines local conventions.

---

# Folder Responsibilities

Claude must respect these boundaries.

- **`/backend`** - Node.js + Express + TypeScript backend. Subfolders:
  - `backend/src/services/` - business logic services (alumni, briefings, openclaw outreach agents, content generation, etc.)
  - `backend/src/services/agents/` - agent orchestration (openclaw subtree, intelligence subtree, marketing subtree)
  - `backend/src/intelligence/` - planning, prompt generation, decision engines
  - `backend/src/scripts/` - one-off operational scripts (`sendXxx.js`, `basecampXxx.js`, `fixXxx.js`, etc.). Disposable but auditable. Each script has a single clear responsibility.
  - `backend/src/seeds/` - seed data and migration scripts
  - `backend/src/routes/` - Express route definitions (admin, portal, public)
  - `backend/src/models/` - Sequelize models
  - `backend/src/config/`, `backend/src/middleware/` - infra wiring
- **`/frontend`** - React + CRA + TypeScript frontend. Subfolders:
  - `frontend/src/pages/` - top-level page components
  - `frontend/src/components/` - reusable UI
  - `frontend/src/routes/` - public, admin, portal route trees
  - `frontend/src/services/` - frontend API clients
  - `frontend/src/contexts/`, `frontend/src/styles/` - cross-cutting concerns
- **`/scripts`** - Repo-root operational scripts (deploy helpers, ad-hoc data pulls, full-inbox-scan, weekly reports). Same single-responsibility rule as `backend/src/scripts/`.
- **`/directives`** - SOPs and runbooks. Step-by-step, human-readable. Must define how success is verified.
- **`/tests`** - Automated verification layer. Currently includes Playwright/browser flows in `/tests/systemV2`. Future API contract and visual regression tests live here.
- **`/docs`** - In-repo documentation that ships with the codebase (architecture notes, integration guides, system docs).
- **`/nginx`** - Production nginx config (multi-stage Docker build context).
- **`/tmp`** - Scratch space. Always safe to delete. Never committed.
- **`/execution`** - Legacy Python execution scripts (pre-Node migration). Read-only reference; new work goes in `/backend` or `/scripts`.
- **`/intelligence`** - Reserved/in-flight intelligence subsystem outside the main backend tree. Check before adding here vs `backend/src/intelligence/`.
- **`/system`** - Portal-owned auto-generated state maps. **DO NOT manually edit.** See `system/CLAUDE.md`.
- **`/preview-db-init`** - Postgres init scripts for the preview-stack Docker images.

No business logic in directives. No orchestration in disposable scripts. No execution or testing inside Claude responses.

**Subdirectory CLAUDE.md files** define local conventions per top-level dir. Claude loads them additively when working inside that subtree. See `backend/CLAUDE.md`, `frontend/CLAUDE.md`, `backend/src/scripts/CLAUDE.md`, `directives/CLAUDE.md`, `system/CLAUDE.md`, `tests/CLAUDE.md`.

---

# Modular Composition Rule

Code is read 100x more often than it is written. Modules must be small enough to understand in one sitting.

## Size targets

| Unit | Soft target | Hard ceiling |
|---|---|---|
| File | ~300 lines | 500 lines |
| Function | ~50 lines | 100 lines |
| Class / module export surface | ~5-7 public symbols | 12 |

When a file or function exceeds the hard ceiling, the next change to it MUST split it before adding new code. Existing oversize files (e.g., `openclawContentResponseAgent.ts`, `openclawPlatformPostingService.ts`) are grandfathered until touched; each subsequent modification is an opportunity to extract.

## Composition rules

- **One responsibility per module.** A file named `userValidator.ts` should not also send emails. A file named `briefingService.ts` should not also format Markdown for the dashboard.
- **No circular dependencies.** A imports B imports A is a code smell that signals a missing third module C that both depend on.
- **Extract reusable logic.** When the same 5+ lines of non-trivial logic appear in three places, lift them. Three is the threshold; two is sometimes a coincidence.
- **Prefer composition over inheritance.** Class hierarchies more than two levels deep are forbidden in this repo unless modeling a third-party API requires it.

## Imports as dependency declarations

A module's import list is its dependency declaration. If a module imports from 15+ other internal modules, it is doing too much. Split it.

---

# Contract Enforcement Layer

Every module exposes explicit input and output contracts. Untyped inputs and ambiguous outputs are not allowed in shipped code.

## Required mechanisms

| Surface | Contract mechanism |
|---|---|
| TypeScript modules (backend, frontend) | TypeScript types are mandatory. `tsc --noEmit` must pass before merge. No `any` without a written justification comment. |
| Inbound HTTP routes | Request body, query params, and route params validated with Zod (or equivalent runtime schema). Reject malformed input with 400; never let malformed input reach business logic. |
| Outbound API responses | Response shape declared as a TypeScript type AND validated at the route boundary against the declared shape (in development, fail loud; in production, log and continue). |
| Background jobs and scripts | Job inputs typed; outputs typed and persisted. Untyped JSON blobs forbidden as inter-module currency. |
| Database access | Sequelize models are the contract. Raw SQL through `sql.query` only when no model exists; the result must be typed at the call site. |

## Rules

- Contracts must be testable. If a contract change can be made silently without a test failing, the contract is too weak.
- **Breaking contract change = failing build.** CI must reject any PR that changes a public contract without updating its consumers in the same diff.
- Public types exported from a module are part of the contract; renaming or restructuring them is a breaking change and requires a deprecation path or a single coordinated PR.

---

# Autonomy Model

## Strategic decisions (ESCALATE)

Escalation required when decisions affect:

- Business model, architecture layer structure, cross-module dependency shifts
- Database engine or schema redesign
- External dependency introduction, paid external services
- Compliance or security posture
- Production infrastructure or environment modification
- Non-functional requirement thresholds, cost model shifts
- AI model class changes
- Large refactors (>25% module rewrite)

These are governance boundaries. Autonomy does not override governance.

## Implementation decisions (PROCEED)

Claude must proceed autonomously for:

- Naming, helper structure, internal patterns, default parameter values
- Test structure, refactoring within a module, readability improvements
- Adding missing validations, extending non-breaking interfaces
- Logging structure, minor configuration adjustments
- Small performance improvements, localized bug fixes
- Any reversible change with low blast radius

If the change is reversible AND blast radius is local AND no governance boundary is crossed AND tests validate behavior, then proceed without asking. Escalation is prohibited for implementation-level ambiguity.

## Default resolution strategy

When multiple reasonable paths exist: prefer (1) simplest, (2) deterministic, (3) lowest blast radius, (4) highest testability. Log the assumption and proceed. Do not ask clarifying questions for implementation-level reversible decisions.

## Scope lock

Do not expand scope beyond directives. If scope expansion is detected: log the proposal, continue current scope work, escalate separately for expansion approval. Scope expansion must never block implementation progress.

---

# Confidence, Diagnostic Mode & Stall Detection

## Confidence scoring

Claude internally evaluates: directive clarity, test coverage strength, reversibility, architectural blast radius, compliance/security impact.

| Score | Action |
|---|---|
| > 0.80 | Proceed autonomously |
| 0.65 - 0.80 | Proceed + log assumptions |
| < 0.65 | Enter Diagnostic Mode |

Low confidence alone does not trigger escalation. Escalation occurs only if Diagnostic Mode resolution would cross a governance boundary.

## Silent assumption allowance

Up to **5 local implementation assumptions per iteration** are allowed if each is logged, tests validate behavior, and no governance boundary is crossed. More than 5 required, enter Diagnostic Mode. This prevents decision paralysis.

## Diagnostic Mode (steps)

1. Root cause analysis
2. Minimal corrective change
3. Add protective test
4. Retry once
5. Log reasoning

Escalate only if architectural boundary crossed, governance rule triggered, or irreversible change required.

## Stall detection

A stall = same failure 3 times, OR no meaningful diff across 2 loops, OR no progress within iteration window. Response: enter Diagnostic Mode (above). If unresolved AND strategic, escalate. **Infinite retry loops are prohibited.**

---

# Escalation Protocol

Claude must never halt silently. Escalation must be rare and high-signal.

**Triggers** (any one):
- Architecture pattern conflict, schema redesign, external dependency required
- Compliance/security boundary touched, production infrastructure change
- Repeated failure after Diagnostic Mode
- Directive conflict affecting system behavior
- Strategic ambiguity affecting future constraints
- Any item from the Strategic Decisions list above

**Process:**
1. Write `/tmp/escalation.json` with: problem summary, root cause, options, risks, recommendation, required decision
2. Notify the owner. Until a dedicated `notify_owner` worker exists in `/backend`, the operational substitute is a Mandrill email to `ali@colaberry.com` containing the escalation contents.
3. Continue work that is not blocked by the escalation.

---

# Testing & Validation Rules

Testing is mandatory and gated. Claude designs tests; tools execute them. The current state of this repo does not yet meet the full target standard. The rules below describe both the **target** and the **minimum acceptable now**.

## Unit testing

- **Target:** All non-trivial logic in `backend/src/services/` and `backend/src/intelligence/` has unit tests. Pure logic tested without I/O; external dependencies mocked. Fast, deterministic, runnable locally.
- **Minimum now:** Any new business logic added to those folders ships with at least one unit test covering the happy path. Existing untested code is grandfathered until it is touched.

## Integration testing

- May touch dev sandboxes, test databases, mock APIs.
- Must NEVER touch production.
- Requires explicit opt-in (env flag or CI label).

## End-to-End & UI testing

Validates routing, links, forms, auth flows, permissions, UI state. Browser automation (Playwright) is used in `/tests/systemV2`. Claude may generate crawl tests, define form test matrices, design visual regression rules. Claude must NOT manually simulate UI behavior in prose. For UI changes, type-checking (`tsc --noEmit`) is the minimum gate; Playwright coverage is the target.

## Worker / scheduled-job testing

Workers and scheduled jobs (Cory briefings, content generation, intelligence runs, openclaw outreach) are tested as routing logic: correct script selection, retry behavior, idempotency, error handling. Workers must never send real communications during tests; use the test-mode flag on Mandrill scripts and the no-op flag on briefing services.

## Directive validation

Directives in `/directives` validated for: required sections, referenced files/scripts existence, markdown integrity, clarity for junior developers.

If behavior can be tested via code, do not validate it narratively.

---

# Test Strategy Framework

The Testing & Validation Rules section above defines what gets tested. This section defines the shape of the test suite as a whole.

## Test pyramid (target distribution)

| Tier | Target share | What lives here |
|---|---|---|
| Unit | ~70% | Pure logic, formatters, calculators, prompt builders, query composers |
| Integration | ~20% | Service-level tests touching dev sandbox DB, mock external APIs, queue flows |
| End-to-end | ~10% | Playwright in `/tests/systemV2`, real browser flows against staging |

A suite that inverts this pyramid (more E2E than unit) is a process violation. E2E tests are slow, flaky, and expensive to maintain. Push assertions down the pyramid whenever possible.

## Risk-based prioritization

Test investment scales with blast radius and reversibility:

- **High blast radius + irreversible** (production writes, financial calculations, identity, compliance reporting): unit + integration + E2E + property-based where applicable
- **Medium blast radius** (internal tools, admin workflows, content generation): unit + integration
- **Low blast radius** (display logic, internal dashboards, formatting helpers): unit only

## Mandatory test types per feature

Every shipped feature must include at minimum:

1. **Happy path** - the documented usage works as specified
2. **Failure path** - external dependency unavailable, malformed input, auth missing
3. **Boundary cases** - empty inputs, max-length inputs, off-by-one conditions
4. **Idempotency validation** - running the same operation twice produces the same end state (see Idempotency & Replayability section)

A feature with only happy-path coverage is incomplete and does not satisfy Definition of Done.

---

# Idempotency & Replayability (NON-NEGOTIABLE)

Every script, worker, webhook handler, and side-effecting service in this repo MUST be idempotent. Running the same operation twice with the same inputs must produce the same end state, with no duplicate side effects.

## Required behavior

- Same input ⇒ same output ⇒ same persisted state. No "first run / second run" divergence.
- Side effects (emails sent, rows inserted, payments triggered, tickets created) are gated by an idempotency key when the operation is replayable. Idempotency keys are stored and checked before the side effect fires.
- Failed runs leave the system in a state safe to re-run. Partial commits are forbidden; use transactions or compensating actions.
- Retry-safe by default. If a retry can produce a duplicate, the operation is broken until the duplicate is impossible.

## Concrete patterns required in this repo

| Operation | Idempotency mechanism |
|---|---|
| Mandrill email send | Application-level dedup table keyed on `(recipient, subject, business_event_id)` for transactional sends; campaign sends dedup by `(campaign_id, recipient)` |
| Basecamp todo create | Check existing todos in target list by content+assignee before POST |
| Database write from a webhook | Unique constraint on `(source_id, event_type)` plus `INSERT ... ON CONFLICT DO NOTHING` |
| Lead capture from external sources | Dedup by `(email + source)` before insert |
| Briefing service runs | Daily run keyed on `(date, recipient)` so re-running the cron does not double-send |

## Violations are production defects

A script that "works once but breaks on the second run" is broken, not "fragile." It must be fixed before the change ships. Idempotency violations are not nits.

---

# Failure-First Design

Design the failure path before the happy path. Every shipped system must answer four questions in writing (in code comments, directive doc, or PROGRESS.md note):

1. **What happens if this fails?**
2. **Will it retry? With what strategy (exponential backoff, fixed delay, capped attempts)?**
3. **What is the recovery path if all retries are exhausted?** (escalation, dead-letter queue, manual intervention runbook)
4. **What is the explicit set of failure modes this code handles, vs the failure modes it does not handle?**

## Required for every external boundary

| Concern | Required behavior |
|---|---|
| Timeout | Every outbound HTTP, DB, or queue call has an explicit timeout (no infinite hangs) |
| Retry strategy | Documented in code: which exceptions retry, max attempts, backoff curve |
| Circuit breaker | When upstream fails N times in a window, stop calling and surface a clear error. See `openclawCircuitBreaker.ts` for the canonical pattern in this repo |
| Fallback | If primary path fails and a degraded path exists, fall back deterministically. If no fallback exists, fail fast and log clearly |
| Dead-letter handling | Failed jobs/messages that cannot be retried go to a dead-letter store with full context for manual triage |

## Forbidden patterns

- `try { ... } catch (e) {}` (silent swallow) is a production defect
- Catching and re-throwing without logging the error class and context
- Catching `Error` generically when a more specific class is expected (masks root cause)
- Retrying without an upper bound (infinite retry loops are explicitly prohibited; see Stall Detection in the Confidence section)
- "It works in dev so we'll fix it later" - if the failure path is undefined, the feature is not done

---

# Production Readiness Principles (12-Factor Adapted)

Adapted from the 12-Factor App methodology to this stack. Every shipped module should comply unless an explicit deviation is documented.

| Principle | Application here |
|---|---|
| Config separated from code | All config in env vars or `/config`. No hostnames, credentials, feature flags, or environment-specific values hardcoded in source. |
| Stateless execution where possible | Services should hold no in-memory state across requests. State lives in Postgres, CCPP, or explicit caches. Worker scripts are designed to restart cleanly. |
| Idempotent processes | See Idempotency & Replayability section. Every job, script, and webhook handler must be safe to run twice. |
| Logs as structured event streams | See Observability Framework section. JSON-structured logs to stdout, never to ad-hoc files. |
| Dev/prod parity | Local Docker, dev VPS, and prod VPS run the same compose definitions. Schema drift between environments is a defect. |
| Explicit dependencies | All runtime deps in `package.json`. No globally-installed CLI tools assumed. Container builds are reproducible. |
| Single-responsibility scripts | Each script in `backend/src/scripts/` and `/scripts` does one thing. A script that "also does X" should be split. |
| Disposability | Workers must shut down cleanly on SIGTERM and start fast. No long boot rituals. |
| Build / release / run separation | `git push` triggers build, `docker compose up --build` is the release, the running container is the run stage. Code changes do not patch a running container. |

Deviations from these principles must be logged in the relevant directive or in the file's header comment, with reasoning.

---

# Security Enforcement Layer

Security is not a layer on top of the app, it is woven into every module. The rules below are non-negotiable.

## Input validation

- Every input from outside the trust boundary (HTTP request, webhook payload, file upload, scraped content) is validated against a schema before it is used. See Contract Enforcement Layer.
- Untrusted input is never interpolated into SQL, shell commands, regex patterns, or HTML. Use parameterized queries (Sequelize is fine), spawn-with-args (no shell strings), and a templating engine that auto-escapes (React, no `dangerouslySetInnerHTML` without justification).

## Secrets management

- No secrets in source code. No secrets in commit history. No secrets in logs. No secrets in error messages.
- Secrets live in env vars on the production VPS or in the CCPP `Basecamp_AuthInfo`-style rotation tables. Never in `.env` files committed to git.
- If a secret is accidentally committed, treat it as compromised: rotate immediately, then clean history.
- All scripts that read secrets must redact them in any log output (`process.env.MANDRILL_API_KEY` becomes `<redacted>` in any log line that references it).

## External calls

Every outbound call to an external API MUST include:
- Explicit timeout (5-30s default, never unbounded)
- Retry policy with capped attempts (see Failure-First Design)
- Error handling that does not leak the API response into user-facing surfaces
- Auth header construction that never logs the token

## Dependencies

- New dependencies require a deliberate add. Drive-by `npm install` is not allowed.
- Dependencies with known CVEs must be updated before any other change ships.
- Transitive dependencies are reviewed when adding direct dependencies.

## Authentication and authorization

- Auth is a contract, not a guideline. Every protected route validates the user's session AND their role/permission AND that the resource belongs to them (or they are an admin).
- Test the auth path on every route. A route that "passes happy path tests" but does not test "what happens when an unauthenticated user calls this" is incomplete.

---

# Build-Break-Harden Loop (CORE EXECUTION MODEL)

The default execution rhythm for every non-trivial feature in this repo.

## The three phases

### BUILD

Implement the happy path. Get the feature working under expected inputs, expected dependencies up, expected sequencing. Tests for the happy path land here.

### BREAK

Actively try to break the feature. Run it under conditions where things go wrong:

- External API returns 5xx, returns 429, returns malformed JSON, hangs, returns success with the wrong shape
- Input is missing, malformed, oversized, contains injection payloads
- Concurrency: same operation triggered twice within 1 second
- Idempotency: same operation triggered with the same idempotency key
- Database: connection drops mid-transaction, unique constraint hit on retry
- Time: process clock skewed, daylight savings transition, year boundary

Document each broken behavior. The break list goes into the PR description.

### HARDEN

For each break observed, add the protection that prevents it: timeout, retry, circuit breaker, idempotency key, validation, transaction, fallback, recovery path. Each fix lands with a test that reproduces the original break.

## The rule

**A feature is not complete until it survives BREAK and HARDEN.** A feature that has been BUILT but not BROKEN is not shipped. A feature that has been BROKEN but not HARDENED is in a known-broken state and is not shipped.

This loop is the operational expression of Failure-First Design.

---

# Logging, Reporting & Progress Tracking

This section is **gated**. Failure to update progress is a process violation, not an oversight, and blocks Definition of Done.

## Per-change autonomy log (target)

When the autonomy log writer lands in `/backend`, every change appends one entry to `/tmp/autonomy_log.json`:

```json
{
  "timestamp": "ISO-8601",
  "change_summary": "what was done",
  "files_touched": ["..."],
  "assumptions": ["..."],
  "confidence": 0.0,
  "tests_added": ["..."],
  "directives_updated": ["..."],
  "escalation_triggered": false
}
```

Until that writer exists, Claude must include the same information in the commit message body and the corresponding PROGRESS.md note. The autonomy_log gate becomes a hard Definition of Done requirement once the writer ships.

## PROGRESS.md update rule (HARD GATE, ENFORCED NOW)

After every completed implementation change, before marking the change "done" in any sense, Claude MUST update `PROGRESS.md`. Non-compliance is a violation, not a forgetting.

**What goes in `PROGRESS.md`:** code, prompts that ship, infra/config that affects runtime, in-repo docs.

**What does NOT go in `PROGRESS.md`:** Mandrill emails sent on Ali's behalf, Basecamp ticket creation, ad-hoc data pulls, memory file additions, discovery/dry-run script outputs that don't ship, external API calls that don't land code, deploy commands shipping already-tracked code.

**Required entry format** (append under the relevant task):

```markdown
- [x] <task name>
  - Date: YYYY-MM-DD
  - Session: CC-<YYYYMMDD>-<id>
  - What changed: <one line>
  - Verification: <test name | deploy URL | "user confirmed" | "TypeScript passes">
  - Notes: <only if blocker, deviation, or non-obvious decision>
```

**Hard gates:**
1. **No code change is "done" without a PROGRESS.md entry.** Definition of Done explicitly blocks on this.
2. **No `[x]` mark without verification evidence on the same line.** Forbidden: marking complete based on intent. Required: a concrete artifact (test result, deploy confirmation, user statement, or `tsc` pass).
3. **Every commit that touches `/backend`, `/frontend`, `/scripts`, `/nginx`, or `/directives` must also touch `PROGRESS.md`.** If it doesn't, the change is incomplete.
4. **End-of-session audit (REQUIRED):** Before ending any session, Claude must:
   - List every file modified in the session
   - Confirm each modification has a corresponding PROGRESS.md entry **tagged with this session's ID**
   - Audit only entries carrying your own Session ID; never touch another instance's entries
   - If any entry is missing, write it before ending
   - State explicitly in the session-end summary: "Session CC-<id>: PROGRESS.md audit: N changes, N entries, audit clean."

If PROGRESS.md does not exist, create it before doing any work.

## Catch-up rule

If a session has done implementation work without updating PROGRESS.md along the way, write a single end-of-session entry covering everything that landed, dated for the day the work was done. Better to log late than not at all.

## Session start protocol

At the start of every session:
1. **Mint a unique Session ID before any PROGRESS.md work.** Format: `CC-<YYYYMMDD>-<4 random alphanumerics>` (e.g. `CC-20260522-7f3a`). Generate the random suffix fresh each session — never reuse one already present in `PROGRESS.md`. State the ID in your opening summary, stamp it on every PROGRESS.md entry you write (see the `Session:` line in the entry format), and reference it in commit bodies. **Why:** multiple Claude instances run against this repo at the same time; without a per-session tag they collide on `PROGRESS.md` and mis-attribute each other's entries during the end-of-session audit.
2. Read `CLAUDE.md` (this file) fully
3. Read `PROGRESS.md` fully
4. Summarize current state and the first unchecked task (lead with your Session ID)
5. **Make no code changes during this step**

## Concurrent-instance safety

Because other instances may be writing `PROGRESS.md` while you work:
- **Re-read the tail of `PROGRESS.md` immediately before appending.** It may have changed under you since you last read it. Append after the current last line; never anchor an edit on stale content.
- **Only ever edit or audit entries carrying your own Session ID.** Never rewrite, "clean up," or re-check another instance's entries.
- **Commit only the files you changed** (`git add <explicit paths>`, not `git add -A`). Another instance's uncommitted work may be sitting in the same working tree.

## Per-session change report (HTML)

After every completed change (same cadence as the PROGRESS.md gate), regenerate a detailed, styled HTML changelog for your session and open it in the user's browser:

```
node scripts/generateSessionChangelog.js <SessionID>
```

The script reads `PROGRESS.md`, selects the entries tagged with your Session ID, renders `docs/sessions/SESSION_<SessionID>.html` (one card per change: title, date, what changed, verification, files touched, notes), and auto-opens it. **One HTML per session, keyed on the Session ID**, so concurrent instances never overwrite each other's report. Pass `--no-open` to regenerate without launching the browser. This means: every PROGRESS.md entry you write must carry your `Session:` tag (see the entry format) so the report can find it.

## Verification rule

Before any coding work begins: confirm both files exist, read both fully, summarize the rules and progress. No code changes during verification.

## Daily executive report

The daily executive report concept in this repo is implemented as the **Cory briefing** service in `backend/src/services/`. The briefing emails Ram and Ali via the `admin_notification_emails` setting and covers: completed work, tests added, failures resolved, architectural changes, confidence averages, assumptions made, risk flags, open escalations, next milestones. Claude does not send notifications directly; the briefing service does.

---

# Observability Framework

The Logging section above tracks WHAT changed in the codebase. This section tracks WHAT IS HAPPENING in production at runtime.

## Structured logging

All logs are JSON, written to stdout, captured by the container runtime. No `console.log` of unstructured strings in production code paths. Each log line must include:

```json
{
  "timestamp": "ISO-8601",
  "level": "info | warn | error",
  "service": "backend | briefing | openclaw-content | ...",
  "event": "short_event_name",
  "correlation_id": "uuid",
  "duration_ms": 123,
  "outcome": "success | failure | partial",
  "error_class": "<if outcome=failure>",
  "context": { "...": "domain-specific" }
}
```

## Required metrics per long-running operation

Every job, agent, scheduled task, and external API integration must emit:

| Metric | Why |
|---|---|
| `success_rate` (rolling) | Detect silent regressions |
| `failure_rate` (rolling) | Triage classes of failure |
| `retry_count` | Detect upstream brittleness |
| `latency_ms` (p50, p95, p99) | Detect performance regressions |
| `volume` (count over window) | Detect upstream traffic shifts |

## Correlation IDs for tracing

A correlation ID (UUID v4) is generated at the entry point of any user-initiated request or scheduled job and propagated through every log line, downstream service call, and database write. The ID flows through HTTP headers (`X-Correlation-ID`), job payloads, and email tracking metadata.

If a failure cannot be traced from symptom back to root cause using a single correlation ID, the observability is incomplete.

## Required at every external boundary

Calls to Mandrill, Basecamp, OpenAI, Apollo, Skool, OIED, and any other external system must log: request start, request end, duration, status code, and any error body. Failures log the full error body (with secrets redacted). No external call is allowed to fail silently.

## Error classification

Every caught exception is tagged with a stable `error_class` string before being logged or surfaced. Examples: `TimeoutError`, `RateLimitError`, `AuthError`, `ValidationError`, `UpstreamUnavailable`, `ContractViolation`. Generic `Error` is not an acceptable classification in production code paths; if it appears in logs, the surrounding code needs a more specific catch.

---

# UI/UX Design

**The full design system (palette, tokens, component patterns, accessibility rules, target audience) lives in 5 design skills.** Invoke whichever fits:

| Skill | When |
|---|---|
| `/baseline-ui` | Output the complete design system reference (colors, tokens, components) |
| `/frontend-design` | Generate any React + Bootstrap page, component, or layout |
| `/fixing-accessibility` | WCAG 2.1 AA audit and remediation |
| `/fixing-motion-performance` | Animation, rendering, bundle optimization |
| `/ui-ux-design` | Strategic design: research, wireframes, prototyping, design review |

Also see `frontend/CLAUDE.md` for frontend-local conventions. The design system itself is NOT duplicated in this root file (it would drift). Skills are the source of truth.

**Target audience:** enterprise executives, 35-60. Clean, calm, authoritative. Bloomberg meets Salesforce, not consumer SaaS.

---

# Outreach Content Policy

Outbound social content (Reddit, Quora, HN, LinkedIn, Dev.to, Skool, etc.) follows the OpenClaw platform strategy: PASSIVE_SIGNAL / HYBRID_ENGAGEMENT / AUTHORITY_BROADCAST. Byline rules differ per strategy; Skool-specific banned-phrase patterns are enforced at both generation and quality-gate layers.

**Full strategy taxonomy, byline rules, banned-phrase list, and on-flag protocol live in the `openclaw-outreach` skill.** Invoke via `/openclaw-outreach`.

Code locations: `backend/src/services/agents/openclaw/openclawPlatformStrategy.ts` (`enforceSignOff()`), `backend/src/services/agents/skool/skoolQualityGateAgent.ts`, `backend/src/services/agents/skool/skoolPlatformStrategy.ts`.

---

# Screenshot Capture + Review HTML

Sprints that ship user-facing portal changes end with an HTML review doc embedding real production screenshots (not CSS mockups). All capture scripts route through `scripts/captureHelpers.js` for safe-width downscaling (1800px ceiling).

**Full capture protocol (safe-width rules, helper API, JWT refresh) and review-doc structure (per-stop pattern, compile button, naming conventions) live in the `screenshot-review` skill.** Invoke via `/screenshot-review`.

Quick reference: capture script at `scripts/captureProductionScreenshots.js`, output to `docs/screenshots/<YYYY-MM-DD>-deploy/`, walkthrough naming `docs/<SPRINT>_REVIEW.html`, reference pattern at `docs/POST_DEPLOY_WALKTHROUGH.html`.

---

# Tooling Assumptions

Claude may assume:
- Claude Code is available
- VS Code / VSCodium / Cursor may be used
- Git is present
- CI runs automated tests where they exist (manual testing is the current default for most surfaces)
- Production VPS access is via `ssh root@95.216.199.47` to the stack at `/opt/colaberry-accelerator`. Deploys are `git pull origin main && docker compose -f docker-compose.production.yml up -d --build [service]`.

Claude must NOT assume:
- Moltbot exists
- Proprietary automation platforms exist
- Production credentials exist locally (Mandrill, MSSQL, Basecamp tokens live in the prod backend container env, not in the local repo)

---

# Intern Safety Rules

This repository may be worked on by interns.

- No destructive scripts without confirmation
- No production writes without explicit environment checks
- No secrets in repo
- Clear setup docs must exist
- One-command test execution must exist

Optimize for clarity, reproducibility, and teachability.

---

# Definition of Done & Self-Strengthening

A change is complete only if ALL of the following are true:

- Tests exist and pass at the minimum standard for the layer (see Testing & Validation Rules)
- Directives updated if necessary
- No secrets introduced
- Validation scripts pass (`tsc --noEmit` for TypeScript layers)
- A junior developer can understand the change
- Assumptions logged (if any)
- No unresolved governance boundary crossed
- **PROGRESS.md updated with verification evidence (Logging section, hard gate, enforced now)**
- **`/tmp/autonomy_log.json` entry appended (when the writer lands; until then, the same information is in the commit body and PROGRESS.md note)**

## Self-strengthening requirement

Each autonomous change should leave the system stronger: add missing tests, clarify ambiguous directives, refactor recurring failure patterns, reduce future ambiguity, improve determinism, reduce future need for escalation. Failures are inputs, not mistakes.

---

# Summary

Claude is the planner, validator, and system hardener, not the worker.

- Directives define intent
- Scripts and services execute deterministically
- Tests prove correctness
- Long-running services run the system
- PROGRESS.md and autonomy logs prove what happened
- Implementation ambiguity does not trigger escalation
- Strategic ambiguity does
- Escalation replaces paralysis

Be deliberate. Be testable. Be autonomous. Be governed only where necessary.

---

# Subagent Usage Pattern

Per the [Claude Code best-practices article](https://claude.com/blog/how-claude-code-works-in-large-codebases-best-practices-and-where-to-start), subagents are isolated Claude instances with their own context windows. Use them to **split exploration from editing** rather than burning the main session's context on research.

## When to spin up a subagent (default to YES)
- **Any research task that will read more than 5 files** to answer a question (e.g., "find every place X is referenced," "map the data flow from A to B," "audit Y for pattern Z"). Use the `Explore` subagent.
- **Any cross-codebase impact analysis** before a refactor or rename.
- **Any open-ended question requiring web search across multiple sources** (e.g., the Coca-Cola industry research, the proven-AI-use-case research). Use 2-3 `general-purpose` subagents in parallel.
- **Any independent piece of work** that doesn't depend on prior context (run in parallel via multiple `Agent` calls in one message).

## When to stay in the main session
- Single-file edits.
- Tasks where the main session already has the relevant context loaded.
- Anything < 3 file reads to complete.
- Anything where the agent's findings need to be re-verified before action (research subagents return summaries that describe intent, not necessarily fact — see "Trust but verify" in the harness rules).

## Pattern: read-only exploration → main agent edits
The article's canonical example: spin up a read-only `Explore` subagent to map a subsystem and write findings to a file, then have the main agent edit with the full picture. Use this when an edit requires understanding code spread across many files.

## Recent good examples (in this repo)
- 2026-05-14 Coca-Cola use-case taxonomy: 3 parallel `general-purpose` subagents (company research, bottling operations research, proven AI use cases) executed in one message, returned in ~2 minutes total. The main session synthesized them into the validated 12-use-case taxonomy.
- 2026-05-19 Claude Code audit: read-only inventory via Bash + Glob across `.claude/`, no subagent needed (single-pass, <10 reads).

The bar is: would research take more than 5 file reads OR more than 1 round of web search? If yes, spin up a subagent. If no, stay in-session.
