---
title: VideoGenie
audience: agent
---
# VideoGenie

VideoGenie is a Python foundation for agent-driven video production. Its local core
stores projects and storylines in SQLite with stable UUIDs, revision checks, and
idempotent creation. Durable media-probe jobs inspect local files with ffprobe and
store structured results.

This revision includes the project and probe services and a CLI with help/version
commands. The machine-readable CLI and MCP transports are being integrated;
their installed cross-transport release proof is still pending. The launch recipes
below distinguish working installation checks from the pending interface.

Hosted tenancy, rendering/editing workflows, templates, and Higgsfield,
ElevenLabs, and DaVinci Resolve adapters are planned. Existing provider access in
an agent's environment doesn't automatically configure a VideoGenie integration.

## Install a verified build

Python 3.12 is required; development uses Python 3.12.13. Build from the
[VideoGenie repository](https://github.com/jasonfaria84-rgb/video-genie):

```bash
uv sync --locked
uv build
```

Install the resulting wheel into an environment outside the checkout. Replace the
absolute example paths with your own:

```bash
uv venv --python 3.12 /absolute/runtime
uv pip install --python /absolute/runtime/bin/python /absolute/artifacts/video_genie-0.1.0-py3-none-any.whl
/absolute/runtime/bin/videogenie --version
/absolute/runtime/bin/python -m videogenie --help
```

Use a wheel built from this repository or a verified release artifact. An
identically named package from a public index isn't verified project provenance.
The installed package doesn't need CLEO, a source checkout, or a prior agent
conversation.

Media probes require ffprobe on PATH. Install FFmpeg through your platform's
package manager, then check `ffprobe -version`. FFmpeg itself is also needed for
the generated-media regression fixtures. Probe results contain metadata, not
rendered video.

## Keep state independent of the agent

Use an absolute data directory on local storage. The resolution order is
`--data-dir`, then `VIDEOGENIE_DATA_DIR`, then the OS-local application-data
default. Different agents sharing a project must use the same data directory.
The current working folder doesn't select a project or relocate the database.

Carry explicit project and storyline UUIDs across calls. Display names aren't
identifiers. Mutating project operations require the expected revision; create
and probe operations use idempotency keys. Store media separately from the
database, keep originals in place, and don't commit state or credentials.

## Prepare the pending CLI interface

The following commands describe the agreed transport interface. They require the
CLI integration and installed release verification; they aren't capabilities of
the help/version-only CLI in this revision.

```bash
/absolute/runtime/bin/videogenie --data-dir /absolute/data capabilities --json
printf '%s\n' '{"name":"Demo","idempotency_key":"demo-create-1"}' |
  /absolute/runtime/bin/videogenie --data-dir /absolute/data project create --json --input-json -
```

Read `data.project_id` and `data.storyline_id` from the returned project.
The planned JSON operations are:

| Command | JSON input |
| --- | --- |
| `project list` | `{"include_archived": false}` |
| `project get` | `{"project_id":"PROJECT_UUID"}` |
| `project rename` | `{"project_id":"PROJECT_UUID","name":"New name","expected_revision":1}` |
| `project archive` / `project restore` | `{"project_id":"PROJECT_UUID","expected_revision":1}` |
| `media probe` | `{"project_id":"PROJECT_UUID","storyline_id":"STORYLINE_UUID","path":"/absolute/input.mp4","idempotency_key":"probe-1"}` |
| `job get` / `job resume` | `{"project_id":"PROJECT_UUID","job_id":"JOB_UUID"}` |

Pass each object through `--json --input-json -` on stdin. Responses use a
versioned JSON envelope and command failures use nonzero exit codes. The probe
runs inline with bounded execution and persists its job; it doesn't launch an
unattended worker daemon. Its result reference identifies a SQLite artifact with
a payload and digest, not an output filename. CLI-relative media paths resolve
against the caller's working folder; absolute paths are preferable across agents.

## Prepare the pending MCP interface

The dependency lock pins the official MCP SDK 2.3.0. The server entry point is
declared, but the implementation and official-client verification are pending
integration. Once verified, configure your MCP client's executable and arguments:

```json
{
  "command": "/absolute/runtime/bin/videogenie-mcp",
  "args": ["--data-dir", "/absolute/data"]
}
```

The target transport is local stdio. Don't redirect ordinary output into protocol
stdout. No network listener is part of this release target. Protocol
2026-07-28 is the SDK target; compatibility with a particular host must be proven
with that host rather than inferred from packaging.

Planned tools are `videogenie_capabilities`,
`videogenie_project_create`, `videogenie_project_list`,
`videogenie_project_get`, `videogenie_project_rename`,
`videogenie_project_archive`, `videogenie_project_restore`,
`videogenie_media_probe`, `videogenie_job_get`, and
`videogenie_job_resume`. They share core validation and state with the CLI.
Reconnect using explicit IDs and the same data directory.

## Develop and verify

```bash
uv sync --locked
uv run --locked pre-commit install
make check
```

The checks cover repository hygiene, Ruff, strict Mypy, pytest, and distribution
builds. Run the relevant test file during iteration; CI must select affected
consumers and fall back to broad coverage when dependencies or scope are unclear.

Application services belong in `core/`, schemas in `contracts/`, external
boundaries in `adapters/`, and media execution in `workers/`. CLI and MCP
reuse the same services. Public distributions contain product tests and exclude
private planning fixtures. CLEO is optional development coordination tooling,
not a runtime dependency.

## License and attribution

VideoGenie is distributed under the [MIT license](LICENSE), applied with the
owner's authorization. Keaton Hoskins contributed the original project setup and
planning. Jason Faria (`jasonfaria84-rgb`) contributed the Python package and
subsequent product development. Contributor credit records provenance; it doesn't
assign ownership of unrelated work or third-party dependencies.
