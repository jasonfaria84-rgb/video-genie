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

This source exposes 77 shared agent operations: projects and clients, cycles and
duration, media probes, immutable assets, recipe and brand drafts/versions,
fictional previews, explicit render bindings, bounded local lower-third rendering,
artifacts, costs, approval requests and output selection, plus an editing toolkit:
dead-space removal, word-level transcripts made in DaVinci Resolve, cut plans that
remove filler words and false starts, timeline assembly, color grading and
rendering in Resolve, subtitles, motion graphics overlays placed on Resolve
timelines, a self-check of every cut in a render, and a resumable workflow that
turns a folder of takes into a finished video. Seven
additive discovery reads preserve the earlier 54 operation schemas and results; the
editing operations follow them. A separate supervised owner console handles
enrollment, publication, budget grants, and final review.

Rendering overlays with Fusion, Remotion, HyperFrames or Manim, reusable project
templates, additional storyline
creation, paid provider integrations/billing, and hosted tenancy remain outside this
slice. Resolve operations need DaVinci Resolve Studio running on the same machine
with external scripting set to Local. Higgsfield and ElevenLabs access elsewhere in
an agent's environment doesn't configure a VideoGenie integration. Physical owner
consent and hosted deployment aren't established by synthetic test fixtures.

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

The server exposes the same 77 agent operations. Replace spaces in each CLI command
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

This uses silence only. To remove filler words ("um", "uh") and false starts,
plan cuts from a transcript as described below.

## Remove filler words and false starts

`edit cuts plan` reads a stored word-level transcript and saves a cut plan that
removes filler words, false starts and long pauses. It works only on transcript
timings: it never reads or changes media, and it doesn't transcribe. Transcripts
come from `transcript.create`, a separate operation. `edit cuts get` reads a saved
plan again by `plan_id`.

```bash
printf '%s' '{"transcript_id":"TRANSCRIPT_UUID"}' \
  | videogenie edit cuts plan --json --input-json -
printf '%s' '{"plan_id":"PLAN_UUID"}' | videogenie edit cuts get --json --input-json -
```

| Field | Default | Meaning |
| --- | --- | --- |
| `transcript_id` | required | The stored transcript to plan from. |
| `filler_words` | um, umm, uhm, uh, uhh, er, erm, ah, hmm, mm | Words to cut. Case, spaces and punctuation are ignored, so `" Um,"` matches `um`. A multi-word entry such as `"you know"` matches consecutive words; none are in the default list. |
| `remove_false_starts` | true | Cut the earlier copy of a word or 1-4 word phrase repeated within about a second (`I I think`, `we should, we should go`). |
| `cut_sounds` | false | Also cut untranscribed sound (`kind: "sound"`) as a filler. Off by default: on real interviews much of it is speech the engine skipped. |
| `max_filler_seconds` | 1.2 | With `cut_sounds`, sound up to this long is cut as a filler (0.1 to 3). Longer sound, such as laughter or music, stays. |
| `min_pause_seconds` | 0.5 | Shortest gap between kept words to cut as a pause (0.2 to 30). |
| `padding_seconds` | 0.15 | Gap kept beside speech on each side of a cut. Must be under half of `min_pause_seconds`. |

Each cut has a reason: `filler`, `false_start`, `pause`, `leading_silence` or
`trailing_silence`, plus the IDs of the words it removes. A filler or false start
and the pause around it merge into one cut, reported with the more specific
reason (`filler` before `false_start` before `pause`). Cuts never start or end
inside a kept word; a filler that overlaps other speech is left in. Single words
people often double on purpose, such as "very very" or "that that", aren't treated
as false starts. If nothing would be left, the plan cuts nothing. The plan's
`kept` ranges are what remains to assemble; it names the transcript and source
SHA-256 it was made from. Use `resolve timeline assemble` to turn one or more
plans into a Resolve timeline, then `resolve render` to make the file.

## Create subtitles

