---
title: VideoGenie
audience: agent
---
# VideoGenie

VideoGenie lets an external agent manage persistent video projects and inspect
local media through an installed CLI or stdio MCP server. Both transports share
SQLite storage, stable project and storyline UUIDs, revision checks, idempotency
keys, and durable ffprobe jobs. No source checkout, CLEO installation, or previous
agent conversation is needed at runtime.

This source exposes 67 shared agent operations: projects and clients, cycles and
duration, media probes, immutable assets, recipe and brand drafts/versions,
fictional previews, explicit render bindings, bounded local lower-third rendering,
artifacts, costs, approval requests, output selection, dead-space removal, a
DaVinci Resolve status check, word-level transcripts made in Resolve, and
timeline assembly in Resolve. Seven additive discovery reads preserve the earlier
54 operation schemas and results; the dead-space and Resolve operations follow them. A separate supervised
owner console handles enrollment, publication, budget grants, and final review.

General video editing, reusable project templates, additional storyline creation,
paid provider integrations/billing, and hosted tenancy remain outside this slice.
Higgsfield, ElevenLabs, and DaVinci Resolve access elsewhere in an agent's
environment doesn't configure a VideoGenie integration. Physical owner consent
and hosted deployment aren't established by synthetic test fixtures.

## Install

Use Python 3.12 and uv. From the
[VideoGenie repository](https://github.com/jasonfaria84-rgb/video-genie):

```bash
git clone https://github.com/jasonfaria84-rgb/video-genie.git
cd video-genie
uv sync --locked
uv build
uv venv --python 3.12 /absolute/runtime
uv export --frozen --no-dev --no-emit-project --format requirements-txt > /absolute/runtime/requirements.txt
uv pip sync --python /absolute/runtime/bin/python --require-hashes /absolute/runtime/requirements.txt
uv pip install --python /absolute/runtime/bin/python --no-deps dist/video_genie-0.1.0-py3-none-any.whl
/absolute/runtime/bin/videogenie --version
/absolute/runtime/bin/python -m videogenie --help
```

Replace example absolute paths with your own. Use a wheel built from this
repository or a verified release artifact; a similarly named index package
doesn't establish project provenance. Development uses Python 3.12.13.

Media probes require ffprobe on PATH. Install FFmpeg with your platform's package
manager and check `ffprobe -version`. Tests also use ffmpeg to generate fixtures.
Local lower-third rendering also requires ffmpeg. It uses explicitly registered,
verified font bytes, Pillow BASIC layout, and a bounded 1920×1080, 24 fps,
150-frame silent qtrle/straight-alpha MOV. It doesn't upload media. Complex-script
shaping, logos, general editing, and paid rendering aren't implied. Five verified
PNG samples are browser-displayable; the MOV is download-only in the console,
and still samples aren't continuous-motion review.

## Share state between agents

Choose one absolute data directory on local storage, such as `/absolute/data`,
and use it consistently. Resolution is `--data-dir`, then
`VIDEOGENIE_DATA_DIR`, then the OS-local application-data default. Changing the
working folder doesn't relocate storage. Different data directories are separate
registries, not hosted tenants.

Keep the returned client, project, storyline, cycle, and job IDs between calls.
Names aren't identifiers, and these IDs aren't CLEO tasks, provider IDs, or Resolve project
identifiers. Keep original media in place and back up the data directory.
Credentials, media, databases, and outputs don't belong in Git.

The local registry assumes cooperative agents under the owner's OS account.
Project-scope and revision checks protect application operations; they aren't
hostile-agent isolation from a process with unrestricted access to the same files.
Owner publication and grants require the local enrolled passkey channel, not an
agent-supplied actor flag. There is no hosted client portal. Agents may prepare
approval requests and inspect outcomes; they can't enroll, publish, grant, revoke,
or approve through the shared CLI/MCP operation catalog.

## Call the CLI from any folder

Discover operations and their exact input schemas before invoking them:

```bash
/absolute/runtime/bin/videogenie --data-dir /absolute/data capabilities --json
/absolute/runtime/bin/videogenie project create --help
/absolute/runtime/bin/videogenie project create --help --json
```

Per-operation JSON help returns `data.operation` and `data.input_schema`,
generated from the same input models used for validation. Help doesn't read stdin
or create project storage.

Create a project:

```bash
printf '%s\n' '{"name":"Demo","idempotency_key":"demo-create-1"}' |
  /absolute/runtime/bin/videogenie --data-dir /absolute/data project create --json --input-json -
```

Read `data.project_id`, `data.storyline_id`, and `data.revision` from the
response. Pass each following object on stdin with `--json --input-json -`;
replace UUID placeholders and use the latest revision for each mutation.

| Command | JSON input |
| --- | --- |
| `project list` | `{"include_archived":false}` |
| `project get` | `{"project_id":"PROJECT_UUID"}` |
| `project rename` | `{"project_id":"PROJECT_UUID","name":"New name","expected_revision":1}` |
| `project archive` / `project restore` | `{"project_id":"PROJECT_UUID","expected_revision":1}` |
| `media probe` | `{"project_id":"PROJECT_UUID","storyline_id":"STORYLINE_UUID","path":"/absolute/input.mp4","idempotency_key":"probe-1"}` |
| `job get` / `job resume` | `{"project_id":"PROJECT_UUID","job_id":"JOB_UUID"}` |

Responses have seven fields: `schema_version`, `success`, `request_id`,
`project_id`, `storyline_id`, `data`, and `error`. Successful records are directly
under `data`; legacy lists use named collections such as `data.projects` or
`data.clients`, while the new discovery pages use `data.items`. Client-only calls leave
project/storyline context null; studio project calls leave storyline context null.
Command errors use nonzero exit codes. Also inspect a returned job's `status` and failure details: a successfully retrieved job can
describe a failed media attempt.

CLI media paths may be relative to the caller's working folder. Absolute paths
are preferable when handing work between agents. Reuse a creation/probe
idempotency key only for the same request; changed input requires a new key.

## Manage a client and a project's work cycles

Use a fresh fictional project for this sequence. Each command receives the JSON
object on stdin with `--json --input-json -` and the same absolute data directory.
Replace `PROJECT_UUID`, `CLIENT_UUID`, and `CYCLE_UUID` with returned IDs.
The revisions below assume no intervening mutations.

```bash
printf '%s\n' '{"name":"Example Studio","idempotency_key":"studio-client-1"}' |
  /absolute/runtime/bin/videogenie --data-dir /absolute/data client create --json --input-json -
printf '%s\n' '{"name":"Studio demo","idempotency_key":"studio-project-1"}' |
  /absolute/runtime/bin/videogenie --data-dir /absolute/data project create --json --input-json -
```

Save `data.client_id` from the first response and `data.project_id` from the
second. The client and project each start at revision 1; activity starts at 0.

| Step | CLI command | JSON input |
| --- | --- | --- |
| List clients | `client list` | `{"include_archived":false}` |
| Read the client | `client get` | `{"client_id":"CLIENT_UUID"}` |
| Read the unassigned link | `project client get` | `{"project_id":"PROJECT_UUID"}` |
| Assign client; project revision becomes 2 | `project client assign` | `{"project_id":"PROJECT_UUID","client_id":"CLIENT_UUID","expected_project_revision":1}` |
| Read activity at revision 0 | `project activity get` | `{"project_id":"PROJECT_UUID"}` |
| Open work; save `data.current_cycle.cycle_id` | `project cycle open` | `{"project_id":"PROJECT_UUID","expected_activity_revision":0}` |
| Start owner wait; activity revision becomes 2 | `project wait set` | `{"project_id":"PROJECT_UUID","cycle_id":"CYCLE_UUID","expected_activity_revision":1,"reason":"owner"}` |
| Clear wait; activity revision becomes 3 | `project wait set` | `{"project_id":"PROJECT_UUID","cycle_id":"CYCLE_UUID","expected_activity_revision":2,"reason":null}` |
| Close work; activity revision becomes 4 | `project cycle close` | `{"project_id":"PROJECT_UUID","cycle_id":"CYCLE_UUID","expected_activity_revision":3,"outcome":"completed"}` |
| Report at the current service time | `project duration` | `{"project_id":"PROJECT_UUID","as_of":null}` |
| Archive client; retain the association | `client archive` | `{"client_id":"CLIENT_UUID","expected_revision":1}` |
| Restore that same client | `client restore` | `{"client_id":"CLIENT_UUID","expected_revision":2}` |

Archived clients are excluded from the default list; use
`{"include_archived":true}` to include them. Client archive preserves existing
project links and doesn't archive projects or prevent their existing probe
workflow. A new association to an archived client is rejected until restoration.
To unassign the example project, send
`{"project_id":"PROJECT_UUID","client_id":null,"expected_project_revision":2}`
to `project client assign`; the project revision becomes 3.

The three revisions are independent. Client archive/restore uses the client's
`revision`. Association changes use the project's `revision`, also shared with
project rename/archive/restore. Cycle and wait changes use the activity
`revision`. Fetch current state after `REVISION_CONFLICT` and decide whether the
intended change still applies; don't blindly resubmit with an incremented number.

Reopen with `project cycle open` and
`{"project_id":"PROJECT_UUID","expected_activity_revision":4}`.
It appends a new cycle ID at activity revision 5 and retains the first closure.
Close that new ID with revision 5 when finished. Archive/restore controls project
visibility and eligibility to open work; it doesn't close a cycle or prove delivery.

`reason` accepts `"owner"`, `"client"`, `"input"`, or explicit `null`.
Omitting `reason` is an error. Likewise `client_id:null` explicitly clears an
association; omitting the field is an error. `outcome` defaults to
`"completed"` and also accepts `"cancelled"`. Integers, booleans, and nulls
must be JSON values of the advertised type; extra fields are rejected.

## Read duration reports without inventing work

For a repeatable historical report, copy `closed_at` from the returned closed
cycle into `as_of`:

```json
{"project_id":"PROJECT_UUID","as_of":"CLOSED_AT"}
```

Pass that object to `project duration`. `CLOSED_AT` is a placeholder for the
actual timestamp, such as an ISO 8601 string ending in `+00:00`; don't send the
placeholder literally. Timestamps require an explicit UTC offset and normalize
to UTC. Omit `as_of`, or pass null, to use current service time. A snapshot before
project creation or after current service time returns `INVALID_ARGUMENT`;
the latter has `error.details.reason="future_as_of"`.

- `project_age_seconds` measures creation to report time, including time after
  completion. It continues growing.
- `project_elapsed_seconds` measures creation to the latest visible cycle's
  closure, or to `as_of` while a cycle is open. Completion freezes it until
  another cycle opens. It includes calendar gaps between cycles.
- `cycle_elapsed_seconds` sums recorded work cycles. Gaps between cycles don't
  become active processing, waits, or idle time within a cycle.
- `processing_active_seconds_known` is the union of observed worker intervals.
  `processing_attempt_seconds_known` sums them and can exceed the union when
  attempts overlap.
- Explicit owner/client/input waits and idle are disjoint within cycles:
  processing takes precedence, then owner wait, client wait, input wait, and idle.
  Raw wait intervals remain available separately.
- `unknown_seconds` and `coverage_notes` disclose unobserved history, queued
  time, and uncertain interrupted attempts. Before any recorded cycle, completion
  elapsed and known processing/wait/idle metrics are null, not measured zero.

An interrupted probe's recovery timestamp isn't its measured execution end.
These reports describe recorded lifecycle and worker wall time, not CPU time,
human labor, billable hours, approval, or client delivery. They don't calculate
provider charges or invoices.

## Register the MCP server

The installed `videogenie-mcp` executable runs a local stdio server. Configure
your MCP host with:

```json
{
  "command": "/absolute/runtime/bin/videogenie-mcp",
  "args": ["--data-dir", "/absolute/data"]
}
```

For Codex CLI:

```bash
codex mcp add videogenie -- /absolute/runtime/bin/videogenie-mcp --data-dir /absolute/data
codex mcp list
```

For Claude Code:

```bash
claude mcp add --transport stdio --scope user videogenie -- /absolute/runtime/bin/videogenie-mcp --data-dir /absolute/data
claude mcp list
```

Restart or reconnect the host if needed, then discover tools and call
`videogenie_capabilities`. Registration doesn't grant permission to use tools.
Respect the host's approval policy; the owner can consent to the specific tools
and data directory needed for a session. Don't disable approvals globally.
Ensure the server's environment can find ffprobe.

The server exposes the same 67 agent operations. Replace spaces in each CLI command
with underscores and prefix `videogenie_`: `project client assign` becomes
`videogenie_project_client_assign`, and `project duration` becomes
`videogenie_project_duration`. `capabilities` is `videogenie_capabilities`.
Tool discovery provides the exact typed schemas; the JSON inputs in the tables
above are also the MCP arguments. Client create/list/get/archive/restore and all
project activity tools use the same shared services and persisted state as CLI.
MCP media and asset source paths must be absolute. Reconnect with the same data directory and
explicit IDs to continue work started through the CLI.

The lock pins the official MCP SDK 2.3.0. The observed official-client protocol
is 2026-07-28; compatibility with every host isn't implied. The MCP transport has no HTTP
listener. The separately launched owner console binds local loopback HTTP; it
isn't remote authentication or a hosted service.

## Continue a recipe workflow from public scope

Use `capabilities`, operation `--help --json`, or MCP `tools/list` from the installed
build. CLI names replace operation dots with spaces; MCP names replace them with
underscores and add `videogenie_`. Discover schemas again after changing builds.
The 61-operation catalog includes these seven additive reads:

| Operation | Required scope | Result |
| --- | --- | --- |
| `recipe.version.list` | `recipe_id` | Immutable published versions, ordered by publication ordinal |
| `brand.version.list` | `client_id`, `brand_id` | Published versions in that client scope |
| `project.recipe.context` | `project_id`, `storyline_id`, `client_id` | Latest config revision per config ID in requested client scope, plus independent current policy |
| `grant.list` | Same three scope IDs | Latest scoped grant revisions, including unused, expired, and revoked history |
| `approval.list` | Discriminated `scope` object | Pending and completed ordinary action handles/outcomes; no enrollment or recovery records |
| `approval.outcome.get` | `application_instance_id`, `challenge_id` | Exact action outcome and, when completed, its immutable domain result |
| `output.review.list` | Three scope IDs, `artifact_id` | Reviews of that artifact and a separately reported `latest_review` |

A fresh caller doesn't need a prior conversation or repository files:

1. Discover projects and clients with `project.list/get`, `client.list/get`, and
   `project.client.get`. Choose the intended project, its returned storyline ID,
   and client explicitly. A historical client scope may differ from the current
   association; don't silently substitute it.
2. Call `recipe.draft.list` with `{}` and `brand.draft.list` with `client_id`.
   These existing inventories reveal recipe/brand IDs. Choose the intended draft;
   create or update through `recipe.draft.*` / `brand.draft.*` if needed. Register
   licensed assets with `asset.register` and use returned immutable pins. Never
   infer a recipe/brand pairing from names or list order.
3. Prepare `recipe.preview` from explicit current draft revision/hash pins, and
   use `recipe.compare` / `brand.compare` with an explicit prior-version ID or
   null baseline. Request owner publication using `approval.request` with its
   advertised action schema. An uninitialized owner returns
   `ACCESS_DENIED` / `owner_auth_uninitialized`; it doesn't enroll automatically.
4. Recover a lost publication handle with `approval.list` using
   `{"scope":{"kind":"recipe","recipe_id":"RECIPE_UUID"}}` or
   `{"scope":{"kind":"brand","client_id":"CLIENT_UUID","brand_id":"BRAND_UUID"}}`.
   Poll the returned application-instance/challenge pair with
   `approval.outcome.get`; `approval.get` still reads the exact challenge.
   Publication requires an owner action. Use `recipe.version.list` and
   `brand.version.list` to discover and explicitly choose published version pins.
5. Bind those exact pins with `project.recipe.bind`, following its project/config
   CAS inputs. To resume arbitrary existing bindings, call
   `project.recipe.context` with the three scope IDs and choose a returned config.
   `config.config_revision`, binding `config_policy_revision`, and
   `scope.current_policy_revision` are separate values. Don't derive policy from
   config revision, reuse a grant's policy as current truth, or choose a binding
   automatically because it appears last.
6. Request a bounded budget grant through `approval.request`; the owner reviews
   the exact scope and terms. `approval.list` with
   `{"scope":{"kind":"project","project_id":"PROJECT_UUID","storyline_id":"STORYLINE_UUID","client_id":"CLIENT_UUID"}}`
   recovers lost grant/revocation/review handles. `grant.list` discovers unused
   grants even when no job exists. Choose a grant explicitly; its presence is
   not permission. Execution rechecks current bindings, assignment, revisions,
   expiry, revocation, run/concurrency limits, and budget.
7. Submit `graphics.render_lower_third` with the chosen exact config/grant pins
   and an idempotency key. `render.job.list` discovers jobs;
   `render.job.get/resume` continues an exact job. `artifact.list/get` recovers
   output and provenance. Replaying a completed request returns its historical
   result; it doesn't authorize a new execution. Open/close explicit project
   cycles separately and read `cost.report` / `project.duration`.
8. Use `output.review.prepare` for the actual artifact and request final review
   through the owner channel. `output.review.list` recovers review IDs; its
   `latest_review` is insertion-ordered for the artifact, not the last UUID page
   item. Explicit `output.select` requires the current eligible approval and
   selection CAS revision. `output.selection.get` reads the historical selection;
   neither approval nor selection proves delivery or closes a work cycle.

The owner can launch the local console with `videogenie owner --help` guidance.
`owner serve/review/enroll/recover` are supervised local launch commands, not MCP
agent tools. Launch URLs, admission tokens, and recovery secrets stay private.
Every owner domain action requires fresh passkey verification. A valid model,
read record, host tool approval, or synthetic fixture isn't owner consent.

### Read bounded live pages without inventing a snapshot

All six list/context reads above accept `limit` (default 50, maximum 200) and
`after` (default null). Version cursors are positive publication ordinals; other
cursors are UUIDs. Keep scope unchanged and pass the returned `next_cursor` back
as `after` while `has_more` is true. Each response contains `returned_count`,
`range_exhausted`, `observed_at`, `storage`, and
`consistency: "per_response_live"`.

One response is coherent within one read transaction. Several pages aren't a
historical snapshot: concurrent insertion behind a cursor can be missed, and
current metadata can change between pages. Restart enumeration when a complete
current inventory is required; never describe range exhaustion as an immutable
multi-page snapshot. No discovery read chooses content, a grant, or an output.

Bindings and grants are filtered by the requested client before selecting the
latest revision for each ID. After reassignment, older client-scoped history can
remain visible beside `current_client_matches: false`, current project/client
status, assignment sequence, and current policy. Archive/revocation doesn't erase
that history or grant new eligibility. Mutation paths perform their own live checks.

`storage: "not_initialized"` differs from an available empty list. Missing required
client/project identity returns a structured error rather than fabricated scope.
The seven new reads don't initialize or migrate optional domain tables; existing
CLI/MCP startup may create the base registry on a fresh data root. This promise
doesn't apply to older operations such as `render.job.list`, whose established
initialization behavior is unchanged. Help remains storage-free.

For example, send this object to `project recipe context` using the same stdin
convention as above; replace the UUID placeholders with chosen public IDs:

```json
{"project_id":"PROJECT_UUID","storyline_id":"STORYLINE_UUID","client_id":"CLIENT_UUID","limit":50,"after":null}
```

Cost reports separate estimates, reservations, actual/reconciled facts, unknown
amounts, and client charges. Preserve currencies and explicit snapshot cutoffs
including `knowledge_sequence`; don't invent exchange rates or treat unpriced
compute/labor as free. Historical cost attribution stays immutable while current
client metadata is displayed separately.

## Remove dead space from dialogue

`media silence detect` finds pauses in a file's first audio stream and returns the
cut list without writing anything. `media silence remove` renders a tightened copy
to a new `.mp4`, `.mov` or `.m4a` file. The source is never modified, and an
existing output file is never overwritten.

```bash
printf '%s' '{"source_path":"/absolute/talk.mp4"}' \
  | videogenie media silence detect --json --input-json -
printf '%s' '{"source_path":"/absolute/talk.mp4","output_path":"/absolute/talk-tight.mp4"}' \
  | videogenie media silence remove --json --input-json -
```

| Field | Default | Meaning |
| --- | --- | --- |
| `min_silence_seconds` | 0.5 | Shortest pause to cut (0.2 to 30). |
| `noise_threshold_db` | -35 | Audio quieter than this counts as silence (-90 to -10). Raise it toward -25 for noisy rooms. |
| `padding_seconds` | 0.15 | Silence kept beside speech on each side of a cut, so a 0.5 s pause becomes 0.3 s. Must be under half of `min_silence_seconds`. |
| `fade_seconds` | 0.03 | Audio fade at each cut so joins don't click (remove only, 0 to 0.25). |
| `timeout_seconds` | 300 / 1800 | Deadline for detect / remove. |

The result lists every detected silence, each cut with its reason
(`leading_silence`, `pause` or `trailing_silence`), the kept segments, and the
original and tightened durations. Remove also reports the output's SHA-256, size
and measured duration. Cut edges are aligned to video frames, moving away from
speech. Video is re-encoded as H.264 (CRF 18) with AAC audio; only the first video
and audio streams are kept. One render handles up to 2,000 kept segments.
MCP paths must be absolute; CLI paths may be relative to the current folder.

This uses silence only. Filler words ("um", "uh") and false starts need a
word-level transcript and aren't detected yet.

## Check DaVinci Resolve

`resolve status` reports whether DaVinci Resolve Studio is open and scriptable,
without changing anything in Resolve. When it isn't, `reason` and `fix` say why
(`library_missing`, `library_unloadable`, `not_running_or_scripting_disabled`,
`timeout` or `free_edition`).

```bash
printf '{}' | videogenie resolve status --json --input-json -
```

Resolve must be open with Preferences > System > General > External scripting
using set to Local. Set `RESOLVE_SCRIPT_LIB` if Resolve is installed somewhere
other than the default location. VideoGenie edits only Resolve projects named
`VideoGenie <project ID>`, and it won't switch away from a project you have open
unless the request explicitly allows it.

## Transcribe with DaVinci Resolve

`transcript create` imports one local file into the project's own Resolve project
(`VideoGenie <project_id>`), transcribes it word by word with Resolve Studio, and
stores the result. `transcript get` reads it back by `transcript_id`.

```bash
printf '%s' '{"project_id":"<uuid>","source_path":"/absolute/talk.mp4"}' \
  | videogenie transcript create --json --input-json -
```

Each entry has `word_id`, `start_seconds`, `end_seconds`, `text`, `speaker` and
`kind`. Resolve leaves filler words such as "um" and "uh" out of its text, so
VideoGenie checks every gap between words with FFmpeg: quiet stretches become
`kind: "silence"` and audible ones `kind: "sound"`, which usually means a filler,
breath or noise. Transcripts are tied to the source file's SHA-256.

The call refuses when you have a different Resolve project open; pass
`"allow_switch": true` after saving your work to let VideoGenie open its own
project. `speaker_detection` (default true), `noise_threshold_db` (default -35)
and `timeout_seconds` (default 900) are optional. The file must be a format Resolve
imports; Resolve rejects the AIFF files written by macOS `say`, for example.

## Assemble an edit in DaVinci Resolve

`resolve timeline assemble` builds a new timeline in the project's own Resolve
project from the kept ranges of one or more takes, in the order given. Each take is
named by a stored cut plan, whose transcript names the source file; a source that
changed since transcription is refused.

```bash
printf '%s' '{"project_id":"<uuid>","takes":[{"cut_plan_id":"<uuid>"},{"cut_plan_id":"<uuid>"}]}' \
  | videogenie resolve timeline assemble --json --input-json -
```

The timeline uses the first take's frame rate and resolution, and every take must
share that frame rate. `fade_frames` (default 1, the shortest fade Resolve allows)
adds audio fades at each join. `timeline_name` must be new; existing timelines are
never edited. The result reports the timeline's ID, length in frames and seconds,
and `cut_points_seconds`, the output times of every join, which `render.check`-style
reviews can use.

## Recover a probe job

A probe executes inline with bounded execution and persists its attempt; no
background daemon continues it after the client exits. Its result reference is a
SQLite artifact containing `payload_json` and its SHA-256, not an output file.
Metadata reflects ffprobe's reported values; unavailable fields remain null.

After interruption, inspect the job and resume by project/job ID. A live lease
is rejected with `LEASE_HELD`; an expired lease can be reclaimed. An interrupted
job can still display `running` until resume claims it. A completed job returns
its existing artifact. Resolve missing tools or media before retrying, and submit
changed media with a new idempotency key. Revision conflicts require fetching the
current project and deciding whether the intended mutation still applies.

## Verify and develop

The [local recipe proof](docs/local-recipe-proof.md) records the historical
54-operation build, its exact source/wheel hashes, SDK runs, and separately
identified native-host evidence. Those observations don't prove this 61-operation
build, a later global installation, or physical owner approval. Repeat installed
proofs with an explicit `VIDEOGENIE_TEST_WHEEL` artifact and retain their generated
verification receipts, source/wheel/RECORD hashes, command transcripts, and native
host events. A passing SDK call isn't a native-agent call; a source label alone
isn't byte verification. Final-build CI/install/native outcomes must be observed
before claiming them, and no final wheel hash is embedded in these packaged docs.

The [local studio proof](docs/local-studio-proof.md) covers the client/cycle
workflow and its installed-wheel evidence. The earlier
[installed-agent proof](docs/external-agent-proof.md) records real
cross-folder CLI/MCP, fresh Codex host, and independent-agent observations,
including recovery and limitations. Those candidate observations aren't a claim
that every later release commit has passed hosted CI.

```bash
uv sync --locked
uv run --locked pre-commit install
uv run --locked pytest tests/test_cli.py
make check
```

Run relevant tests while iterating. `make check` covers hygiene, Ruff, strict
Mypy, pytest, and distribution builds. Hosted CI selects affected consumers with
broad fallback for shared, dependency, CI, unknown, deleted, or renamed inputs.
It caches locked dependencies and built distributions. The distribution cache uses
an exact key covering package input paths, modes, and content plus the builder
OS/architecture, Python, uv, and workflow. Restored artifact names and SHA-256
hashes must validate; malformed caches fail closed. A hit still runs selected
checks and reuses the same verified wheel across Linux/macOS jobs. GitHub cache
scope applies: a PR merge-ref cache can be reused by that PR's reruns, not by
unrelated branches. Hosted cache-hit evidence is recorded separately from this
configuration description.

Read [AGENTS.md](AGENTS.md) for development boundaries. CLEO is optional local
coordination tooling and isn't a runtime dependency. To repeat the full installed
proof, follow its linked guide; the interruption case intentionally waits for
the real lease to expire.

## License and attribution

VideoGenie is distributed under the [MIT license](LICENSE), applied with the
owner's authorization. Keaton Hoskins contributed the original project setup and
planning. Jason Faria (`jasonfaria84-rgb`) contributed the Python package and
subsequent product development. Contributor credit records provenance; it doesn't
assign ownership of unrelated work or third-party dependencies.
