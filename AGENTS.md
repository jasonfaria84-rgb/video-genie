---
title: Develop VideoGenie
audience: agent
---
# Develop VideoGenie

Confirm the checkout, branch, and Git status before changing files. Read README.md
and the relevant source/tests. The installed CLI and stdio MCP server currently
expose 61 shared agent operations, including local recipe/brand drafts, immutable
versions/assets, bounded lower-third rendering, costs, output review preparation,
and seven additive discovery reads. General editing/templates, paid provider
integrations/billing, hosted tenancy, and a physical owner's consent remain
unproved. Don't claim direct external-tool activity was performed by VideoGenie.

## Architecture and state

- Keep services in `src/videogenie/core/`, shared models in `contracts/`,
  external boundaries in `adapters/`, and media execution in `workers/`.
  CLI and MCP must reuse the same domain rules.
- Carry explicit client/project/storyline/cycle/job identities. Share an absolute
  data directory across clients; working-directory changes must not create a new
  registry. Preserve revisions, idempotency, leases, fingerprints, and errors.
- Keep client, project, and activity revisions independent. Associations advance
  project revision; cycles and waits advance activity revision. Validate same-project
  cycle ownership and reject stale changes without state or audit mutation.
- Keep completion separate from archive, approval, and delivery. Project age grows;
  completion elapsed freezes at the latest closure until reopening. Reopening
  retains old cycles. Duration as_of requires an offset and a past/present instant.
  Unknown history stays null or explicitly uncertain, never fabricated processing,
  human labor, billable hours, or zero total production cost.
- Preserve the seven-field envelope and strict model-derived schemas. Client-only
  results have no project/storyline context; project studio results don't invent
  storyline IDs. Required nullable inputs need explicit null to clear a link/wait.
- This local registry trusts cooperative agents sharing the owner's OS account.
  Scope/revision checks aren't isolation from unrestricted same-account file access.
  Don't add unguarded owner-publication or budget-grant tools.
- Keep credentials, databases, footage, outputs, caches, and virtual environments
  out of Git. Preserve local productions; don't move or delete customer media.
- Keep source changes bounded and reversible. Inspect callers and tests before
  changing contracts. Report incomplete coverage and unsupported capabilities.

## Resume using public scope and explicit choices

- Follow the README's public discovery workflow. Starting with project/storyline/
  client scope, `recipe.draft.list` and client-scoped `brand.draft.list` expose
  recipe/brand identities; published version lists expose exact pins. Choose each
  identity/version explicitly. No latest-content or grant auto-selection.
- The seven additive operations are `recipe.version.list`, `brand.version.list`,
  `project.recipe.context`, `grant.list`, `approval.outcome.get`,
  `output.review.list`, and `approval.list`. Preserve the original 54 input/result
  shapes and catalog prefix; `INPUT_MODELS` remains the shared CLI/MCP registry.
- `approval.list` takes an explicit recipe, client+brand, or project/storyline/
  client scope. It recovers pending handles and completed outcomes without
  credential/proof exposure. `approval.outcome.get` takes the returned
  application-instance/challenge IDs; enrollment/recovery aren't agent operations.
- `project.recipe.context` returns latest config revisions within the requested
  client scope and independent current policy. Filter by client before latest.
  `grant.list` includes unused/expired/revoked history. Compare stored binding
  policy with observed current policy; config revision isn't a policy epoch.
- Read `output.review.list.latest_review` for the actual artifact, not the last
  page item. Explicit selection needs a current eligible approval and CAS.
  Historical selection/replay remains a record, not current execution permission.
- Page limits are strict (default 50/max 200). Retain scope, follow `next_cursor`
  only while `has_more`, and inspect `returned_count`, `range_exhausted`,
  `observed_at`, and `storage`. `per_response_live` promises one-response snapshot
  coherence, not a stable snapshot across pages. Concurrent insertion behind a
  cursor can require restarting enumeration. Uninitialized isn't available-empty.
- New discovery reads don't initialize/migrate optional domains. Existing base
  CLI/MCP startup can create the registry; older operations keep their established
  initialization behavior. Don't claim completely filesystem-free discovery.
- Reassignment, archive, revocation, and recovery don't erase historical outcomes.
  Observe current client match/status and independent assignment/policy values;
  all mutations must recheck current eligibility. No read grants authority.
- Agents draft and request approvals. Only the supervised owner console/passkey
  publishes, grants, revokes, or reviews outputs. Ordinary actor flags, host tool
  consent, and synthetic credentials cannot stand in for owner consent. Keep
  owner launch/recovery secrets out of messages, logs, and ordinary JSON output.

## Verify a change

Use Python 3.12 and `uv sync --locked`. Run focused pytest files during iteration
and the relevant hygiene/lint/typecheck/build gates before review. Hosted CI
runs affected tests with conservative fallback; don't rerun the full local suite
for a bounded change. Record which commands ran and
their results; don't substitute a scaffold test for an actual media/transport
proof. CI must preserve affected consumers and broad fallback when scope is
unclear.

Installed-package tests accept `VIDEOGENIE_TEST_WHEEL` as an absolute existing
wheel path. When supplied, use that exact artifact; don't silently rebuild.
Keep installed-client tests outside the source tree and verify non-editable
imports. See `docs/external-agent-proof.md` for probe/host evidence and
`docs/local-studio-proof.md` for client/cycle proof. Compare duration reports using
one explicit as_of. A successful editable test isn't an installed-wheel proof.
`docs/local-recipe-proof.md` retains exact historical 54-operation source/wheel and
native-host evidence. The current 61-operation source needs its own generated
receipts and actual host calls; don't relabel old proofs or a prepared invocation
as final installation/native success. Bind supplied-wheel bytes, installed RECORD,
locked runtime dependencies, and source provenance separately. Keep physical
owner acceptance distinct from genuine but synthetic WebAuthn tests.

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