`captions create` turns a stored word-level transcript into an SRT file of short
cues: two spoken words per cue in UPPERCASE by default. Give `cut_plan_id` (a plan
made from the same transcript) to time cues to the edited output instead of the
source; words inside cuts are dropped and no cue spans a cut. The SRT is plain UTF-8
and imports into DaVinci Resolve as a subtitle track; `resolve timeline assemble` can
place it for you (see below). Add `burn_in` to also render
the subtitles into a new copy of a video with local FFmpeg.

```bash
printf '%s' '{"transcript_id":"<uuid>","output_path":"/absolute/talk.srt"}' \
  | videogenie captions create --json --input-json -
printf '%s' '{"transcript_id":"<uuid>","cut_plan_id":"<uuid>","output_path":"/absolute/talk.srt",
  "burn_in":{"video_path":"/absolute/talk-edit.mp4","output_path":"/absolute/talk-subtitled.mp4"}}' \
  | videogenie captions create --json --input-json -
```

| Field | Default | Meaning |
| --- | --- | --- |
| `words_per_cue` | 2 | Spoken words per cue (1 to 12). |
| `uppercase` | true | Uppercase the text. Leading/trailing punctuation is always removed; apostrophes stay. |
| `max_cue_seconds` | 3.0 | A cue ends early rather than run longer than this. A pause of 0.5 s or more also ends a cue. |
| `min_cue_seconds` | 0.35 | Shorter cues join the next (else previous) cue when that fits; otherwise they're lengthened into the following gap. |
| `burn_in.font_size` | 18 | libass font size on a 288-line canvas, so it scales with video height (8 to 96). |
| `burn_in.timeout_seconds` | 1800 | Deadline for the burn-in render. |

The result reports the cue count, words captioned and dropped, the SRT's SHA-256 and
size, and the first five cues. Burn-in reports the new file's SHA-256, size, measured
duration and the style used. With a cut plan, `burn_in.video_path` must be the edited
output, not the source. Burn-in needs an FFmpeg built with libass (the `subtitles`
filter); without it the request fails with `DEPENDENCY_UNAVAILABLE` before anything is
written, and you can drop `burn_in` to get the SRT alone. Inputs are never modified and
existing files are never overwritten; a failed burn-in removes the SRT it just wrote.
MCP paths, including those inside `burn_in`, must be absolute.

## Check a render before delivery

`render check` inspects every cut of a finished render before anyone calls it final.
It reads the file only (its SHA-256 is checked before and after) and uses local FFmpeg.
Give cut positions in output time, either as `cut_points_seconds` or as the
`cut_plan_id` of a stored cut plan, where cuts fall at the joins of its kept ranges.
For a `media silence remove` result, the cut points are the running totals of the
kept segments' durations, without the last one.

```bash
printf '%s' '{"rendered_path":"/absolute/talk-tight.mp4","cut_points_seconds":[1.733,3.066],"contact_sheet_path":"/absolute/talk-cuts.png"}' \
  | videogenie render check --json --input-json -
```

Each cut gets a pass or fail with reasons:

| Reason | Fails when | Default |
| --- | --- | --- |
| `audio_jump` | The 10 ms audio level before the cut and after it differ by more than `max_audio_jump_db`. Levels are dBFS, floored at -60 so room tone can't jump. | 20 dB |
| `audio_click` | The largest sample step within 5 ms of the cut is more than `max_click_db` above the largest step in the surrounding 100 ms (48 kHz mono). | 12 dB |
| `black_frame` | The frame just before or just after the cut is black: 98% of pixels at or below `black_luma_threshold`. | 0.1 |
| `frozen_frame` | Three consecutive frames within three frames of the cut differ by less than `freeze_max_difference` (mean luma, 0-255). 0 turns this off. | 0.1 |

