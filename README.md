# Bench Studio

A benchmark workbench for AI Runtime and an OpenAI-compatible Ollama router on the home network. Choose one loaded model, select a versioned profile, and run. Compare native throughput, executable coding checks, and repository issue resolution without mixing them into a universal score.

## Everyday use

Open the application on port **9001**. Select **New benchmark**, one currently advertised, healthy model, and a profile. Jobs wait for the runtime to be idle. Keep context changes and model loading in AI Runtime. Historical results remain available when their model is no longer loaded.

- **Quick smoke / Coding throughput / Everyday mix:** BetterBench's existing workloads and weighted throughput, in tokens/second.
- **Context sweep:** prefill throughput plotted against actual prompt depth.
- **Function checks:** one attempt per task; quick = 4, standard = 40, full = all eligible pinned tasks. Combined profiles split quick/standard evenly between Python and TypeScript. Python uses HumanEval+ with EvalPlus; TypeScript uses MultiPL-E HumanEval tests. Single-language profiles use 4 or 40 tasks from that language.
- **Coding sessions:** Small/Medium/Large features in curated full-stack apps, from read-only planning through approval, implementation and trusted verification. Measure commit-ready rate and active time together.
- **Vision checks / Visual design:** fixed screenshot understanding checks and clickable prototypes with screenshot critique, objective interaction checks and optional human review. New suites require [offline preparation and runtime qualification](docs/coding-sessions.md).
- **Repository tasks:** fixed local SWE-bench Pro subsets of 2, 5, or 20 reference-validated tasks with Harbor's Terminus 2 agent. Defaults: 40 turns and 30 minutes per task. These are **local subset results**, not SWE-bench Pro leaderboard scores.

Advanced settings can be saved as a new immutable custom profile. Reasoning defaults to the runtime's setting. Output limits and temperature affect results; keep them stable for repeatable comparisons. Unsupported parameters such as `top_k` are rejected.

Active jobs are visible above history. Open a job for live logs and progress; **Stop** cancels only that job. The browser can close while work continues. **Run again** opens the launch screen with the previous profile and matches its saved canonical model to current discovery. If that model is gone or changed, choose a current model before launching.

Completed runs expose native scores, per-task outcomes, configuration snapshots, original reports, and a ZIP export. **Set baseline** records a baseline per profile, size, mode, and target. Select two compatible runs to compare metrics and changed conditions. Failed, invalid, interrupted, and cancelled runs retain evidence but have no final score. An idle check is not an exclusive reservation: detected unrelated requests are flagged.

Completed runs open with a **Performance** scorecard: median output speed, first-token time, backend prompt-processing speed when available, and complete-request latency. History and task/request tables expose the same measurements. Output speed includes thinking and uses actual output tokens divided over the time after the first output; it is separate from BetterBench's existing weighted throughput score. Sample coverage and request failures stay visible. Expand **Performance details** for per-token latency, variability, successful-task timings, and the explicitly labeled prompt-throughput estimate.

Each eligible metric has a same-model rank and an all-model rank within matching workloads in this deployment. Equivalent custom-profile copies compare together; changes to tasks, budgets, execution mode, or benchmark/verifier versions form separate groups. Changes are relative to the previous eligible matching run for the canonical model, with recorded tuning differences available on each card. Pinned baselines continue to work independently. Rankings update when runs are added or deleted; a first result shows **#1 of 1**. Historical artifacts are never rewritten, and missing backend timing is not inferred from first-token latency. See [performance measurement details](docs/performance.md).

Run history contains finished runs. Select individual rows or use the header checkbox to **Select all**, then **Delete selected**. Confirming permanently removes the selected results, reports, logs, exports, review records, and their baseline references. Active, queued, and paused runs remain above history; stop them before deleting. Deleted legacy runs stay deleted after restarts. If file cleanup fails, the app reports it while keeping those runs out of history.

## Install

Linux x86-64, Docker Engine + Compose, and a reachable router exposing AI Runtime metadata are required. The app does not require or receive GPU devices. The router and models may run on another machine; the local Docker daemon is used only for Bench Studio workers and verification containers.

```sh
git clone https://github.com/astigmatism/bench-studio.git
cd bench-studio
cp .env.example .env
# Set PROJECT_DIR to this checkout's absolute path. Set BIND_IP to this host's
# LAN address for access from other machines, and set HOST_UID/HOST_GID/DOCKER_GID.
# Set LLM_ENDPOINT and RUNTIME_URL to the router host (see .env.example).
mkdir -p data
python3 scripts/update.py --check-config
SOURCE_REVISION=$(git rev-parse HEAD) docker compose --profile images build
SOURCE_REVISION=$(git rev-parse HEAD) docker compose up -d --wait reports runner
```

