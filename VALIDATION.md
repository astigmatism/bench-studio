# Bench Studio acceptance evidence

Accepted on 2026-09-18 UTC. Raw benchmark results, generated solutions, machine configuration, and full logs remain in the deployment's private `data/` directory. The checks below verify the application; small smoke samples are not a model ranking.

## Automated checks

The Python suite contains **186 tests** across the pinned engine, integration layer, API, scheduler, results, and updater. **Three Playwright tests** cover launch, both-model selection, live logs, cancellation, rerun review, profiles, keyboard operation, reports/exports, comparisons, narrow layouts, and dark appearance. CI runs the complete suite and a production frontend build: [GitHub Actions](https://github.com/astigmatism/bench-studio/actions).

Regression checks include busy backends, queued model/context changes, duplicate submissions, discovery outages, missing/crashed workers, restart reconciliation, unsupported parameters, malformed/truncated streams, missing token usage, incompatible comparisons, artifact traversal, and invalid prefill measurements. Updater tests reject dirty/wrong-origin/wrong-branch/wrong-upstream/divergent/unpublished source, failed builds, and failed health checks; they preserve the database. Partial build failures restore prior image tags, and failed restoration keeps maintenance active.

All **53 vendored BetterBench files** match the committed upstream SHA-256 manifest. Request integration is confined to the explicit adapter.

## Execution and grading

- Offline coding fixtures: known correct and incorrect Python and TypeScript solutions produced exactly **two passes and two failures**, using EvalPlus extended tests and the actual TypeScript compiler/verifier.
- All **20 selected repository reference solutions** passed every required upstream test with networking disabled. The committed set contains **12 Python and 8 TypeScript tasks**. Quick/standard/full use fixed prefixes of 2/5/20 tasks.
- Harbor's oracle agent passed through the actual custom Docker environment adapter. The pinned Terminus 2 transport also passed a no-inference integration check, including propagation of malformed-response failures.
- Both loaded runtime backends completed live coding, repository-agent, native BetterBench speed, and two-depth prefill smoke runs. Coding used four tasks per model and one attempt per task; a repeated coding run used the same settings. Repository smoke used two tasks per model with a deliberately reduced four-turn budget. Normal repository defaults remain 40 turns and 30 minutes per task.
- Inference adapters required real token counts and clean stream completion. Installation diagnostic failures and cancelled runs remained unscored with their evidence retained.

Task containers were inspected with networking disabled, no GPU, no Docker socket, no credentials, and only their own output mounts. The application container has no Docker socket; the controller owns Docker access. Production model services were not restarted or reconfigured.

## Application and recovery

- Actual browser launch and cancellation succeeded. A duplicated API submission returned the same run ID. A queued job survived a controller restart, and the active generation worker retained the same container identity without request replay.
- During a speed run, the application service was restarted. The browser reconnected its event stream without reloading, logs advanced, and the benchmark continued. `Last-Event-ID` replay was also verified.
- Completed results exported as valid ZIP archives. Baselines and a two-run comparison worked and exposed changed application/image revisions. Different benchmark metrics were rejected.
- All **43 historical run files** matched the pre-migration backup hashes after service recreation. Original report URLs returned successfully and rendered in a real browser.
- Real Service Portal updates fetched public Git history, ran the updater as UID/GID 1000:1000, built all five images, recreated the application/controller, and reported success only after both health checks passed. Verified jobs include `89c4af90-3176-4c0c-86fc-92c8efa16737` and `91df765e-4cdb-4328-9292-a508ad44cb1d`.
- A real update request during an active benchmark was refused. Database/image recovery backups were created under `data/backups/`; results and configuration remained in place. No global prune or `compose down` was used.

The operator guide is in [README.md](README.md); task preparation and recovery procedures are under [docs/](docs/).

## Coding sessions and visual suites — local validation, 2026-09-18

This extension was validated locally without deploying the application or restarting or reconfiguring the model services. The suite preparation receipt is local to this Docker host and is not a production rollout receipt.

- Complete application and vendored-engine suite: **251 passed, 3 skipped**. The skips are existing compiler integration tests that require the separate verifier's npm dependencies. New coverage includes durable review races and reconnection, planning permissions, cancellation, controller interruption, configuration drift, multiple compactions, bounded context recovery, malformed transport, measured timing, baseline compatibility, missing usage, prototype isolation and automatic deployment setup.
- Browser tests: **12 passed**, including session launch options, custom profiles, plan approval, revision requests, prototype sandboxing, completion/time results, narrow layouts, legacy profiles, deployment setup status and qualification retries. The production TypeScript/Vite build passed. The existing large-chunk advisory remains.
- Offline coding controls: all **18 expected outcomes** passed across six tasks. Every reference solution passed; every unchanged base and deliberately incomplete implementation failed a relevant private acceptance check. Evidence: `data/session-validation/prepare-41d0b83fc7/coding-sessions/`.
- Offline visual controls: all **18 expected outcomes** passed across six briefs, including Large multi-screen navigation. Reference prototypes passed functional interactions and desktop/mobile layout checks; empty and noninteractive prototypes failed. Evidence: `data/session-validation/prepare-408b083dcc/visual-design/`.
- All **12 pinned screenshots** passed manifest/hash validation. The renderer records Chromium 145.0.7632.6, a 960 × 640 viewport and device scale 1.
- A scripted reference session passed through the real Harbor agent/environment, read-only planning, fixed approval, source edits, fresh offline verification and patch export in seven model-action turns and one verification attempt. It used scripted responses, **not model inference**, and does not qualify a suite. Evidence: `data/session-validation/protocol-real-harbor-v1/`.
- Fixture source SHA-256: `da2a0f0d3242a055b87843d27bb9e7232c870230f47e10bacc6f88ebc83d7888`. Prepared image identity: `sha256:3e74f144f209d47ffe7eb3e00faa51f5c4d417ea466a5518d65e82c3c237f70a`. The issue-tracker and inventory base revisions are stored in `data/session-preparation.json`.

Live qualifications were submitted through the local controller at `http://127.0.0.1:19001`, targeting the existing Daytime runtime:

| Suite | Qualification run | Outcome |
| --- | --- | --- |
| Vision checks | `20260918T203505Z-2b556743` | Passed the selected screenshot check |
| Coding sessions | `20260918T203930Z-88187cf6` | Passed after one repair; 33 model turns, two verification attempts |
| Visual design | `20260918T204301Z-2319cdc5` | Queued behind other runtime work at this entry |

Coding sessions and Vision checks are locally qualified. Visual design remains gated until its live qualification passes. Queued or scripted checks are not presented as live model success. Current status and qualification receipts are available in the local run records; production deployment automatically performs preparation and qualification on its own Docker host and data directory. Operational instructions are in [docs/coding-sessions.md](docs/coding-sessions.md).

## Service Portal rollout validation

All six Compose images built locally using separate `:portal-check` tags. The updater image contains Git, Docker CLI, Compose, Python, SQLite, Harbor and the preparation script. Compose configuration preserves the existing single Portal entrypoint and automatically enables suite setup in the runner. This is compatible with the earlier updater's build/recreate commands on the first upgrade.

Tests cover deployment build failures, image restoration, failed health checks keeping maintenance active, refusal of active/reviewing benchmarks, setup's execution lock, interrupted setup cleanup, changed-image invalidation, runtime outages and duplicate qualification prevention. Existing compatible receipts skip setup, and failed live qualification is retried explicitly through the browser.

An isolated container rehearsal used the built application/controller images, the production read-only/capability restrictions, numeric non-root ownership, Docker socket group access and the same absolute host-data mount used in production. Both services became healthy while maintenance remained active. Releasing maintenance started automatic preparation, which passed all 36 coding/visual controls and 12 screenshot validations. A second recreation with the final images reused the identical preparation receipt, created no additional validation directories or inference requests, released the execution lock, and preserved the seeded legacy baseline and report. The rehearsal deliberately selected a nonexistent model to verify that unavailable-runtime qualification remains gated. Evidence: `data/portal-rehearsal/rehearsal-result.json`. Only the isolated rehearsal containers were stopped afterward; the production Portal button was not invoked.

## Live setup status correction — 2026-09-19 UTC

Read-only inspection of production after its Portal update confirmed that automatic qualification was running, while the setup receipt still said queued. The coordinator deliberately yields during active execution. Health and profile readiness now read current run progress without scheduling work; the banner lists each suite's phase, model turns, waiting/failure details, and a button to open its run. An unsuccessful completed smoke run is identified as not passed, and compatible qualification receipts unlock suites independently.

- Session/setup tests: **56 passed**, including progress while the coordinator is paused, idle-queue reasons, blocked/failed qualification, independent unlocks and stale preparation receipts.
- Browser tests: **13 passed**, including live setup details, failure/retry navigation and mobile layout. The production TypeScript/Vite build passed.
- Production inference was left running; no qualification was bypassed and no runtime configuration was changed.

## Explicit setup controls — 2026-09-19 UTC

The earlier display fix did not disable automatic work. Production rebuilt the session image and began preparation again after the next update. Automatic startup setup is now removed: the controller requires a durable Start setup request, and Stop setup cancels only its owned preparation and qualification runs. A controller restart or application update interrupts that request instead of replaying it; browser closure does not. The retired environment switch cannot enable automatic work.

- Full Python suite: **266 passed, 3 existing skips**. Coverage includes idle startup, legacy configuration and queued-job migration, explicit-start idempotency, stop/launch races, scoped cancellation, update-lock release, and restart/revision interruption.
- Browser suite: **14 passed**, including no setup mutation on app load/reload, explicit start/stop, persistent stopped state, run navigation and mobile layout. Production frontend and runner image builds passed.
- A real Docker controller rehearsal started idle with the legacy auto-setup variable set, began offline preparation only after an API start request, stopped and removed its owned containers, released the execution lock, and remained idle after restart. No model endpoint was reachable in this rehearsal and no inference jobs were queued. Both API and controller used Linux processes for SQLite WAL access. Evidence: `data/manual-setup-rehearsal/result.json`.
- On production, the existing runner was recreated with automatic setup disabled, without changing its application image or the model services. Preparation containers were removed, the update lock was confirmed free, the checkout remained clean, and no active or queued Bench Studio runs remained. The permanent code change is delivered through the user's normal Portal update workflow.