The whole file also fails with `av_drift` when its audio and video stream durations
differ by more than `max_av_drift_seconds` (default 0.05), or with
`av_drift_unmeasured` when they can't be measured. Each duration runs from the stream's
first to its last presented packet, read from the first few packets and the last few
seconds only. AAC encoder priming (`skip_samples`) doesn't count as audio, because
Resolve's MP4 edit lists include it in the container's audio duration. If the packets
can't be read, the container's stream duration is used instead. Calibration on generated
media: clean silence-removal cuts measure a 0 dB jump and a -40 dB click score; an
abrupt quiet-to-loud join measured a 34 dB jump, and a same-level phase jump a 21 dB click
score. Frames in a freeze differ by under 0.01; moving test video by 2 or more.

Cut times should be within 5 ms of the real join, and frames are located assuming a
constant frame rate starting at zero, as FFmpeg renders are. A hard cut into loud
speech fails `audio_jump` even when it doesn't click; still slides and screen
recordings can fail `frozen_frame`. Raise the threshold, or set it to 0 for frozen
frames, when that is intended. `contact_sheet_path` writes a new `.png` or `.jpg` grid,
one cell per cut: the frame before the cut on the left, after it on the right, failed
cuts labelled in red. Existing files are never overwritten. Up to 2,000 cuts are checked
per call.

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

## Transcribe speech word by word

`transcript create` transcribes one local file word by word and stores the result.
`transcript get` reads it back by `transcript_id`.

```bash
printf '%s' '{"project_id":"<uuid>","source_path":"/absolute/talk.mp4"}' \
  | videogenie transcript create --json --input-json -
```

Each entry has `word_id`, `start_seconds`, `end_seconds`, `text`, `speaker` and
`kind`. Transcripts are tied to the source file's SHA-256. Choose the engine with
`engine`:

