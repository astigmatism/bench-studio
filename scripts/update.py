#!/usr/bin/env python3
"""Service Portal entrypoint for update, start, and scoped stop operations."""

import contextlib
import datetime
import fcntl
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    "git@github.com:astigmatism/bench-studio.git",
    "https://github.com/astigmatism/bench-studio.git",
    "https://github.com/astigmatism/bench-studio",
}
TERMINAL = ("completed", "failed", "interrupted", "invalid", "cancelled")
SERVICES = ("reports", "runner")


def run(*args, check=True, capture=True, env=None):
    p = subprocess.run(list(args), cwd=ROOT, env=env, text=True, capture_output=capture)
    if check and p.returncode:
        raise RuntimeError(
            (p.stderr if capture else "") or "Command failed: " + " ".join(args)
        )
    return p


def validate_compose_config(env=None):
    """Ensure both services use this checkout's durable host data directory."""
    config = json.loads(
        run("docker", "compose", "config", "--format", "json", env=env).stdout
    )
    services = config["services"]
    reports, runner = services["reports"], services["runner"]
    project = runner.get("environment", {}).get("PROJECT_DIR", "")
    if not Path(project).is_absolute() or Path(project).resolve() != ROOT.resolve():
        raise RuntimeError(
            "PROJECT_DIR must be the absolute path of this checkout: " + str(ROOT)
        )
    data = str(ROOT / "data")

    def has_bind(service, target):
        return any(
            volume.get("type") == "bind"
            and os.path.normpath(volume.get("source", "")) == data
            and os.path.normpath(volume.get("target", "")) == target
            for volume in service.get("volumes", [])
        )

    if not has_bind(reports, "/data") or not has_bind(runner, data):
        raise RuntimeError(
            "reports and runner must bind the same PROJECT_DIR/data host path"
        )
    if (
        reports.get("environment", {}).get("DATA_ROOT") != "/data"
        or os.path.normpath(runner.get("environment", {}).get("DATA_ROOT", "")) != data
    ):
        raise RuntimeError("DATA_ROOT must match each service's data bind mount")
    uid = runner.get("environment", {}).get("HOST_UID", "")
    gid = runner.get("environment", {}).get("HOST_GID", "")
    if (
        not uid.isdecimal()
        or not gid.isdecimal()
        or int(uid) > 2**31 - 1
        or int(gid) > 2**31 - 1
    ):
        raise RuntimeError("HOST_UID and HOST_GID must be valid numeric host IDs")
    user = uid + ":" + gid
    if reports.get("user") != user or runner.get("user") != user:
        raise RuntimeError("Service user and worker HOST_UID/HOST_GID must match")


def runtime_preflight():
    if not shutil.which("docker"):
        raise RuntimeError("Missing required executable: docker")
    if not (ROOT / "compose.yaml").exists():
        raise RuntimeError("Missing compose.yaml")
    run("docker", "info", "--format", "{{.ServerVersion}}")
    run("docker", "compose", "version")
    validate_compose_config()


@contextlib.contextmanager
def lifecycle_lock():
    """Serialize Portal actions without waiting for long offline preparation."""
    data = ROOT / "data"
    data.mkdir(exist_ok=True)
    with (data / ".lifecycle.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another Bench Studio lifecycle action is running")
        yield


def atomic_json_file(path, value):
    """Replace an on-disk lifecycle state only after the JSON is durable."""
    fd, name = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + "-")
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(value, out)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.chmod(name, 0o644)
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def atomic_marker(value):
    atomic_json_file(ROOT / "data" / ".maintenance", value)


def read_marker():
    marker = ROOT / "data" / ".maintenance"
    return json.loads(marker.read_text()) if marker.exists() else None


def utc_time(value):
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(datetime.timezone.utc) if parsed.tzinfo else None
    except (AttributeError, ValueError):
        return None


