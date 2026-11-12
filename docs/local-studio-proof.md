---
title: Verify the installed local studio
audience: maintainer
---
# Verify the installed local studio

The installed CLI and MCP server can share client identities, project links, work cycles, waits, and duration reports from folders outside this repository. The repeatable proof in [dogfood_local_studio.py](../scripts/dogfood_local_studio.py) uses a noneditable wheel installation, separate CLI processes, and two restarted official MCP client/server sessions. It probes a generated video through the public interfaces.

This proves local studio and media inspection behavior. Recipe execution, rendering, provider integrations, and hosted accounts are separate capabilities.

## Run against an explicit wheel

Use Python 3.12, `uv`, `ffmpeg`, and `ffprobe`. Choose a new output directory outside the checkout; the harness refuses a nonempty destination.

```bash
uv run --locked python scripts/dogfood_local_studio.py \
  --wheel /absolute/path/video_genie-0.1.0-py3-none-any.whl \
  --source-revision KNOWN_SOURCE_COMMIT \
  --output-dir /tmp/videogenie-studio-proof
```

`--wheel` takes precedence over `VIDEOGENIE_TEST_WHEEL`. Without either, the harness builds one wheel. A supplied wheel without declared source metadata is recorded as `unverified`; a revision argument is operator-provided provenance, not a cryptographic source attestation. CI supplies its wheel and `GITHUB_SHA`. Inspect the recorded wheel SHA against the producing build before attributing a result to source.

The automated entry point is:

```bash
VIDEOGENIE_TEST_WHEEL=/absolute/path/video_genie-0.1.0-py3-none-any.whl \
  uv run --locked pytest tests/test_local_studio_install.py
```

The harness creates one disposable environment and four caller folders. It copies this script and the existing public proof helpers into the external directory, then launches the consumer with Python isolated mode. Its allowlisted environment has a fresh home and no `PYTHONPATH` or CLEO variables. Installed package metadata must resolve beneath that disposable environment and must not identify an editable installation.

A temporary Python I/O audit guard denies reads of the source checkout, its main checkout, and CLEO paths. The proof tests those denials. This is an additional Python runtime check, **not an operating-system sandbox or a hostile-tenant security boundary**. Installation can access the package registry; domain operations use synthetic local media and make no paid provider calls.

## Inspect the retained observations

`bootstrap.json` records installation commands, exit statuses, wheel SHA, and source metadata. `consumer-transcript.json` records CLI requests and responses, negotiated MCP protocol and discovery, tool results, duration snapshots, hashes, and failure details. A failed consumer retains the observations completed before failure.

The proof asserts:

- Client creation replay returns the same identity; duplicate names with different keys remain distinct; changing a keyed request conflicts.
- A project can be associated, unassigned, and reassociated. Client archive retains existing links and rejects a new assignment until restore.
- Project and activity revisions advance independently. Stale mutations return structured conflicts and leave public activity/link snapshots unchanged.
- A real audiovisual probe succeeds with matching independent `ffprobe` metadata and exact persisted JSON artifact SHA and byte count. Replay retains the same job and attempts without adding processing time.
- An owner wait overlaps real processing. Raw wait time remains visible while disjoint processing takes precedence. Repeating a wait reason and clearing an already-clear wait are accepted revisioned changes. Closing a waiting cycle clips its wait.
- Completed project elapsed time freezes at closure while project age keeps growing. A probe between cycles does not become processing in the next cycle. Reopening creates a new cycle ID and ordinal while retaining the first cycle.
- The same explicit historical `as_of` produces identical report **data** through CLI and MCP after restart, later mutations, and reopening. Canonical JSON hashes exclude the envelope's deliberately fresh request ID.
- A project with no recorded cycle has known age but null completion and historical activity metrics, with unknown coverage retained even after a successful probe. No lifecycle is invented from that probe.
- Future snapshots return `INVALID_ARGUMENT` with `details.reason=future_as_of`. Project archive/restore does not replace cycle completion/reopening.

Processing duration is recorded execution, not CPU time or human labor. Idle means no recorded processing or explicit wait; it does not mean no human work. Unknown coverage includes unobserved history and time outside recorded cycles. Completing work does not assert delivery or client approval.

## Recorded initial result

