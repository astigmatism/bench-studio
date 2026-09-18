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