- `whisper` (default) runs local Whisper ([mlx-whisper](https://pypi.org/project/mlx-whisper/),
  Apple silicon) on this machine. It keeps filler words such as "um" and "uh" as
  words with their own timings, which is what precise filler removal needs. Install it
  with `pip install mlx-whisper`, or point `VIDEOGENIE_WHISPER_BIN` at its `mlx_whisper`
  command. Audio never leaves the machine. The model (`whisper_model`, default
  `mlx-community/whisper-large-v3-turbo`) is fetched from the Hugging Face Hub the first
  time, which counts against `timeout_seconds`; once it is in the local Hub cache, runs
  are forced offline and the transcript records the cached revision in
  `engine_version`. `language` (such as `en`) skips language detection.
- `resolve` imports the file into the project's own Resolve project
  (`VideoGenie <project_id>`) and uses Resolve Studio's transcription, with
  `speaker_detection` (default true). Resolve leaves fillers out of its text, and on
  real interviews it also skips some real words. VideoGenie switches Resolve to the
  Edit page first, because Resolve won't transcribe on the Deliver page, and fails
  straight away if Resolve refuses. The call refuses when you have a different Resolve
  project open; pass `"allow_switch": true` after saving your work. The file must be a
  format Resolve imports; Resolve rejects the AIFF files written by macOS `say`.

With either engine, VideoGenie checks every gap between words with FFmpeg: quiet
stretches become `kind: "silence"` and audible ones `kind: "sound"` (a filler, breath,
noise or speech the engine skipped). `noise_threshold_db` (default -35) and
`timeout_seconds` (default 900) are optional.

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
share that frame rate. Resolve accepts fractional rates such as 23.976 or 29.97 only
as a project setting, so VideoGenie first sets its own `VideoGenie <project ID>`
project's default frame rate to the footage rate; the new timeline inherits it.
The reported `frame_rate` is read back from the timeline, and a rate that doesn't
stick is an error. Resolve timelines are at least 256 pixels on each side, so
smaller footage (such as 320x180) gets a larger frame and renders letterboxed. The
result's `width` and `height` are read back from Resolve, next to the footage's
`source_width` and `source_height`, and `resolution_warning` explains any
difference. `fade_frames` (default 1, the shortest fade Resolve allows)
adds audio fades at each join. `timeline_name` must be new; existing timelines are
never edited. The result reports the timeline's ID, length in frames and seconds,
and `cut_points_seconds`, the output times of every join, which `render.check`-style
reviews can use.

### Put subtitles on the timeline

Add `subtitles_path`, an absolute `.srt` file, to place its cues on subtitle track 1
of the new timeline. The SRT's times must already be in the output time of this exact
assembly, in seconds from the timeline start. `captions create` with the same cut plan
gives you that for a single-take edit; for several takes, write an SRT timed to the
combined edit.

```bash
printf '%s' '{"project_id":"<uuid>","takes":[{"cut_plan_id":"<uuid>"}],"subtitles_path":"/absolute/talk.srt"}' \
  | videogenie resolve timeline assemble --json --input-json -
```

VideoGenie adds the subtitles while the new timeline is still empty, reads every cue
back, and checks that each one starts within one frame of its SRT time. Then it places
the takes from the timeline start. A cue that ends after the assembled length is
refused before Resolve is touched. If Resolve places the cues anywhere else, or any
later step fails, the new timeline is deleted, so you can retry with the same name.
Resolve keeps an imported SRT's cues in its media pool, so VideoGenie imports a private
copy named by the file's SHA-256 (`subtitles/<sha256>.srt` in the data directory):
an edited SRT always gets a fresh import. The result adds `subtitle_cue_count` and
`subtitles_sha256`; both are null without subtitles. VideoGenie can't check that the
SRT text matches the takes.

Resolve 21.1's scripting API can't put SRT cues at their own times on a timeline
that already has clips: it places them after the timeline's end. For an existing or
hand-edited timeline, import the SRT in Resolve yourself (File > Import > Subtitle).

## Grade color in DaVinci Resolve

`resolve color apply` grades clips on a timeline in the project's own Resolve
project. The grade goes into a separate color version on each clip (default
`VideoGenie`), so the original grade stays available in Resolve.

```bash
printf '%s' '{"project_id":"<uuid>","timeline_name":"My edit","look":"warm_cinematic","adjustments":{"temperature":0.2}}' \
  | videogenie resolve color apply --json --input-json -
```

| Field | Default | Meaning |
| --- | --- | --- |
| `look` | `neutral` | `neutral`, `neutral_punch`, `warm_cinematic`, `cool_clean`, `soft_film` or `mono`. |
| `adjustments.exposure_stops` | 0 | -2 to 2 stops. |
| `adjustments.contrast` | 1 | 0.5 to 2. |
| `adjustments.saturation` | 1 | 0 to 2. |
| `adjustments.temperature` | 0 | -1 (cooler) to 1 (warmer). |
| `adjustments.tint` | 0 | -1 to 1. |
| `lut` | none | A LUT installed in Resolve's LUT library, as a relative path such as `Blackmagic Design/Blackmagic 4.6K Film to Rec709.cube`. |
| `track_index`, `item_numbers` | 1, all | Which clips to grade, by position on the track. |
| `version_name` | `VideoGenie` | The color version that holds the grade. |

The look and adjustments become ASC CDL values on node 1. Resolve only applies
LUTs from its own LUT library, so install new LUTs through Resolve first. Raw
filter text and arbitrary file paths aren't accepted.

### Store a grade in a recipe

Recipe content can carry an optional `color` section with the same `look` and
`adjustments` fields and bounds as `resolve color apply`, for example
`"color":{"look":"warm_cinematic","adjustments":{"temperature":0.2}}`. It's checked
like every other recipe field and has its own `schema_version`. LUTs aren't part of
it, because they name files in one machine's Resolve library. Recipes without
`color` keep exactly the content and digest they had before the section existed.
Nothing applies the stored grade automatically yet: read it from the recipe and pass
`look` and `adjustments` to `resolve color apply` yourself.

## Render a timeline from DaVinci Resolve

`resolve render` renders one timeline of the project's own Resolve project to a
new `.mp4` or `.mov` file with H.264, using a Resolve render preset (default
`H.264 Master`). Resolve renders into a private folder under the data directory;
VideoGenie checks that the file's duration matches the timeline, then publishes it
to `output_path`, which must not exist. If the render fails or passes
`timeout_seconds`, it is stopped and nothing is published.

```bash
printf '%s' '{"project_id":"<uuid>","timeline_name":"My edit","output_path":"/absolute/final.mp4"}' \
  | videogenie resolve render --json --input-json -
```

## Place motion graphics overlays

`overlay renderers` reports which motion-graphics renderers this machine can use:
`pil` (Pillow and FFmpeg, used by `graphics render_lower_third`), `fusion` and
`lottie` (DaVinci Resolve Studio), and `remotion`, `hyperframes` and `manim`
(found on `PATH`). Each entry has `available`, `version`, `missing`, a `fix` hint
and a `note`. Detection installs and downloads nothing, and every version check
has a time limit. Remotion, HyperFrames and Manim are detected only; VideoGenie
doesn't render with them yet. Remotion's license is free only for individuals
and companies of 1–3 people, so it stays off until the owner decides.

```bash
printf '{}' | videogenie overlay renderers --json --input-json -
```

`overlay place` puts an existing overlay file on a named timeline in the
project's own Resolve project: a `.mov` with alpha (such as the lower third's
QuickTime Animation file), a `.png` still, or a Fusion composition (`.comp`, up
to 16 MiB). The file is imported into the media pool once and reused after that.
A `.comp` goes on a fully transparent carrier clip that VideoGenie makes with
FFmpeg at the timeline's size and frame rate (kept in the data directory under
`overlay-carriers/`), and the composition is attached to that clip so Fusion
draws it over the footage. Resolve evaluates the composition just as it would
if you imported it yourself, so pass only compositions you trust. The result's
`fusion_comp_name` names the active composition on the new item.

