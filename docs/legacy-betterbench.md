# Historical BetterBench results

Bench Studio imports the original `data/runs/RUN_ID/manifest.json` documents without rewriting their measurements. Original HTML/JSON report paths remain available under `/runs/RUN_ID/`. Historical interrupted jobs retain partial evidence and have no final score.

The pinned workloads remain unchanged:

| Profile | Workload | Warmups / measured requests |
|---|---|---|
| `smoke` | Code, file edit, JSON | 1 / 5 per category |
| `coding` | Code, file edit, JSON | 3 / 20 per category |
| `standard` | Eight assistant workloads | 3 / 20 per category |
| `prefill` | Approximately 2K, 8K, 16K, 32K, 64K input tokens | 2 / 8 per depth |
| `prefill-smoke` | Approximately 2K, 8K input tokens | 1 / 2 per depth |

These BetterBench results measure throughput and latency. Executable correctness checks and repository-task grading are separate Bench Studio families.

The browser and current `./bench` CLI both use the application's durable scheduler. Refer to [the current operator guide](../README.md) for launch, cancellation, updates, and recovery. The old implementation under `scripts/legacy-bench` is retained as source history; it is not the supported command entrypoint.

Host addresses, runtime settings, GPU assignments, and downloaded results belong in the deployment's ignored configuration/data directories.