def setup_snapshot():
    path = ROOT / "data" / "session-setup.json"
    if not path.exists():
        return None
    setup = json.loads(path.read_text())
    owner, request_id = setup.get("owner"), setup.get("request_id")
    if not owner or not request_id:
        return None
    if not all(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", part) for part in (owner, request_id)
    ):
        raise RuntimeError("Unsafe eligibility preparation owner or request ID")
    return {
        "owner": owner,
        "request_id": request_id,
        "preparing": setup.get("phase") in {"requested", "preparing", "stopping"},
        "active": setup.get("phase")
        in {
            "requested",
            "preparing",
            "stopping",
            "qualifying",
            "waiting",
            "waiting_for_preparation",
        },
    }


def capture_stop():
    """Lock SQLite before publishing the guard so launches cannot miss the snapshot."""
    data = ROOT / "data"
    db = data / "studio.sqlite3"
    marker = read_marker()
    if marker and marker.get("action") not in {None, "stop", "update"}:
        raise RuntimeError("Unrecognized maintenance guard; inspect it before stopping")
    captured = (
        {row["id"]: row for row in marker.get("captured", [])}
        if marker and marker.get("action") == "stop"
        else {}
    )
    requested_at = (
        marker.get("requested_at")
        if marker and marker.get("action") == "stop"
        else os.environ.get("SERVICE_PORTAL_ACTION_REQUESTED_AT")
    )
    cutoff = utc_time(requested_at)
    if requested_at and cutoff is None:
        raise RuntimeError("Invalid SERVICE_PORTAL_ACTION_REQUESTED_AT timestamp")
    connection = sqlite3.connect(db, timeout=30) if db.exists() else None
    try:
        if connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
            if not {"id", "status", "idempotency_key"} <= columns:
                raise RuntimeError("Run database schema is incomplete")
            for row in connection.execute(
                "SELECT id,status,document,idempotency_key FROM runs"
            ):
                unfinished = row["status"] not in TERMINAL
                finished = None
                if cutoff and not unfinished:
                    try:
                        finished = utc_time(
                            json.loads(row["document"]).get("finished_at")
                        )
                    except (TypeError, json.JSONDecodeError):
                        pass
                if unfinished or (cutoff and finished and finished >= cutoff):
                    captured[row["id"]] = {
                        "id": row["id"],
                        "idempotency_key": row["idempotency_key"],
                    }
        setup = (marker or {}).get("setup")
        guard = {
            "action": "stop",
            "started_at": (marker or {}).get("started_at")
            or datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "requested_at": requested_at,
            "job": os.environ.get("SERVICE_PORTAL_UPDATE_JOB_ID"),
            "captured": list(captured.values()),
            "setup": setup,
        }
        atomic_marker(guard)
        for rid in captured:
            if not isinstance(rid, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]*", rid
            ):
                raise RuntimeError("Unsafe run ID in stop snapshot: " + repr(rid))
        if setup and not all(
            isinstance(setup.get(part), str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", setup[part])
            for part in ("owner", "request_id")
        ):
            raise RuntimeError("Unsafe eligibility preparation owner or request ID")
        if not guard["setup"]:
            setup = setup_snapshot()
            if setup:
                guard["setup"] = setup
                atomic_marker(guard)
        if connection:
            connection.commit()
        return guard
    except BaseException:
        if connection:
            connection.rollback()
        raise
    finally:
        if connection:
            connection.close()


def remove_transient_containers(guard):
    ids = set()
    labels = ["io.bench-studio.project=" + str(ROOT)]
    labels.extend("io.bench-studio.run=" + row["id"] for row in guard["captured"])
    owners = set()
    if guard.get("setup"):
        owners.add(guard["setup"]["owner"])
    setup = ROOT / "data" / "session-setup.json"
    if setup.exists():
        owner = json.loads(setup.read_text()).get("owner")
        if owner:
            owners.add(owner)
    labels.extend("io.bench-studio.preparation=" + owner for owner in sorted(owners))
    for label in labels:
        ids.update(
            run("docker", "ps", "-aq", "--filter", "label=" + label).stdout.split()
        )
    if ids:
        run("docker", "rm", "-f", *sorted(ids))


def delete_captured_records(guard):
    db = ROOT / "data" / "studio.sqlite3"
    if not db.exists():
        return
    with contextlib.ExitStack() as stack:
        connection = stack.enter_context(
            contextlib.closing(sqlite3.connect(db, timeout=30))
        )
        stack.enter_context(connection)
        connection.execute("BEGIN IMMEDIATE")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "runs" not in tables:
            raise RuntimeError("Run database schema is incomplete")
        if "deleted_runs" not in tables:
            connection.execute(
                "CREATE TABLE deleted_runs(id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE)"
            )
        if "state" in tables:
            row = connection.execute(
                "SELECT document FROM state WHERE key='session_setup_control'"
            ).fetchone()
            if row:
                control = json.loads(row[0])
                if control.get("active"):
                    control.update(
                        active=False,
                        outcome="stopped",
                        stopped_at=datetime.datetime.now(
                            datetime.timezone.utc
                        ).isoformat(),
                    )
                    connection.execute(
                        "UPDATE state SET document=? WHERE key='session_setup_control'",
                        (json.dumps(control, separators=(",", ":")),),
                    )
        for row in guard["captured"]:
            rid, key = row["id"], row["idempotency_key"]
            connection.execute(
                "INSERT OR IGNORE INTO deleted_runs(id,idempotency_key) VALUES(?,?)",
                (rid, key),
            )
            if not connection.execute(
                "SELECT 1 FROM deleted_runs WHERE id=?", (rid,)
            ).fetchone():
                raise RuntimeError("Could not retain deletion tombstone for " + rid)
            for table in ("baselines", "session_reviews", "events"):
                if table in tables:
                    connection.execute(f"DELETE FROM {table} WHERE run_id=?", (rid,))
            connection.execute("DELETE FROM runs WHERE id=?", (rid,))


def delete_captured_artifacts(guard):
    data = ROOT / "data"
    for row in guard["captured"]:
        rid = row["id"]
        for path in (data / "runs" / rid, data / "exports" / (rid + ".zip")):
            if path.parent.is_symlink():
                raise RuntimeError(
                    "Artifact directory is a symlink: " + str(path.parent)
                )
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.exists():
                shutil.rmtree(path)
    setup = guard.get("setup")
    if setup and setup.get("preparing"):
        path = data / "session-validation" / setup["owner"] / setup["request_id"]
        receipt = data / "session-preparation.json"
        saved = json.loads(receipt.read_text()) if receipt.exists() else {}
        evidence = saved.get("evidence")
        if not evidence or not Path(evidence).is_relative_to(path):
            if any(parent.is_symlink() for parent in (path.parent, path.parent.parent)):
                raise RuntimeError(
                    "Preparation directory is a symlink: " + str(path.parent)
                )
            if path.is_symlink():
                path.unlink()
            elif path.exists():
                shutil.rmtree(path)


def reset_setup_status(guard):
    setup = guard.get("setup")
    path = ROOT / "data" / "session-setup.json"
    if not setup or not setup.get("active") or not path.exists():
        return
    state = json.loads(path.read_text())
    if state.get("request_id") != setup["request_id"]:
        return
    state.update(
        phase="paused",
        detail="Eligibility checking stopped with Bench Studio. Select Check eligibility to run it again.",
        request_id=None,
        suites={},
    )
    for key in ("progress_path", "preparation_progress", "log"):
        state.pop(key, None)
    atomic_json_file(path, state)


def scrub_preparation_receipt(guard):
    path = ROOT / "data" / "session-preparation.json"
    if not path.exists():
        return
    receipt = json.loads(path.read_text())
    captured = {row["id"] for row in guard["captured"]}
    changed = False
    for suite in receipt.get("suites", {}).values():
        if suite.get("qualified_run") in captured:
            del suite["qualified_run"]
            changed = True
    if changed:
        atomic_json_file(path, receipt)


def finish_stop(guard):
    """Safe to retry after a crash at any point; retain the guard until Start succeeds."""
    run("docker", "compose", "stop", *SERVICES, capture=False)
    remove_transient_containers(guard)
    delete_captured_records(guard)
    delete_captured_artifacts(guard)
    scrub_preparation_receipt(guard)
    reset_setup_status(guard)
    print(
        "Bench Studio stopped; unfinished jobs and transient workers removed",
        flush=True,
    )


def stop():
    with lifecycle_lock():
        guard = capture_stop()
        runtime_preflight()
        finish_stop(guard)


def start():
    with lifecycle_lock():
        runtime_preflight()
        marker = read_marker()
        if marker:
            if marker.get("action") != "stop":
                raise RuntimeError(
                    "Maintenance is active after an update; resolve it before Start"
                )
            guard = capture_stop()
            finish_stop(guard)
        run(
            "docker",
            "compose",
            "up",
            "-d",
            "--no-build",
            "--wait",
            "--wait-timeout",
            "150",
            *SERVICES,
            capture=False,
        )
        (ROOT / "data" / ".maintenance").unlink(missing_ok=True)
        print("Bench Studio started; reports and runner are healthy", flush=True)


def preflight():
    for tool in ["git", "docker", "python3"]:
        if not shutil.which(tool):
            raise RuntimeError("Missing required executable: " + tool)
    if run("git", "status", "--porcelain").stdout.strip():
        raise RuntimeError(
            "Refusing dirty checkout; preserve and commit local changes first"
        )
    if (
        run("git", "symbolic-ref", "--short", "HEAD", check=False).stdout.strip()
        != "main"
    ):
        raise RuntimeError("Refusing detached HEAD or wrong branch; expected main")
    if run("git", "remote", "get-url", "origin").stdout.strip() not in EXPECTED:
        raise RuntimeError("Refusing unexpected origin")
    if (
        run(
            "git", "rev-parse", "--abbrev-ref", "@{upstream}", check=False
        ).stdout.strip()
        != "origin/main"
    ):
        raise RuntimeError("Refusing unexpected upstream; expected origin/main")
    if not (ROOT / "compose.yaml").exists():
        raise RuntimeError("Missing compose.yaml")
    runtime_preflight()


def _deploy_locked():
    preflight()
    data = ROOT / "data"
    data.mkdir(exist_ok=True)
    with (data / ".execution.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(
                "Refusing: another update or launch holds the maintenance lock"
            )
        prior_marker = read_marker()
        stopped_before = bool(prior_marker and prior_marker.get("action") == "stop")
        if prior_marker and prior_marker.get("action") not in {None, "stop", "update"}:
            raise RuntimeError(
                "Unrecognized maintenance guard; inspect it before updating"
            )
        if stopped_before:
            # A previous Stop may have committed tombstones before file cleanup failed.
            # Keep its captured IDs and guard until the replacement services are healthy.
            finish_stop(capture_stop())
        db = data / "studio.sqlite3"
        if db.exists():
            with sqlite3.connect(db, timeout=15) as c:
                active = c.execute(
                    "SELECT id FROM runs WHERE status IN ('starting','running','grading','stopping','awaiting_review','resume_queued') LIMIT 1"
                ).fetchone()
                if active:
                    raise RuntimeError(
                        "Refusing update while benchmark is active: " + active[0]
                    )
        marker = data / ".maintenance"
        if not stopped_before:
            atomic_marker(
                {
                    "action": "update",
                    "started_at": datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat(),
                    "job": os.environ.get("SERVICE_PORTAL_UPDATE_JOB_ID"),
                }
            )
        safe_to_resume = True
        successful = False
        try:
            before = run("git", "rev-parse", "HEAD").stdout.strip()
            print("Fetching public upstream", flush=True)
            run(
                "git",
                "fetch",
                "https://github.com/astigmatism/bench-studio.git",
                "main:refs/remotes/origin/main",
            )
            ahead = (
                run(
                    "git",
                    "merge-base",
                    "--is-ancestor",
                    "HEAD",
                    "origin/main",
                    check=False,
                ).returncode
                == 0
            )
            if not ahead:
                raise RuntimeError(
                    "Refusing divergent, rewritten, or unpublished local history"
                )
            run("git", "merge", "--ff-only", "origin/main")
            revision = run("git", "rev-parse", "HEAD").stdout.strip()
            env = dict(os.environ, SOURCE_REVISION=revision)
            validate_compose_config(env=env)
            backup = (
                data
                / "backups"
                / datetime.datetime.now(datetime.timezone.utc).strftime(
                    "%Y%m%dT%H%M%SZ"
                )
            )
            backup.mkdir(parents=True, exist_ok=True)
            if db.exists():
                with (
                    sqlite3.connect(db) as source,
                    sqlite3.connect(backup / "studio.sqlite3") as dest,
                ):
                    source.backup(dest)
            evidence = {
                "before_revision": before,
                "new_revision": revision,
                "images": {},
            }
            for image in [
                "local/bench-studio:current",
                "local/bench-studio-runner:current",
                "local/bench-studio-worker:current",
                "local/bench-studio-verifier:current",
                "local/bench-studio-updater:current",
                "local/bench-studio-session:current",
            ]:
                p = run(
                    "docker",
                    "image",
                    "inspect",
                    image,
                    "--format",
                    "{{.Id}}",
                    check=False,
                )
                if p.returncode == 0:
                    evidence["images"][image] = p.stdout.strip()
            (backup / "deployment.json").write_text(json.dumps(evidence, indent=2))
            run("docker", "compose", "--profile", "images", "config", "-q", env=env)
            print(
                "Building replacement images while the current application stays available",
                flush=True,
            )
            try:
                run(
                    "docker",
                    "compose",
                    "--profile",
                    "images",
                    "build",
                    capture=False,
                    env=env,
                )
            except Exception:
                # Compose may retag some images before another target's build fails.
                # Keep the still-running application on its previous execution images.
                for image, image_id in evidence["images"].items():
                    restored = run(
                        "docker", "image", "tag", image_id, image, check=False
                    )
                    if restored.returncode:
                        safe_to_resume = False
                if not safe_to_resume:
                    print(
                        "Image restoration failed; maintenance remains active. Follow the recovery guide.",
                        flush=True,
                    )
                raise
            print(
                "Recreating application and controller; waiting for health", flush=True
            )
            try:
                run(
                    "docker",
                    "compose",
                    "up",
                    "-d",
                    "--force-recreate",
                    "--wait",
                    "--wait-timeout",
                    "150",
                    "reports",
                    "runner",
                    capture=False,
                    env=env,
                )
            except Exception:
                safe_to_resume = False
                print(
                    "Deployment health failed; maintenance remains active. Follow the recovery guide.",
                    flush=True,
                )
                raise
            print("Success: healthy Bench Studio " + revision, flush=True)
            print(
                "No benchmark checks start automatically. Use Check eligibility in Bench Studio to validate fixtures. Model smoke tests are optional and do not gate benchmarking.",
                flush=True,
            )
            successful = True
        finally:
            if successful or (safe_to_resume and not stopped_before):
                marker.unlink(missing_ok=True)


def deploy():
    with lifecycle_lock():
        _deploy_locked()


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--check-config"]:
            validate_compose_config()
            print("Compose uses this checkout's absolute, shared data directory")
        elif len(sys.argv) == 1:
            deploy()
        elif sys.argv[1:] == ["start"]:
            start()
        elif sys.argv[1:] == ["stop"]:
            stop()
        else:
            raise RuntimeError("Usage: update.py [--check-config|start|stop]")
    except Exception as e:
        print("Error: " + str(e), file=sys.stderr, flush=True)
        sys.exit(1)
