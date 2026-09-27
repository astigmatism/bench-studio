#!/usr/bin/env python3
"""Service Portal entrypoint: fail closed, preserve data, and verify health."""

import datetime
import fcntl
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    "git@github.com:astigmatism/bench-studio.git",
    "https://github.com/astigmatism/bench-studio.git",
    "https://github.com/astigmatism/bench-studio",
}


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
        or os.path.normpath(runner.get("environment", {}).get("DATA_ROOT", ""))
        != data
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
    run("docker", "info", "--format", "{{.ServerVersion}}")
    run("docker", "compose", "version")
    validate_compose_config()


def deploy():
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
        marker.write_text(
            json.dumps(
                {
                    "started_at": datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat(),
                    "job": os.environ.get("SERVICE_PORTAL_UPDATE_JOB_ID"),
                }
            )
        )
        safe_to_resume = True
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
        finally:
            if safe_to_resume:
                marker.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--check-config"]:
            validate_compose_config()
            print("Compose uses this checkout's absolute, shared data directory")
        elif len(sys.argv) == 1:
            deploy()
        else:
            raise RuntimeError("Usage: update.py [--check-config]")
    except Exception as e:
        print("Error: " + str(e), file=sys.stderr, flush=True)
        sys.exit(1)
