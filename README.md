# Bench Studio

A local benchmark workbench for AI Runtime and an OpenAI-compatible Ollama router. Choose a loaded model, select a versioned profile, and run. Compare native throughput, executable coding checks, and repository issue resolution without mixing them into a universal score.

## Everyday use

Open the application on port **9001**. Select **New benchmark**, a loaded model (or both), and a profile. Both models run sequentially unless you explicitly select **Simultaneous · shared load**. Jobs wait for the runtime to be idle. Keep context changes and model loading in AI Runtime.

- **Quick smoke / Coding speed / Everyday mix:** BetterBench's existing workloads and weighted throughput, in tokens/second.
- **Context sweep:** prefill throughput plotted against actual prompt depth.
- **Coding checks:** one attempt per task; quick = 4, standard = 40, full = all eligible pinned tasks. Combined profiles split quick/standard evenly between Python and TypeScript. Python uses HumanEval+ with EvalPlus; TypeScript uses MultiPL-E HumanEval tests. Single-language profiles use 4 or 40 tasks from that language.
- **Repository tasks:** fixed local SWE-bench Pro subsets of 2, 5, or 20 reference-validated tasks with Harbor's Terminus 2 agent. Defaults: 40 turns and 30 minutes per task. These are **local subset results**, not SWE-bench Pro leaderboard scores.

Advanced settings can be saved as a new immutable custom profile. Reasoning defaults to the runtime's setting. Output limits and temperature affect results; keep them stable for repeatable comparisons. Unsupported parameters such as `top_k` are rejected.

Active jobs are visible above history. Open a job for live logs and progress; **Stop** cancels only that job. The browser can close while work continues. **Run again** opens the launch screen with the previous profile and shows the currently resolved model/context for review.

Completed runs expose native scores, per-task outcomes, configuration snapshots, original reports, and a ZIP export. **Set baseline** records a baseline per profile, size, mode, and target. Select two compatible runs to compare metrics and changed conditions. Failed, invalid, interrupted, and cancelled runs retain evidence but have no final score. An idle check is not an exclusive reservation: detected unrelated requests are flagged.

## Install

Linux x86-64, Docker Engine + Compose, UID/GID 1000, and a router exposing AI Runtime metadata are required. The app does not require or receive GPU devices. It observes model services already running on the host.

```sh
git clone https://github.com/astigmatism/bench-studio.git
cd bench-studio
cp .env.example .env
# Edit PROJECT_DIR, BIND_IP, LLM_ENDPOINT, RUNTIME_URL and DOCKER_GID.
mkdir -p data
SOURCE_REVISION=$(git rev-parse HEAD) docker compose --profile images build
SOURCE_REVISION=$(git rev-parse HEAD) docker compose up -d --wait reports runner
```

Use a host/LAN address reachable from task workers for the router URL, not `localhost`. `PROJECT_DIR` must be the absolute checkout path. Set `DOCKER_GID` from `stat -c %g /var/run/docker.sock`. Port 9001 serves both the interface and API. Keep this application on a trusted LAN; it has no user-account/authentication layer.

Coding datasets download from immutable upstream revisions during the image build and are checked against committed digests. Results, `.env`, credentials, and downloaded caches are excluded from Git.

Repository tasks require a separate preparation step before they can be launched. This downloads substantial CPU task images, prepares dependencies, checks reference solutions with networking disabled, and records exact image IDs. It makes no LLM requests. See [repository preparation](docs/repository-tasks.md).

## CLI

The CLI uses the same API and durable queue as the browser:

```sh
export BENCH_STUDIO_URL=http://127.0.0.1:9001
./bench models
./bench profiles
./bench run both --profile smoke
./bench run nighttime --profile coding-checks --size quick
./bench status
./bench logs RUN_ID --follow
./bench stop RUN_ID
```

Run `./bench --help` for all commands. Historical manifests under `data/runs` are imported automatically without rewriting their measurements. Original `/runs/RUN_ID/...` report URLs remain accessible.

## Updates and recovery

The `reports` service opts into Service Portal's **Update and restart** action. The prebuilt `local/bench-studio-updater:current` image runs `scripts/update-and-restart.sh` as 1000:1000. It validates Git origin/main/upstream and cleanliness, acquires the scheduler's maintenance lock, refuses active benchmarks, backs up SQLite and image identities, builds replacements, then reconciles both long-lived services with a bounded health wait. Queued jobs persist and require review if the application revision changed.

Source changes belong in Git; push to `main`, then use the Portal button. No global prune, volume removal, or application `compose down` is used. [Recovery instructions](docs/recovery.md) cover restoring the previous image and database backup.

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
