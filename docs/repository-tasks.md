# Preparing repository tasks

The repository family is a local subset of SWE-bench Pro, not a full leaderboard evaluation. Candidate task IDs and all upstream revisions are committed under `datasets/`. Candidates are ordered by SHA256 of `42:instance_id`, interleaved between Python and TypeScript, excluding upstream-known invalid/timeout tasks. Select ten verified tasks per language; quick and standard use the first two and five of that fixed list.

The dataset's TypeScript tasks come from Tutanota; Python candidates span the available Python repositories. This limits what the language-specific score represents. Do not generalize a small subset to all repository work.

Preparation makes no model requests, but uses disk, network downloads, and CPU. Budget hundreds of GB for upstream task images. The provisioner stops downloading below 80 GiB free. It retains preparation and oracle logs in `data/repository-validation/` and exact runnable task definitions in `data/repository-tasks/`.

```sh
# Use Python 3.12 with pyarrow==25.0.1 installed.
python scripts/prepare-repositories.py
python scripts/bootstrap-repositories.py
```

`prepare-repositories.py` reconstructs definitions from pinned upstream data and scripts. `bootstrap-repositories.py` resolves base-image digests, builds dependencies, runs the gold patch with networking disabled, and rejects tasks whose required tests do not pass. It records image IDs in each task configuration. No gold solution or gold test is included in the pristine task image. The verifier installs gold tests after the agent finishes.

Tutanota's native Node dependencies are prebuilt before timing. Its verifier starts wall time at **2025-01-15 12:00:00 UTC**, using libfaketime with real monotonic time. This addresses an upstream test that otherwise fails late in the day, without changing the test assertions or required-test set. This local adapter condition is recorded in the committed selection manifest.

A failed oracle is an environment/setup issue, never a model failure. The application rejects launches without enough validated tasks. Changed task manifests make old comparisons incompatible. Do not replace a selected task silently: publish a new manifest/profile version, validate its reference solutions, and keep the previous manifest for old results.

For a direct environment integration check, run `scripts/validate-harbor.py` inside the runner image with the same data and Docker mounts as the controller. It uses Harbor's oracle agent and makes no model requests.