The production example uses `LLM_ENDPOINT=http://192.168.1.4:11434/v1` for inference and `RUNTIME_URL=http://192.168.1.4:11436/api/status` for readiness and model identity. Both models use the same router endpoint; port 11435 serves the router's health/UI service. Use the router machine's LAN address, not `localhost` or `host.docker.internal`. Check reachability from this host and from a bridge-networked Docker container before running benchmarks. `PROJECT_DIR` must be the absolute checkout path. Set `HOST_UID=$(id -u)`, `HOST_GID=$(id -g)`, and `DOCKER_GID=$(stat -c %g /var/run/docker.sock)` in `.env`; the first two must own `data/` and are passed to dynamically started workers. Port 9001 serves both the interface and API. Keep this application on a trusted LAN; it has no user-account/authentication layer.

Both long-lived services mount this host's `PROJECT_DIR/data` directory, which holds SQLite, run artifacts, backups, and preparation data across image rebuilds. Coding datasets download from immutable upstream revisions during the image build and are checked against committed manifests. Results, `.env`, credentials, and downloaded caches are excluded from Git. Each deployment has its own history; moving history between hosts is a separate operation.

Repository tasks require a separate preparation step before they can be launched. This downloads substantial CPU task images, prepares dependencies, checks reference solutions with networking disabled, and records exact image IDs. It makes no LLM requests. See [repository preparation](docs/repository-tasks.md).

## CLI

The CLI uses the same API and durable queue as the browser:

```sh
export BENCH_STUDIO_URL=http://127.0.0.1:9001
./bench models
./bench profiles
./bench run MODEL_ALIAS --profile smoke
./bench run MODEL_ALIAS --profile coding-checks --size quick
./bench status
./bench logs RUN_ID  # follows until the run finishes
./bench stop RUN_ID
```

Run `./bench --help` for all commands. Historical manifests under `data/runs` are imported automatically without rewriting their measurements. Original `/runs/RUN_ID/...` report URLs remain accessible.

## Updates and recovery

The `reports` service opts into Service Portal's **Update and restart** action. The prebuilt `local/bench-studio-updater:current` image runs `scripts/update-and-restart.sh` with the configured host UID/GID. It validates Git origin/main/upstream and cleanliness, checks the absolute shared data mounts, acquires the scheduler's maintenance lock, refuses active benchmarks and pending reviews, backs up SQLite and image identities, builds replacements, then reconciles both long-lived services with a bounded health wait. Queued jobs persist and require review if the application revision changed. Saved history and backups stay in `data/` throughout the update.

The same button builds the session environment, but **updates, restarts, and opening the app do not start preparation or benchmarks**. Open **Profiles → Eligibility**, then select **Check eligibility** to validate the bundled projects and tests on the Docker host. Eligibility checks run offline and make no model requests. Compatible preparation receipts are reused; changed fixtures or images require fresh validation. Once preparation passes, Coding sessions, Vision checks, and Visual design are available for normal benchmarking. If a selected profile needs an eligibility check, **Check eligibility** is also available directly on the New benchmark screen. That action runs offline checks only, keeps your selections, and enables **Queue benchmark** when preparation finishes.

**Model smoke tests are optional.** Enable the checkbox during eligibility checks or choose **Run optional model checks** afterward to run one Small unattended task per suite. Leave `SESSION_SMOKE_TARGET` empty to select a currently eligible model, or set it to a current model alias. Their pass/fail results never determine profile availability. A model failing a task is benchmark evidence. Runtime availability, configuration, and vision support are checked when launching and executing a run.

The collapsed **Eligibility** panel in Profiles separates fixture preparation from model results, shows completed preparation checks, and reports live generation activity. **Stop eligibility check** stops preparation and its optional smoke runs, releasing the update lock after cleanup. Other user benchmarks are unaffected. Browser closure preserves explicit requests; controller restarts and updates interrupt them without replay. Compatible prepared suites remain available across restarts. Failed model runs retain evidence and can be retried with **Run again**.

Source changes belong in Git; push to `main`, then use the Portal button. Operator preparation writes ignored caches and verifies committed manifests, so it does not make the source checkout dirty. Local source edits still fail the updater's strict clean-source check. No global prune, volume removal, or application `compose down` is used. [Recovery instructions](docs/recovery.md) cover restoring the previous image and database backup.

## Development

```sh
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt -r test-requirements.txt
PYTHONPATH=.:vendor .venv/bin/pytest tests vendor/tests
cd frontend
npm ci
npx playwright install chromium
npm run build
npx playwright test
```

Architecture and isolation: [docs/architecture.md](docs/architecture.md). Upstream pins and attribution: [THIRD_PARTY.md](THIRD_PARTY.md). Validation evidence: [VALIDATION.md](VALIDATION.md).
