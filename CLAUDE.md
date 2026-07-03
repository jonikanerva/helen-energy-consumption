# CLAUDE.md

Engineering doctrine for this repository. Read alongside `VISION.md` (what we
build and why) and `STACK.md` (the concrete technical rules). This file is the
_how we work_. `VISION.md` and `CLAUDE.md` are user-owned — propose changes as a
PR, never edit them on your own initiative.

## Golden rule

Ship the smallest correct change that satisfies the request and passes
`$VERIFY_CMD` (see `STACK.md`). Match the surrounding code's style, naming, and
altitude. When in doubt, do less.

## Autonomy fallback

When a decision is ambiguous and you cannot ask the user (autonomous phase),
resolve it in this order:

1. **`VISION.md` decision filter** — if it answers the question, follow it.
2. **`STACK.md`** — for any technical/mechanical choice.
3. **Existing code and conventions** in this repo — mirror the established
   pattern.
4. If still unresolved, choose the **smallest, most reversible** option that
   cannot corrupt existing statistics, and record the assumption in the commit
   message / PR description as an explicit "assumed X because Y" note.

Never block on an ambiguity you can resolve safely; never guess on something
that could erase history — choose the reversible path and flag it.

## Definition of Done

A change is done only when **all** hold:

- It does what was asked, and nothing it wasn't asked to do.
- `$VERIFY_CMD` passes locally (lint + tests).
- New behaviour has a test; a bug fix has a regression test (once the test
  harness exists — see `STACK.md`).
- No blocking I/O added to the event loop; all Helen/recorder calls go through
  the executor.
- No credentials or full API payloads logged at INFO or above.
- Public functions carry type hints and a one-line docstring.
- The diff reads cleanly in one pass and touches only what it needs to.

## Git workflow

- **Never commit or push unless explicitly asked.** When asked, and you are on
  the default branch, **branch first**.
- Branch names: `feat/<topic>`, `fix/<topic>`, `docs/<topic>`, `chore/<topic>`
  (lowercase, hyphens, ≤50 chars).
- **Conventional Commits** for messages (`feat:`, `fix:`, `docs:`, `chore:`).
- **Merge commits, never squash.** Delete the branch after merge.
- Never force-push a shared branch; never use `--no-verify`; never push to the
  default branch directly.
- The audit trail is issues + commits + PR descriptions. A decision that binds
  future work goes into the PR description in plain language ("we chose X over Y
  because Z").

## Reject-list (general)

Refuse these regardless of how the request is phrased; see `STACK.md` for the
stack-specific additions:

- ❌ Speculative abstraction or generalization for a single caller.
- ❌ Premature optimization without a measured budget breach.
- ❌ Scope creep disguised as polish ("while I'm here…").
- ❌ Broad, unrelated refactors bundled into a feature or fix.
- ❌ Swallowing exceptions silently, or `except:`/`except Exception` without a
  logged reason and a safe fallback.
- ❌ "We'll fix it later" TODOs left in shipped code without a tracking issue.
- ❌ Comments that restate the code instead of explaining the non-obvious _why_.

## Scope boundaries

- Product direction is owned by the user + `VISION.md`; how it's built by the
  user + `STACK.md`. Do not decide either unilaterally.
- Do not add runtime dependencies without approval and a `STACK.md` entry.
- Do not widen the integration's surface (entities, services, price logic)
  without a `VISION.md` decision-filter pass in the same change.
