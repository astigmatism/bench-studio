# Benchmark validation v2

Bench Studio v2 execution adapters correct defects found in the September 18 coding and repository runs. Historical artifacts and measurements are immutable. The UI labels v1 runs and disallows comparisons across execution adapter/dataset versions.

Coding uses explicit TypeScript CommonJS compilation with pinned TypeScript and Node declarations. Exports and Node built-in imports are supported. The original MultiPL-E tests are retained except `HumanEval_92_any_int`, excluded in the versioned manifest because it requires different outputs for JavaScript-equivalent `3` and `3.0`. Full combined coding now contains 164 Python and 158 TypeScript tasks. Quick/standard remain four/40 tasks, selected deterministically.

The total output limit includes reasoning and the answer. Exhaustion, empty answers, compilation errors, test failures, and verifier timeouts are distinct outcomes. Exact repeated reasoning tails are flagged conservatively; this is a diagnostic, not a correctness judgment or an automatic retry. An optional per-request thinking limit can leave answer space within the total output allowance when the endpoint advertises budget support. Blank preserves runtime behavior. No runtime model, sampling defaults, or context configuration is changed. Raising output limits does not by itself fix model repetition.

Repository runs copy only task definitions (instruction, task configuration, environment, tests, solution) into per-run snapshots. Oracle/setup logs are excluded. Every input is checked for readability, symlinks rejected, script execution bits normalized on the new copy, image identities verified, and source hashes recorded before inference. This prevents old root-owned oracle logs from crashing Harbor's recursive checksum after earlier tasks finish.

Nested Harbor exceptions expose their underlying cause. Partial trial results remain visible, including completed passes, failures, and unfinished tasks; partial/infrastructure-failed runs never receive a final score. Historical repository trial artifacts are read on demand without rewriting their measurements.

A transient runtime readiness probe gets a bounded 90-second grace period. Model/container/configuration changes still invalidate immediately. New quality/agent requests wait for readiness; inference requests are never automatically replayed. Persistent readiness failure stops the benchmark with a clear error.

Validation uses isolated verifier containers without network access. Saved model outputs may be regraded only in a new validation directory, never in an original run directory. Model smoke tests use the scheduler and its idle gate. Deployment uses the existing Service Portal update contract and state backup.

## Repair validation

- 75 backend tests and six browser tests pass, including healthy/unhealthy identity drift, partial Harbor results, nested errors, dataset exclusion, Node imports/exports, and output-budget diagnostics.
- Network-isolated replay of saved responses: all five prior module/import failures pass. A known incorrect solution still fails, and a reasoning-only truncated response remains an output-exhaustion failure.
- All 20 pinned repository definitions pass readability/image checks and Harbor checksumming using clean per-run copies. The formerly crashing fifth task's reference solution completes offline with reward 1 and no exception.
- Validation outputs are stored separately under the deployment data directory; original run files are not regraded or overwritten.