```bash
printf '%s' '{"project_id":"<uuid>","timeline_name":"My edit","source_path":"/absolute/lower-third.mov","start_seconds":2,"duration_seconds":5}' \
  | videogenie overlay place --json --input-json -
```

`start_seconds` counts from the start of the timeline. With `track` set to
`"auto"` (the default), the overlay goes on the lowest video track above
everything in that time range, and a track is added when needed. An explicit
`track` must be 2 or higher and free over the range. Existing items are never
moved, trimmed or overwritten; a clash is an error. `position_x` and
`position_y` (pixels from the frame centre), `scale` (0.05–4) and `opacity`
(0–1) set Resolve's Pan, Tilt, Zoom and Opacity. A movie must have the timeline's
frame rate and be at least `duration_seconds` long. The result gives the new
item's `timeline_item_id`, `track_index`, record frames (end exclusive) and the
properties read back from Resolve. If any check fails after placing, the item
(and any track it added) is removed.

## Folder to finished video

`workflow run` turns a folder of takes into a finished video in one call. It runs
the editing operations above in order and stores a record of every step, so a run
that stops part-way can carry on later without redoing finished work:

1. `transcribe`: `transcript.create` for each take.
2. `plan_cuts`: `edit.cuts.plan` for each transcript.
3. `assemble`: joins the takes' kept ranges, in take order.
4. `color`: a look and adjustments, as ASC CDL values.
5. `render`: the new output file.
6. `captions`: an SRT beside the output, optionally burned in.
7. `check`: `render.check` at every join, with a contact sheet.

```bash
printf '%s' '{"project_id":"<uuid>","folder":"/absolute/takes","output_path":"/absolute/final.mp4",
  "backend":"ffmpeg","color":{"look":"warm_cinematic"}}' \
  | videogenie workflow run --json --input-json -
printf '%s' '{"workflow_id":"<uuid>"}' | videogenie workflow get --json --input-json -
```

With `backend` `resolve` (the default), assemble, color and render happen in the
project's own Resolve project, as `resolve timeline assemble`, `resolve color apply`
and `resolve render` do. With `ffmpeg`, they happen locally in one FFmpeg render:
kept ranges are trimmed to whole frames, joins get a 30 ms audio fade, and the
output is H.264 (CRF 18) with AAC audio. There is no timeline, so `timeline_name`
stays null. Both backends grade with the same CDL values; FFmpeg applies them with
`lutrgb` per channel and saturation with `hue`. The output takes its picture size and
frame rate from the first take that keeps anything. Every take is retimed to that
frame rate before trimming, so variable frame rate footage stays in sync, and takes
of another size are scaled and padded to fit.

