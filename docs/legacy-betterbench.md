# BetterBench on Rosalina

Reports: **http://192.168.1.21:9001/** (also linked from the Service Portal).

This deployment measures routed inference latency and throughput. Code and file-editing
prompts are representative workloads; BetterBench does not compile or grade the answers.
The browser shows run history and reports. Start, follow and stop jobs over SSH.

## Start here

```bash
ssh astigmatism@192.168.1.21
cd ~/apps/betterbench
./bench models
./bench run nighttime --profile smoke
```

The last command prints a **RUN_ID** and returns immediately. The container continues
after SSH disconnects. Substitute that ID in:

```bash
./bench status
./bench status RUN_ID
./bench logs RUN_ID                # live log; Ctrl-C stops following only
./bench logs RUN_ID --no-follow    # print saved log and exit
./bench stop RUN_ID                # cancel this job's inference and worker
./bench wait RUN_ID                # wait; exit 0 only for a completed job
```

Open the report page while a job runs. It refreshes every 15 seconds. Running jobs
have logs and metadata; their report links appear after successful validation.
Each request logs its category, start, real token counts, latency and completion.
The report page does not start or stop jobs. `./bench stop` does.

## Workloads

| Profile | Workload | Warmups / measured requests |
|---|---|---|
| `smoke` | Code, file edit, JSON | 1 / 5 per category |
| `coding` (default) | Code, file edit, JSON | 3 / 20 per category |
| `standard` | Chat, code, file edit, JSON, math, prose, reasoning, summarization | 3 / 20 per category |
| `prefill` | Approximately 2K, 8K, 16K, 32K, 64K input tokens | 2 / 8 per depth |
| `prefill-smoke` | Approximately 2K, 8K input tokens | 1 / 2 per depth |

```bash
./bench run daytime --profile coding
./bench run nighttime --profile standard
./bench run both --profile smoke           # one service, then the other
./bench run both --profile coding --parallel # one request per service simultaneously
./bench run both --profile prefill
```

Each run first performs a short, unmeasured streaming compatibility request for each
target. This confirms real token usage and a complete terminal event. The measured
phases follow. Prefill and decode are separate jobs so stopping one cannot discard
the other's saved measurements. Concurrency sweeps are disabled: each production
service has one generation slot and its own FIFO queue.

The launcher refuses existing router work, backend processing, runtime maintenance,
unready models, and overlapping BetterBench jobs. Retry once idle; it does not
cancel existing work or change runtime admission settings. Keep other clients quiet
throughout a baseline. This is an idle check, not an exclusive reservation against
unrelated applications starting new work.

## Model changes through AI Runtime

The public inference endpoint is `http://192.168.1.21:11434/v1`.
Discovery aliases are `daytime` and `nighttime`. At installation:

| Alias | Canonical model | Context | GPU pair |
|---|---|---|---|
| daytime | qwen3.8-27b-q8_0 | 163,840 | RTX 3090 + RTX 4080 SUPER |
| nighttime | qwen3.8-27b-abliterated-q6_k | 131,072 | RTX 4080 + RTX 3080 Ti |

Use your usual AI Runtime controls to change models **between runs**. Its `daytime-27b`
profile selects the original Q8 model; `daytime` selects Flash-Next. `primary` ensures
the selected Daytime plus Nighttime pair. These are configuration selections, not
time schedules. BetterBench does not run these runtime controls for you.

On every launch, `bench` resolves the current alias, context and revision. The worker
sends the resolved canonical ID, periodically checks identity, and checks again at
completion. A model, container, runtime profile or relevant policy change invalidates
the run. The next launch automatically follows the new alias mapping.

Context discovery uses `x_ollama_router.context_window`; prefill also includes the
router's reserve plus template headroom. Oversized depths are skipped and visible
in the report. New aliases beyond these two would require adding a target to the
wrapper; changing models behind these existing aliases does not.

## Repeatability and interpretation

- BetterBench **0.6.0**, upstream commit `d00ad5ec8098c06584a88ec3468bacd37d5ed098`.
  The vendored upstream source is unmodified and its file hashes are recorded in
  `upstream-source.json`. The image pins its Python base digest and Python dependencies.
