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

This first local release supports project create/list/get/rename/archive/restore,
media probing, and job get/resume. Hosted tenancy, templates, additional storyline
management, editing/rendering workflows, and Higgsfield, ElevenLabs, and DaVinci
Resolve adapters are planned. Provider access elsewhere in an agent's environment
doesn't configure a VideoGenie integration.

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

Keep the returned project, storyline, and job IDs between calls. Names aren't
identifiers, and these IDs aren't CLEO tasks, provider IDs, or Resolve project
identifiers. Keep original media in place and back up the data directory.
Credentials, media, databases, and outputs don't belong in Git.

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

Responses use a versioned JSON envelope with `success`, `request_id`, and
`data` or `error`. Project and job data are returned directly; project lists
use `data.projects`. Command errors use nonzero exit codes. Also inspect a
returned job's `status` and failure details: a successfully retrieved job can
describe a failed media attempt.

CLI media paths may be relative to the caller's working folder. Absolute paths
are preferable when handing work between agents. Reuse a creation/probe
idempotency key only for the same request; changed input requires a new key.

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

The server exposes `videogenie_capabilities`,
`videogenie_project_create`, `videogenie_project_list`,
`videogenie_project_get`, `videogenie_project_rename`,
`videogenie_project_archive`, `videogenie_project_restore`,
`videogenie_media_probe`, `videogenie_job_get`, and
`videogenie_job_resume`. Tool discovery provides typed input schemas.
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

The [installed-agent proof](docs/external-agent-proof.md) records real
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
