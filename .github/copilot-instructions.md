# GitHub Copilot Instructions for JELICA

Before proposing or applying code changes, read:

* `AGENTS.md`;
* `docs/PROJECT_CONTEXT.md`;
* `docs/MVP_SPEC.md`.

Treat these files as the authoritative project instructions.

## Scope

Implement only the task explicitly requested by the user.

Do not add features from the target architecture unless they are required by
the current task.

In particular, do not introduce authentication, databases, task queues,
distributed workers, cloud storage, notifications, Docker,
deployment configuration, or lineage classification unless explicitly
requested.

Prefer the smallest working change that satisfies the stated acceptance
criteria.

## Architecture

Keep JELICA Core independent from:

* FastAPI;
* HTTP;
* Next.js;
* React;
* browser APIs;
* Electron;
* UI components;
* database-specific models.

Analytical functions must use domain models and must be independently testable.

FastAPI and Next.js code must remain thin adapters around Core functionality.

Do not move analytical logic into route handlers, React components, or API
serialization code.

## Biological results

Do not fabricate biological data, analytical output, or interpretation.

Do not:

* assign lineages or clades without an implemented supported method;
* present generic clusters as verified lineages;
* claim that heuristic validation proves biological compatibility;
* return placeholder results when an analytical stage fails;
* silently ignore invalid symbols or failed external tools.

Unsupported conclusions must be represented as warnings or limitations.

## External tools

When invoking external tools such as MAFFT:

* use a controlled subprocess call;
* capture exit status, standard output, and standard error;
* use a timeout;
* handle a missing executable;
* clean temporary files;
* return a clear domain-level error;
* never fabricate replacement output.

## Code changes

Do not modify unrelated files.

Do not rename modules or change existing public interfaces unless the task
requires it.

Do not perform broad refactoring while implementing a narrow feature.

Do not add dependencies unless they are necessary.

Use existing project patterns before creating new abstractions.

Keep functions focused and use explicit types.

Avoid hidden global state.

Prefer deterministic behavior.

## Testing

Add or update tests for every analytical behavior change.

Use small deterministic fixtures.

For variant detection, include known expected SNP, insertion, and deletion
cases.

For bug fixes, add a regression test when practical.

Run relevant tests after making changes.

Do not report a task as complete when relevant tests are failing.

## Git operations

Do not execute:

* `git commit`;
* `git push`;
* `git rebase`;
* `git reset --hard`;
* history-rewriting commands;
* `gh pr create`.

Do not add `Co-authored-by` trailers.

The user reviews and commits changes manually.

## Generated and local files

Do not commit:

* uploaded biological data unless explicitly selected as repository fixtures;
* analysis output;
* temporary files;
* virtual environments;
* dependency caches;
* build directories;
* secrets;
* `.env` files;
* local IDE state;
* operating-system metadata.

## Completion report

After completing a task, report:

1. files changed;
2. commands executed;
3. test results;
4. known limitations;
5. any manual verification still required.

Do not claim that a command or test succeeded unless it was actually executed.