- Profiles request temperature **0.7**, top-p **0.95**, seed **42**, and unique prompt
  nonces. Prefill uses upstream's temperature 0. Pinning a seed improves repeatability;
  it does not guarantee identical output or timing across GPU/runtime changes.
- `top_k` is deliberately left unset in BetterBench because this router does not
  forward it. Backend defaults are captured in each manifest (both currently use 20).
- Thinking uses the service's default, currently enabled. No production reasoning,
  output, context or concurrency setting is changed. BetterBench's finite per-prompt
  output limits may stop within reasoning. A `length` finish is valid for throughput;
  a missing answer/TTFA is not evidence of a completed coding answer.
  BetterBench withholds a category's TTFA median until at least five measured
  responses reach an answer; a dash in these short smoke reports is expected.
- Results include router tokenization, transport and durable-archive overhead. They
  measure the API applications use, not raw llama.cpp engine speed.
- Only actual server token counts are accepted. Missing usage, invalid metrics,
  missing finish reasons and failed requests fail the job instead of publishing a
  successful summary. Update checks are disabled during benchmarking.
- Smoke samples are installation checks. Even 20 requests per category provide
  limited evidence for extreme percentiles; preserve BetterBench's sample warnings.
- The combined score uses upstream category weights. Read per-category results
  when comparing coding performance rather than relying only on that score.

## Compare saved results

```bash
./bench compare RUN_A RUN_B
./bench compare RUN_ID:daytime RUN_ID:nighttime
./bench compare RUN_A:daytime RUN_B:daytime --phase prefill
```

The wrapper compares medians and percentage changes for independent runs. It omits
the paired confidence intervals used by upstream's native `compare`, since these
runs were not interleaved pairs. Different models, GPU pairs, thinking behavior or sampling settings change
what the comparison means. A configuration mismatch is called out.

## Files, lifecycle and updates

All project files live in `/home/astigmatism/apps/betterbench`.
Each `data/runs/RUN_ID/` holds:

- `manifest.json`: status, resolved model identity, runtime revision, GPU/engine
  metadata, requested profile, and links to validated reports.
- `run.log`: durable request-level progress and errors.
- Per-target directories: discovery snapshots, compatibility check, actual phase
  configuration, JSON measurements, HTML report, and latest request progress.

Completed results persist across container recreation and host reboot. Workers do
not restart automatically. BetterBench does not checkpoint an unfinished phase;
an interrupted phase may have logs but no result JSON. The browser marks an active
job **unverified** if its heartbeat becomes more than 90 seconds old. `./bench status`
reconciles abruptly stopped workers against Docker and saves the interrupted state.
Use it after a host reboot to reconcile any previously running jobs.

```bash
docker compose ps
docker compose logs --tail 50 reports
docker compose up -d --wait reports
docker compose stop reports             # reports only; workers are independent
docker compose build                   # includes upstream + integration tests
docker compose up -d --wait reports     # apply a deliberately rebuilt image
```

Workers use non-root UID 1000, a read-only root filesystem, dropped capabilities,
and no GPU devices or Docker socket. They call the router over the container bridge.
The report server mounts the results read-only and binds only `192.168.1.21:9001`.
The host-side `bench` command owns Docker operations. No automatic upgrades or
recurring benchmarks are configured. Upstream changes should be reviewed and pinned
before rebuilding; do not edit vendored upstream code as an unnoticed local fork.

The small `invoke.py` integration logs and validates requests around the upstream
runner; it leaves upstream request timing and report calculations unchanged.
`tests/test_integration.py` covers discovery changes, busy-service guards, missing
usage, malformed terminal responses, partial prefill failures and report escaping.

## Remove only this experiment

First run `./bench status` and stop any active run IDs with `./bench stop RUN_ID`.
Then, from this project directory:

```bash
docker compose --profile jobs down --remove-orphans
docker image rm local/betterbench:0.6.0-home1
```

Those commands remove this Compose project's containers/network and its image;
the bind-mounted `data/` directory remains. Archive it if you want to retain results.
Delete `/home/astigmatism/apps/betterbench` only when you also want its source and
saved results gone. Do not use global Docker prune or stop the runtime/router.
