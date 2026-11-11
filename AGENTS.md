---
title: Develop VideoGenie
audience: agent
---
# Develop VideoGenie

Confirm the checkout, branch, and Git status before changing files. Read README.md
and the relevant source/tests. The installed CLI and stdio MCP server currently
manage project lifecycle and durable local media probes. Hosted tenancy, provider
adapters, templates, and editing/rendering remain planned; don't claim direct
external-tool activity was performed by VideoGenie.

## Architecture and state

- Keep services in `src/videogenie/core/`, shared models in `contracts/`,
  external boundaries in `adapters/`, and media execution in `workers/`.
  CLI and MCP must reuse the same domain rules.
- Carry explicit project/storyline/job identities. Preserve revision checks,
  idempotency, leases, input fingerprints, and structured errors.
- Keep credentials, databases, footage, outputs, caches, and virtual environments
  out of Git. Preserve local productions; don't move or delete customer media.
- Keep source changes bounded and reversible. Inspect callers and tests before
  changing contracts. Report incomplete coverage and unsupported capabilities.

## Verify a change

Use Python 3.12 and `uv sync --locked`. Run focused pytest files during iteration
and `make check` before completing a code change. Record which commands ran and
their results; don't substitute a scaffold test for an actual media/transport
proof. CI must preserve affected consumers and broad fallback when scope is
unclear.

Installed-package tests accept `VIDEOGENIE_TEST_WHEEL` as an absolute existing
wheel path. When supplied, use that exact artifact; don't silently rebuild.
Keep installed-client tests outside the source tree and verify non-editable
imports. See `docs/external-agent-proof.md` for the repeatable full proof.

Use task-linked commits when a task tracker is configured. Follow repository
checks and protected-branch review; don't bypass hooks or force-push main.
Document public behavior changes and distinguish local evidence from final
hosted CI status.

## Optional CLEO coordination

A clean clone works without CLEO. If the team uses it, initialize a local project
with `cleo init`, then use `cleo briefing` and `cleo focus TASK_ID`.
Create tasks with acceptance criteria, inspect impact coverage before editing,
record verification evidence linked to those criteria, and only then complete
the task. Use `cleo <command> --help` for the installed version's protocol.
Keep local CLEO state private and out of application runtime paths.

When maintaining canonical documents in CLEO, update them through its document
commands and publish exact repository mirrors. Don't copy internal task dumps,
personal machine paths, or production notes into public documentation.
