---
description: Test, format, and draft a PR for the current change
argument-hint: [pr-title]
allowed-tools: Bash(uv run python -m pytest:*), Bash(uv run ruff format:*), Bash(git add -u:*), Bash(git diff --staged:*)
---

1. Run the test command, from `meshmedic-mcp/`:
   ```
   uv run python -m pytest -q
   ```
   If anything fails — a failing test, an error, or a collection error — STOP here and report the failures verbatim. Do not continue to step 2.
   > WHY: verification is step one, and step one is allowed to say no.

2. On green, run the formatter and stage the change:
   ```
   uv run ruff format .
   git add -u
   ```
   `git add -u` stages modifications to files Git already tracks — it will not pick up new untracked files. If this change added a new file, stage it yourself first; `/ship` won't guess at what's in scope.

3. Read the staged diff with `git diff --staged` and draft a PR description titled `$ARGUMENTS`, containing:
   - **Summary** — what changed and why, in plain language.
   - **Test Evidence** — the passing output from step 1, quoted verbatim.
   - **Risk** — what could break, and what this change does not cover.
   > WHY: `$ARGUMENTS` is whatever you type after `/ship` — the title travels into the body.

This command prepares a change. It does not run `git commit` or `git push` — those stay a separate, deliberate step you take yourself.