| Field | Default | Meaning |
| --- | --- | --- |
| `folder` | required | Folder holding the takes. |
| `files` | all media | File names inside `folder`, in edit order. Without it, every `.mp4` `.mov` `.m4v` `.mxf` `.wav` `.m4a` file directly in the folder is used, sorted by name (up to 200). Names can't contain path separators. |
| `output_path` | required | A new `.mp4` or `.mov`. The SRT (`<name>.srt`), contact sheet (`<name>-cuts.png`) and burned-in copy (`<name>-captioned` with the output's `.mp4` or `.mov`) go beside it. None of these may exist yet. |
| `backend` | `resolve` | `resolve` or `ffmpeg`, for assemble, color and render. |
| `transcribe` | whisper | `engine` (`whisper` or `resolve`), `language`, `whisper_model`, as in `transcript create`. |
| `cuts` | plan defaults | The `edit cuts plan` settings: `filler_words`, `remove_false_starts`, `cut_sounds`, `max_filler_seconds`, `min_pause_seconds`, `padding_seconds`. |
| `color` | null | `look` and `adjustments`, as in `resolve color apply`. Null skips grading. LUTs aren't offered here. |
| `captions` | 2 words, uppercase | `words_per_cue`, `uppercase`, `max_cue_seconds`, `min_cue_seconds`, `burn` (false), `font_size`. |
| `check` | contact sheet on | `contact_sheet` turns the contact sheet on or off. |
| `skip` | none | Steps to leave out, by name. |
| `allow_switch` | false | Let Resolve steps switch away from the Resolve project you have open. |
| `timeout_seconds` | 7200 | Deadline for this call. Steps unfinished at the deadline can be resumed. |
| `resume_workflow_id` | none | Resume a stored workflow; see below. |

Skipping a step also skips the steps that need it, and the record says why. Without
`transcribe` there are no `plan_cuts` or `captions`, and backend `resolve` can't
assemble, so it needs `ffmpeg` or `assemble` skipped too. Without `plan_cuts`, whole
takes are used. Without `assemble`, nothing after it runs. Without `render`, there's
no `check`, and `burn` is skipped while the SRT is still written.

The result is the stored workflow record: `status` (`running`, `done` or `failed`),
the takes, the request, each step's `status`, `attempts`, times, `result` (IDs, paths
and measurements) or `error`, and `links`: `transcript_ids`, `cut_plan_ids`,
`timeline_name`, `render_path`, `captions_path`, `captioned_video_path` and `review`
(the check's verdict, failed cuts, reasons, A/V drift and contact sheet). A failing
step doesn't fail the call: the envelope succeeds, `status` is `failed`, and that step
holds the error code and message. Invalid requests still fail before anything runs.
Records live under the data directory in `workflows/`.

To resume, fix the cause and call `workflow run` again with `project_id` and
`resume_workflow_id`. The run continues at the first unfinished step; finished steps,
and takes already transcribed or planned, aren't repeated. Omit the other fields or
repeat them unchanged; only `timeout_seconds` and `allow_switch` may change. A finished
workflow is returned as it is. Only one call can run a workflow at a time; another
gets `LEASE_HELD`. A `running` step in a stored record was interrupted (or is still
running elsewhere) and runs again on resume. A local FFmpeg render that was published
just before an interruption is kept when its SHA-256 matches the digest recorded
before publishing. Other files an interrupted step already wrote (a Resolve render,
the SRT, the burned-in copy, the contact sheet) must be removed before resuming.
Captions and check refuse with `INPUT_CHANGED` if the render changed since the
render step.

Burning in captions needs an FFmpeg with libass. Without it the captions step fails
with `DEPENDENCY_UNAVAILABLE` and its SRT isn't kept. Audio-only takes skip grading
and burn-in, and the record says why. MCP paths must be absolute; CLI
paths may be relative to the current folder.

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
