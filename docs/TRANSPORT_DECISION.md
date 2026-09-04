# Transport & State Model Decision

**Status:** Proposed — awaiting sign-off before implementation.
**Date:** 2026-09-04
**Applies to:** `meshmedic-mcp` (the only MCP server in this repo with any tools; `order-support-mcp` is an empty stub and out of scope).

## Decision

`meshmedic-mcp` runs over **STDIO transport**, as a **single-user, single-process** server. One process serves exactly one connected client for its whole lifetime; it is launched fresh per session and exits when that session ends.

## State model

In-process memory is allowed to persist for the life of the process, including across multiple tool calls in one session. This process does not need to be — and must not be assumed to be — safe to run as multiple concurrent instances behind a load balancer. Concretely:

- The audit trail (`audit_log.jsonl`) is a local file, appended to directly with no file locking. This is only correct with exactly one writer.
- Roots enforcement (`roots_guard.py`) asks the *currently connected client* for its declared roots via `roots/list`. That question is meaningless without a single, stable, known client for the process's whole lifetime.
- MCP sampling (`prioritize_care_gaps`) sends a request back down the *same session's* back-channel to the *same connected client*. There is no per-request client identity to route on.
- The Epic FHIR private key (`epic_fhir_private_key.pem`) is a plain file on local disk next to the code, not a value injected per-instance from a secret manager.
- `.mcp.json` already launches this server as `uv run --directory <path> python server.py` — a command-and-args spawn, not a URL — which is the stdio launch shape, not an HTTP one.

None of this was built with "any instance can serve any request" in mind, and none of it needs to be: this is a local, single-operator walking-skeleton tool, not a multi-tenant service.

## Why STDIO, not stateless HTTP

Stateless HTTP was considered and rejected. It would require, at minimum: replacing the local JSONL audit trail with a shared external store reachable from every instance; re-fetching or otherwise re-establishing declared roots on every single tool call instead of once per session (since there is no "the connected client" to remember between requests); and re-architecting sampling to route each request back to the correct client across instances. That is real, non-trivial work in service of a scaling requirement this project does not have — there is no multi-user or multi-instance requirement anywhere in `docs/REQUIREMENTS.md` or `docs/STORIES.md`, and the project has a single clinician user model throughout. Building for horizontal scale here would be speculative complexity, not a real need.

## Explicit single-user assumption

**This server assumes exactly one user, one connected client, and one process, for the life of that process.** It must not be scaled horizontally (multiple instances behind a load balancer or process pool sharing one audit log) without first replacing the local-file audit trail and re-deriving the roots/sampling model for a multi-instance world. Anyone tempted to scale this by just running more copies of `server.py` should read this paragraph first.

## Consequences

- No session ID needs to be threaded through requests to disambiguate clients — there is only one.
- In-memory bridging state that lives only for one process's lifetime (e.g. `_sampling_calls` in `server.py`, keyed by `ctx.request_id` and popped on the normal path) is an accepted, correct pattern here — it would not be safe under a stateless-HTTP model with multiple instances.
- A startup log line should state the transport and state model plainly (`stdio`, `single-user-single-process`) so an operator can confirm from process logs alone which model is actually running, without reading this document.
