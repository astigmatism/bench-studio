# Coding sessions and visual benchmarks

Coding throughput (profile ID `coding`) retains BetterBench's existing throughput workload. Function checks (`coding-checks`) retains EvalPlus/MultiPL-E. Their quick/standard/full sample counts, historical data and baseline slots are unchanged.

## New suites

- **Coding sessions** (`coding-sessions`): two curated React/TypeScript + FastAPI/SQLite applications. Each tier has one issue-tracker task and one inventory task. Small changes UI behavior; Medium adds persistent controls; Large adds a workflow board or purchase-order receiving. Each attempt starts from a deterministic Git base revision.
- **Vision checks** (`vision-checks`): twelve committed screenshots with SHA-256 hashes, fixed prompts and annotated answers. Four cases each cover text, interface state and layout defects. The renderer recipe and browser version are recorded; normal runs never regenerate the images.
- **Visual design** (`visual-design`): the same six briefs expressed as interactive prototypes with local mock data. The agent produces `prototype.html`, receives desktop/mobile screenshots, critiques them, repairs failures and presents the verified prototype for review.

Difficulty is separate from sample count. Select all tasks in a tier or one task, and 1, 3 or 5 clean repetitions. Active limits are 30 minutes / 90 minutes / 4 hours with 100 / 300 / 800 model turns. Custom profiles allow at most 8 hours and 1,600 turns. Selected models run sequentially.

## Preparation and qualification

**Setup starts only when requested.** Use **Update and restart** on Bench Studio to install the application and build `local/bench-studio-session:current`. This does not run fixture checks or benchmarks. Select **Start setup** in the app when you want the controller to validate fixtures on the deployment host and queue one Small unattended qualification for each unqualified suite (one screenshot for Vision checks). `SESSION_SMOKE_TARGET` defaults to `daytime`; an unavailable model or missing vision support leaves the affected suite waiting and disabled. The banner provides live progress and **Stop setup**. Application health does not mean all benchmark suites have qualified yet.

The earlier updater's existing `build --profile images` and `up reports runner` operations read the newly fetched Compose configuration. The new controller defaults to idle and ignores the retired `SESSION_AUTO_SETUP` setting. It cancels pending jobs owned by the old automatic setup. It does not depend on the old updater reloading Python code partway through its execution.

Offline setup reserves the same execution lock as updates and benchmark launches. Stop setup terminates its preparation process, cleans up only its owned containers, cancels its queued/active qualifications, and releases that lock after cleanup. Other user runs remain untouched. Explicit requests survive browser closure but are interrupted by controller restart or application update; they never silently replay. Failures retain logs under `data/session-validation/` and require another explicit start. Failed live qualifications can also be opened and retried with **Run again**, preserving qualification mode. Successful preparations and qualifications are reused while their source, image and protocol remain compatible. Incompatible receipts gate the affected suites until you choose to prepare them again.

The commands below are also available for development and explicit operator preparation outside Compose.

Run on the same Docker host and data directory as the Studio controller. Preparation downloads/builds dependencies; all reference verification runs offline. The controller pins the prepared image ID. Reusing an image verifies its embedded base and tool source hashes. The session image is independent of the existing SWE-bench task images.

```sh
PYTHONPATH=.:vendor python scripts/prepare-sessions.py
# Or reuse an image you have already built:
PYTHONPATH=.:vendor python scripts/prepare-sessions.py --no-build --image local/bench-studio-session:current
```

Use `--suite coding-sessions`, `--suite vision-checks`, or `--suite visual-design` to prepare suites independently; omission validates all suites. Preparation checks all six tasks against the reference implementation, the unchanged base and an incomplete implementation, separately for coding and visual design. It saves detailed evidence under `data/session-validation/` and writes `data/session-preparation.json` only after all required checks pass. Changes to fixtures invalidate the receipt. Solutions and private acceptance scripts are never supplied to the agent sandbox.

A suite remains unavailable for ordinary runs until a representative runtime smoke run succeeds:

```sh
python scripts/prepare-sessions.py --smoke-target daytime --suite coding-sessions
python scripts/prepare-sessions.py --smoke-target nighttime --suite vision-checks
python scripts/prepare-sessions.py --smoke-target daytime --suite visual-design
```

Set `BENCH_STUDIO_URL` or pass `--url` for a different API address. Qualification uses one small task (one image for Vision checks), unattended, through the durable scheduler and idle gate. A successful, scored attempt records the qualification run. A failed attempt retains evidence and does not enable the suite. Qualification is an adapter check, not a claim that the model succeeds on every task.

To regenerate screenshots intentionally, install the frontend dependencies and Chromium, then run `node scripts/render-vision-fixtures.mjs`. Review and commit the resulting PNGs and manifest together, and repeat preparation/qualification. This changes the workload identity.

## Session protocol

The dedicated Studio Harbor agent uses a versioned JSON action protocol over the existing streaming router transport. Planning exposes only bounded list/read/search operations. File writes and command execution are unavailable until approval. Interactive review decisions are transactionally keyed by run, target, attempt and revision. Duplicate requests cannot approve or repeat work twice. Unattended approval is a fixed protocol message, not an assessment of the plan.

Review pauses release the inference scheduler. Approved work rejoins scheduling after an idle and identity check. Runtime changes invalidate a paused session at resume. Closing a browser is harmless; a controller restart interrupts the harness without replaying its conversation. Cancellation stops only labelled resources owned by that run. Application updates refuse active or paused sessions.

Implementation runs in an isolated Harbor Docker environment without network access, GPU access, Docker socket or host credentials. Candidate files are exported with size/path restrictions. Verification creates a new container with the original build configuration, dependencies and protected regression tests. It checks Python lint, TypeScript compilation, builds, public regressions, private HTTP behavior and browser interactions. Persistent tasks are tested against a pre-feature database and across a service restart. Receiving is checked for duplicate and concurrent requests.

Successful tasks retain a verified patch (including additions and deletions), changed-file list, test output and suggested commit message. No user repository is committed. Interactive visual acceptance occurs after objective verification; unattended visual results explicitly have no human approval. Preview documents run in sandboxed iframes, with an opaque origin and a CSP that forbids network connections and form submissions.

## What is measured

Results keep planning, implementation, verification, rendering, compaction, runtime waiting, review waiting, resumption waiting and setup separate. Active time includes model work, tool execution, test/repair cycles and compaction. Successful completion time ends after verification. Failed attempts retain their consumed time and reason; they are never treated as fast successes.

Requests retain actual reported token usage, finish reason, elapsed time and time to first token. Queue timing covers Studio scheduling and review resumption; backend queueing that is not separately exposed remains part of request latency. Vision evidence records advertised capability, observed projector arguments and available provenance. Unknown values stay unknown; time to first token is not reported as encoder-only latency. GPU/model settings stay under AI Runtime control.

Compaction preserves the feature contract, approved plan, feedback and verification state. The proactive text estimate is explicitly approximate; router context admission and reported usage are authoritative. A rejected context triggers one compaction/retry; repeated rejection is a context failure, not an infrastructure error or an unlimited retry. Compaction requests consume the same model-turn and active-time budgets.

Baselines are separated by suite, tier, task selection, repetition count, review mode, protocol, manifest and task budgets. Time comparisons use matching successful attempt IDs and always show completion rates. Different revision feedback disables automatic speed-improvement deltas. Model, context, sampling, runtime, image and vision-setting differences are shown as changed experimental conditions. There is no combined universal score.

## CLI

```sh
./bench run daytime --profile coding-sessions --difficulty medium --review-mode unattended
./bench run nighttime --profile vision-checks --repetitions 3
./bench review RUN_ID
./bench review RUN_ID --target daytime --attempt issues-small-r1 --revision 1 --action approve
./bench review RUN_ID --target daytime --attempt issues-small-r1 --revision 2 --action revise --feedback 'Add keyboard navigation to the plan.'
```
