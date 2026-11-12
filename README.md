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

This local release exposes 22 operations: persistent projects and clients,
client associations, work cycles and duration reports, real media probing, and
job get/resume. Recipes, brand kits, templates, additional storyline management,
editing/rendering, cost ledgers, provider billing, hosted tenancy, and Higgsfield,
ElevenLabs, and DaVinci Resolve adapters remain planned. Provider access elsewhere
in an agent's environment doesn't configure a VideoGenie integration.

## Install

Use Python 3.12 and uv. From the
[VideoGenie repository](https://github.com/jasonfaria84-rgb/video-genie):

```bash
git clone https://github.com/jasonfaria84-rgb/video-genie.git
cd video-genie
uv sync --locked
uv build
uv venv --python 3.12 /absolute/runtime
uv pip install --python /absolute/runtime/bin/python dist/video_genie-0.1.0-py3-none-any.whl
/absolute/runtime/bin/videogenie --version
/absolute/runtime/bin/python -m videogenie --help
```

Replace example absolute paths with your own. Use a wheel built from this
repository or a verified release artifact; a similarly named index package
doesn't establish project provenance. Development uses Python 3.12.13.

Media probes require ffprobe on PATH. Install FFmpeg with your platform's package
manager and check `ffprobe -version`. Tests also use ffmpeg to generate fixtures.
The runtime doesn't upload media or render a new video.

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
There is no owner publication, budget-grant, or authenticated client portal yet.

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
under `data`; lists use `data.projects` or `data.clients`. Client-only calls leave
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

The server exposes the same 22 operations. Replace spaces in each CLI command
with underscores and prefix `videogenie_`: `project client assign` becomes
`videogenie_project_client_assign`, and `project duration` becomes
`videogenie_project_duration`. `capabilities` is `videogenie_capabilities`.
Tool discovery provides the exact typed schemas; the JSON inputs in the tables
above are also the MCP arguments. Client create/list/get/archive/restore and all
project activity tools use the same shared services and persisted state as CLI.
MCP media paths must be absolute. Reconnect with the same data directory and
explicit IDs to continue work started through the CLI.

The lock pins the official MCP SDK 2.3.0. The observed official-client protocol
is 2026-07-28; compatibility with every host isn't implied. There is no HTTP
listener, remote authentication, or hosted service in this release.

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