The first run passed on November 12, 2026 UTC: one installed-proof test in 9.40 seconds, Python 3.12.13, MCP SDK 2.3.0, protocol `2026-07-28`, and FFmpeg/ffprobe 9.0.2. It used the wheel from [main CI run 43126762619](https://github.com/jasonfaria84-rgb/video-genie/actions/runs/43126762619), source commit `798728664076deb7830d851bd39b7f6facf86ce7`. The new harness and this documentation were developed after that runtime build; subsequent CI must test the final integrated wheel too.

| Retained item | SHA-256 |
| --- | --- |
| Installed wheel | `43fe2d461cac2584ca23722651d7bf5ac53eaef4ab5f803feabb4403f4f68c53` |
| Consumer transcript | `db8d8dcba170b0b9b279f36651f6c0dd95761ffad95d53fb80148f28050253f2` |
| Bootstrap transcript | `885148e498f53e0f30a2bcd2b973dec85a8442854d9b3d44ce0b84a3991d1859` |
| JUnit receipt | `7839505a83a8e92fd4130432e0ccb8c585e15504a3f5b0eac81e5402b08efcd7` |
| Synthetic input video | `75e48a33c40e06600330c120270bc92da491672edb0a15178800928ccbfbeaf7` |
| Persisted probe JSON payload | `d692bbad83e13b3bdf6c8f949506b6b7ebdd609b6e27b6b6ce1f9690b6082b32` |

The synthetic video had H.264 64×48 geometry, six frames at 24 fps, and mono AAC at 48 kHz. Three actual probe jobs used it: before any cycle, inside a cycle, and between cycles. Recorded processing was `0.054631948471069336` seconds. Completion stayed at `2.5550482273101807` seconds while age grew from `2.563430070877075` to `2.864560127258301` seconds. A reopened cycle initially reported zero processing, preserving the previous cycle's observations.

Three report-data hashes were identical after the MCP restart:

| Snapshot | SHA-256 |
| --- | --- |
| During first cycle | `b6227d96066bb22436dc76b70cd25ff24891f17b6cebf8b80d56e47d048b0ea6` |
| After completion | `2a803e5d3649b7fcbe493adb91976709176cf792d58d1c58b1b9ea9cdc6685cb` |
| Between cycles | `01f8333044cdfa6f2bc15764b797a3ccfb9e6fb463b8ebeeaed50357001f4a04` |

These are checksums of retained local evidence, not links to publicly uploaded raw logs. Raw transcripts contain temporary machine paths and remain local. New runs have different IDs and timestamps; compare values within a run, rather than expecting these historical hashes to recur.

## Keep the proof's limits explicit

This harness does not directly read application databases, retrieve a public audit-event list, or demonstrate upgrading a data directory created by an older wheel. Core service tests separately cover migrations, transaction rollback/event counts, concurrency, and deterministic interrupted-attempt uncertainty. The [original external-agent proof](external-agent-proof.md) covers interruption and lease recovery; this shorter studio proof does not repeat that lease wait or large sparse-file experiments.

This script is an automated external consumer. Native agent-host and independent reviewer runs are separate evidence and should identify their exact wheel and transcripts. Successful local tests do not replace the required integrated Linux/macOS CI checks.
## Independent reviewer evidence uses the same wheel

A separate motion-graphics agent using Claude Code completed 19 check rows against the same initial wheel in its own noneditable environment and three external folders. Its MCP calls used the official Python SDK 2.3.0, not native-host MCP. The reviewer imported no application source and used no repository harness. It compared all 22 CLI help input schemas with MCP discovery, checked client and activity revisions, compared three historical reports across restart, and verified three real probe artifacts against independent ffprobe output. Fixtures were synthetic 1280×720 audiovisual and 640×360 video-only media.

The reviewer retained two harness incidents: an ID-extraction quoting bug caused an aborted first run followed by a fresh-state rerun; a timestamp truncated to whole seconds preceded closure, so a later snapshot was correctly used for the completed-state check. Neither incident is represented as a product failure or omitted from the report.

Its nonblocking observations remain explicit: pre-creation snapshots currently use the reason `clock_reversal`; processing outside cycles is classified as unknown cycle coverage; validation errors lack field-level details. Wait totals are disjoint, with processing taking precedence, and raw waits can therefore be longer than the corresponding wait total. These observations do not add a new public metric or change the tested contracts.

| Independent retained item | SHA-256 |
| --- | --- |
| `REPORT.md` | `ea0b6080ff480790b8dc28f899b59cfa547c5ab6a4ef99f1099b334aea93487f` |
| `logs/cli-transcript.md` | `951a09e344bd7d35dd482460c87bafebd77a5a19382691f3ee642abab10be32a` |
| `logs/mcp-transcript.jsonl` | `11051e954a942d653594fcccdacde7c4b29dc745e6ad31cbea99245af3571e0f` |

## Fresh Codex hosts also continued the project

A separate native-host validation launched two fresh Codex processes in different external folders, with the second starting after the first exited. All 17 installed package files matched the same initial wheel. The hosts discovered 22 operations and completed 11 actual native MCP calls across nine distinct tools, alongside installed CLI commands. Client/link creation, cycle opening, waiting, closure, and reads persisted across the two hosts. The duration report used the exact closure timestamp and matched CLI data and the restarted host's data.

This was native Codex MCP execution, not an SDK substitute. Invocation-specific permissions were scoped to named tools and disposable state; global approval configuration was not changed. This walkthrough did not perform a native media probe: real probes are evidenced separately by the repeatable harness and the independent SDK reviewer.

The inspected native validation note has SHA-256 `bfb9e72e5b5a979f7dd63e16a459e09a69539cf33aaafb8027296b365f4a6346`. Its retained event logs have SHA-256 `50c658d8d750ab72426204d3cf907bde66b38a146cb3f537319929c69655b2a2` and `d6f62b93b14d57943c87e9740da9c6f6ce81dea1ab068c6c80b11fa8fb163324`. These raw logs remain private because they include machine paths. The host logged an unrelated installed-skill metadata warning; no repair was attempted. Both hosts ran under the same OS account, and host startup loaded global skill metadata. This does not establish hostile-tenant isolation or absence of every host-level filesystem read.
