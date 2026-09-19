# Update and recovery

Exactly one long-lived Compose service (`reports`) carries Service Portal's four update labels. The updater image is built in advance and contains Git, Docker CLI, Compose, Python/SQLite. Portal mounts the checkout at its original absolute path and supplies Docker's supplementary group. UID/GID defaults to 1000:1000. No SSH keys or GitHub token are needed to fetch the public repository.

The updater refuses dirty source, detached/wrong branches, unexpected origin/upstream, divergent/unpublished local history, or an active benchmark. An atomic `.execution.lock` shared with the scheduler prevents a launch racing maintenance. `.maintenance` pauses new starts. A queued job remains in SQLite. It is blocked for review if its saved application revision no longer matches after an update.

Update sequence: validate checkout/tools; lock and check active jobs and pending reviews; fetch and fast-forward; back up SQLite with the live backup API and save previous image identities; validate Compose; build all six images (including the session environment); reconcile `reports` and `runner` with a 150-second health wait. A failed build restores previous image tags before releasing maintenance. Failed restoration or failed deployment health keeps maintenance active for recovery. Errors remain nonzero for Portal. The script does not use global prune, remove volumes, or bring down the application stack.

After a healthy restart, setup remains idle until you select **Start setup** in Bench Studio. Updates and restarts never initiate preparation or qualification; the retired `SESSION_AUTO_SETUP` setting is ignored. Existing compatible receipts are reused. Portal success certifies application/controller health, not model task success. If an explicitly started setup blocks an update, select **Stop setup** and wait for its cleanup to finish, then retry the Portal action. This stops setup-owned preparation and smoke runs while retaining evidence; ordinary user benchmarks still require their own Stop action. Controller interruption requires a fresh Start setup action. Failed live model qualifications can also be retried with **Run again**. See [suite setup](coding-sessions.md).

Backups are under `data/backups/TIMESTAMP/`, containing `studio.sqlite3` and `deployment.json`. Raw `data/runs/`, configuration, and downloaded task environments remain in place. Keep an external backup of `data/` for disk failure recovery.

## Roll back after a failed deployment

1. Confirm no benchmark is active. Stop only this application's services with `docker compose stop reports runner`.
2. Preserve the current database and its `-wal`/`-shm` files in a new recovery directory. Read `deployment.json` to identify the previous source revision and image IDs.
3. Restore the backup `studio.sqlite3` into `data/`; remove stale `studio.sqlite3-wal` and `studio.sqlite3-shm` only after both services have stopped and their current copies have been preserved. Keep `data/runs` untouched.
4. Tag the previous image IDs back to the tags recorded in `deployment.json` (older backups contain four images; new backups can contain six). Use the matching previous Compose source from Git in a separate clean recovery checkout, configured with the original absolute `PROJECT_DIR` and data mount. Do not discard a dirty checkout.
5. Run `docker compose up -d --no-build --wait --wait-timeout 150 reports runner` using the matching configuration. Check `/api/health`, the runner health status, retained reports, and the footer revision.

The initial migration also preserves a complete pre-Studio source and data archive outside the checkout. A crashed updater can leave `.maintenance`; remove it only after verifying its Portal job/container is no longer running and no other updater owns `.execution.lock`. Interrupted benchmark evidence remains visible; use Run again to create a fresh run.

## Restart behavior

An app-only restart preserves jobs and reconnects browser events. A controller restart reconciles surviving generation/verifier containers. It does not replay requests. Harbor's in-process trusted harness cannot survive controller termination; its trial artifacts are retained, its task containers are stopped, and the job is marked interrupted without a final score.
