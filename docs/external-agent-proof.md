---
title: Verify an installed VideoGenie candidate
audience: maintainer
date: 2026-11-11
---
# Verify an installed VideoGenie candidate

VideoGenie 0.1.0 can manage persistent projects and inspect real local video through
both its CLI and stdio MCP server. The evidence below covers a local candidate,
not hosted SaaS, paid providers, editorial rendering, or a public-release announcement.

## Repeat the installed-package proof

Use Python 3.12, uv, ffmpeg, and ffprobe. From the source checkout, run:

```bash
uv run --locked python scripts/dogfood_external_agent.py \
  --wheel /absolute/artifacts/video_genie-0.1.0-py3-none-any.whl \
  --source-revision <known-source-commit> \
  --output-dir /absolute/empty/proof-folder
```

Omit `--wheel` to build one wheel locally. CI can supply the same downloaded
artifact through `VIDEOGENIE_TEST_WHEEL`; the harness validates that it is an
existing absolute wheel path and installs it without rebuilding. A supplied
artifact with no source revision is labeled `unverified`, rather than assigned
the current checkout's revision.

The bootstrap creates a non-editable runtime and copies the consumer script into
the external proof folder. The consumer runs in Python isolated mode, with a
fresh home, an allowlisted environment, no PYTHONPATH, and no CLEO variables.
It uses only installed executable entry points and the official MCP client SDK.
It doesn't call application services or inspect the project database.

A temporary Python I/O audit guard rejects source-checkout and `.cleo` reads in
that disposable runtime. The consumer asserts those reads are denied. This is
a Python I/O guard, not an operating-system sandbox, and it doesn't change
permissions on the source checkout.

## Inspect the retained evidence

The output folder contains:

- `bootstrap.json`: build or supplied-wheel identity, SHA-256, source provenance,
  installation commands, exit codes, and the consumer invocation.
- `consumer-transcript.json`: CLI argv/stdin/results, MCP discovery and tool
  requests/results, protocol version, fixture/tool versions, artifact digests,
  lifecycle conflicts, negative paths, and recovery observations.
- The disposable runtime, synthetic audiovisual fixture, external data root,
  copied consumer, and four independent client folders.

The script reports `proof passed` only after all its assertions succeed. Partial
transcripts remain available if a command fails. The interruption test waits for
the actual default lease to expire; it doesn't shorten the product lease, edit
the database, or mark an attempt complete manually. It kills only processes it
created and identified at its barrier.

## Candidate evidence passed on macOS

The tested wheel is `video_genie-0.1.0-py3-none-any.whl`, from local source
snapshot `79db6aa`, with SHA-256:

```text
02d6ff6fff1501219d78a5f8cb7a00f20c0200fa574b25fd1ad5d922455e97ab
```

| Consumer | Observed result |
| --- | --- |
| Repeatable harness | Both transports created, renamed, archived, restored, and reopened projects; actual H.264/AAC metadata matched an independent ffprobe reading; cross-client job reads and completed-job resumes preserved IDs and artifacts; a killed attempt recovered after lease expiry. |
| Fresh Codex host | Five native MCP calls completed: capabilities, project create, media probe, job get, and project get. A CLI process in a second folder retrieved the same job and verified its artifact digest. |
| Independent motion-graphics agent | A separate SDK-only client runtime and non-editable application runtime passed public-client checks from three folders. Real 1080p 23.976 fps audiovisual and 2160p 25 fps video-only fixtures matched independent ffprobe on 29/29 and 19/19 compared fields. Lifecycle, replay/conflict, ownership, restart, concurrent creates, and interrupted-attempt recovery passed. |

The automated run retained 55 consumer events. Its transcript SHA-256 is:

```text
46e67b32104cd401293d79a583ec0909aa255507a9d937f1952c6d54564361b7
```

The fresh-host artifact SHA-256, independently recalculated from the exact
UTF-8 `payload_json` returned through the CLI, is:

```text
8800f5e5fe9b7a4047fbfb6916e16d41c943292c7e7da18e0167047e03f3255d
```

The independent agent's corrected report SHA-256 is
`2cb09c9d672f54fd2f31f6b59bce7fce3682b7e328a8b441995a08291db8c8ad`.
The previous report revision had SHA-256
`1e925a5e03aa61840d565785a13541a3da363e11ec5768f0331822618fc7ded7`.
Its CLI transcript SHA-256 is
`289638f768d72d7e33541607eb6a3d879e3f24af565e5596e89a4400781092d3`;
its MCP transcript SHA-256 is
`c32cd86185c1b8b6c537a1d929eb85c9b841037f1b6371366b6bdff82edfced0`.
Raw host and peer logs remain local; this page doesn't publish private machine
paths or claim that those logs passed a secrets scan.

The official SDK was `mcp==2.3.0`, the negotiated protocol was
`2026-07-28`, and the local ffprobe was 9.0.2. Only these observed client/runtime
combinations are claimed here. Hosted Linux/macOS CI results are separate evidence.

## Keep the observed limits visible

The first fresh Codex invocation was denied by the host's default tool-approval
policy. A second invocation with scoped consent for five tools against a temporary
data root passed. The initial denial is retained as a host-configuration finding;
it wasn't a VideoGenie execution failure. No global approval policy was changed.

CLI media paths may be relative to the caller's working directory. MCP media
paths must be absolute. Both transports keep storage independent of that directory:
explicit `--data-dir`, then `VIDEOGENIE_DATA_DIR`, then OS-local application data.

An expired attempt still appears as `running` with a past `lease_expires_at`
until `job resume` claims it. Successful job reads are immutable. An unknown MCP
tool returned a normal tool result with `isError: true`, text-only content, and no
structured VideoGenie envelope. It wasn't a JSON-RPC protocol error; the server
remained usable. The black-box observation doesn't establish which dispatcher
produced that result.

The peer's generic-CLI-help finding was resolved in follow-up source: individual
operations now expose their input schema through human and JSON help, for example
`videogenie project create --help --json`. The final combined-package regression
uses that follow-up source. Validation details still aren't field-specific, and
capability discovery doesn't report the resolved data directory.

The original candidate wheel contained onboarding text written before transport
integration. Executable behavior was verified independently of that historical
text; current onboarding is maintained separately.

The peer's 12 GiB synthetic sparse fixture occupied about 6.7 MB physically and
succeeded in about 28 seconds end to end. Its attempt took 20.23 seconds; the
roughly 8 seconds before job creation included harness overhead. No timeout was
observed. The peer withdrew its earlier inference that hashing happened twice.
Larger-file timeout behavior remains unverified, and no production footage was
used. This proof doesn't establish production-media throughput.

Metadata includes reported container, stream index/type/codec, duration, rational
frame/sample rates, time base, width, height, channels, and reported frame count.
Absent values remain null. The proof doesn't establish color grading, captions,
timeline editing, templates, generation-provider access, or autonomous paid work.
