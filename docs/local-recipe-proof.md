---
title: Installed local recipe proof
audience: developer
---
# Installed local recipe proof

The repeatable harness installs a noneditable VideoGenie wheel into a fresh virtual environment, then calls its public CLI and official MCP SDK from empty folders outside the checkout. It renders a fictional lower third using the supplied licensed Inter fixture. It neither calls a paid provider nor uses production media.

## Run the automated proof

From a development checkout with `uv`, FFmpeg and ffprobe installed:

```sh
uv sync --locked
VIDEOGENIE_TEST_WHEEL=/absolute/path/video_genie-0.1.0-py3-none-any.whl \
  uv run --locked python scripts/dogfood_recipe_render.py \
  --output-dir /absolute/path/new-empty-proof-folder
```

Without `VIDEOGENIE_TEST_WHEEL` (or `--wheel`), the harness builds one wheel locally. It freezes runtime dependencies from `uv.lock`, checks installed package bytes and RECORD hashes, and rejects editable installation. The output folder must be new and outside the checkout. Retain it for inspection; it contains disposable state, media, command/protocol transcripts and synthetic signed receipts. Treat this local evidence as private rather than publishing raw registry or launch data.

The focused test is `uv run --locked pytest tests/test_recipe_render_install.py -q`. It includes real lease expiry after killing only its own render coordinator, so it deliberately waits for the recorded lease rather than rewriting state or clocks. It also tests actual console cleanup when a separate consumer times out.

## What the automation demonstrates

- Both CLI and official MCP SDK discover 54 operations and share persisted project, storyline, recipe, configuration, job, artifact and cost results across restarts and unrelated working directories.
- Asset registration checks the supplied font and license bytes. Recipe and brand publication, a zero-provider-fee grant, and output approve/request-changes use the installed supervised owner HTTP channel with genuine verifier transactions and **synthetic test credentials**. A forged signature is rejected. These are automated cryptographic tests, not physical human consent or native browser interaction.
- Actual MOV output is 1920×1080, 24 fps, 150 frames, qtrle/argb, straight alpha and no audio. The harness hashes the master and independently decodes all 150 frames. Five HTTP PNGs at frames 0, 16, 75, 129 and 149 are checked against the exact published manifest and independently decoded master pixels. These stills do not demonstrate continuous motion.
- A run limit, new execution after grant revocation, stale binding and superseded output approval reject. Successful historical idempotent replay retains its original result. An interrupted coordinator is resumed only after the real lease expires; the old attempt remains recorded and a new fenced attempt succeeds.
- A cycle opens before rendering and closes afterward. Processing duration is positive; fixed historical snapshots survive restart/archive, and completed elapsed time remains fixed at a later observation while project age grows. Provider fees are known zero for this local renderer; measured wall time remains unpriced, and unknown client/internal/economic amounts remain null.
- Output review and explicit selection remain distinct from delivery and project completion. The proof does not manufacture delivery events or billing rates.

## Initial artifact and evidence boundaries

The initial artifact came from [main CI run 43210781009](https://github.com/jasonfaria84-rgb/video-genie/actions/runs/43210781009), source `2e71f29f33ef74046d7eaa90b122260eae1f07fd`, wheel SHA-256 `8160427da1b33f96041ec5093ad0a83fa493f258b88b4e1df918b10fcbc513c7`. That source passed 1,017 tests on each hosted OS. Source revision labels supplied to the harness are operator metadata, not an attestation. Future CI may supply a later exact wheel through the same environment variable; each run records its actual hash.

An earlier automated installed proof passed with real rendering and interruption recovery; its consumer transcript SHA-256 is `c1d9174cdf418056f97daec3ca06a62d32e23615a73b98960c7deef1aaaec8bd`. Follow-up assertions tighten manifest/sample binding, license provenance, revoked-new-run rejection and timeout cleanup. The corrected focused proof passed one test in 128.98 seconds; JUnit SHA-256 `05dc9fcdf655b609e9d654c4f1eac62de2b97d7e99e9a34bf1d8f2f67090a6cd`, consumer transcript `9e5e1684e76b603a3c87c2eabc941ebe8633aefde110c33daea48b4ba57b5543`. Final committed test and native-host receipts are tracked separately in the task handoff rather than conflating distinct runs.

Global installation was independently checked from an unrelated directory: 37 package files matched the initial wheel, 47 RECORD entries verified, frozen runtime dependencies were compatible, and the installed CLI exposed 54 operations. This is installation evidence, not a new native-agent invocation. The official SDK proof above is never labelled a native Codex-host proof.

A separate fresh native Codex host, launched in an empty unrelated folder against the same installed wheel, completed 31 successful MCP calls across 22 distinct tools. It created a separate scratch project, rendered and replayed a real job in the prepared fictional project, closed its explicitly opened cycle, and preserved prior frozen cost/duration snapshots. Its retained event transcript SHA-256 is `93b7b06016b4a787584989684bc6ced367dbb80e7e79709d4c794aad69aced3d`. This is actual native-host evidence, separate from the SDK harness. Its grant and publications still came from explicitly synthetic owner test credentials; native tool success does not convert those into human consent.

An independent motion-agent audit re-derived the same installed byte/dependency identity, compared all 54 CLI/MCP schemas, exercised a separate synthetic media probe and project cycle from three unrelated folders, and confirmed exact historical CLI/SDK restart parity and later-observation completion freeze. Its phase-one report SHA-256 is `a1a04be949d7afbb5a486945b4529b9fc1a58172e509dbb5752b2b6f7c976b88`. This was official SDK MCP, not that agent’s native host, and did not exercise publication, grants or final approval.

## Remaining acceptance

Physical owner enrollment, publication, grant and final-review evidence remains pending. No synthetic credential, agent message, successful model validation or passing test is represented as that approval. The prepared fictional workflow and exact preview/output must be reviewed through the owner console with a real passkey ceremony before that acceptance can close.

This is a local same-account tool. It is not an OS sandbox, tenant boundary, hosted SaaS deployment, old-version upgrade proof, or protection against arbitrary code already controlling the same account. Secret-bearing private handoffs are removed during normal supervised shutdown; abrupt operating-system termination cannot promise cleanup. Paid integrations, production media and default user state are outside this proof.
