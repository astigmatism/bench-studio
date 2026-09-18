#!/usr/bin/env python3
"""Provision and oracle-check repository tasks. No LLM requests are made."""

import datetime, hashlib, json, os, shutil, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
CACHE = DATA / "repository-tasks"
CACHE.mkdir(parents=True, exist_ok=True)
VALIDATION = DATA / "repository-validation"
VALIDATION.mkdir(exist_ok=True)
SOURCE = ROOT / "datasets" / "cache" / "repository-candidates"
manifest = json.loads((ROOT / "datasets" / "repository-candidates.json").read_text())
published_path = ROOT / "datasets" / "repository-manifest.json"
published = json.loads(published_path.read_text()) if published_path.exists() else None
records = published["tasks"] if published else manifest["candidates"]
selected = []
rejected = []


def run(args, log, timeout=1800):
    with log.open("a") as f:
        f.write("\n$ " + " ".join(args) + "\n")
        f.flush()
        return subprocess.run(
            args, stdout=f, stderr=subprocess.STDOUT, timeout=timeout
        ).returncode


for record in records:
    if len(selected) >= 20:
        break
    # Interleaved deterministic candidates; oracle failures are replaced by the next eligible task.
    task = CACHE / record["id"]
    log = CACHE / (record["id"] + ".setup.log")
    for script in task.rglob("*.sh"):
        script.chmod(0o755)
    name = "bs-oracle-" + hashlib.sha256(record["id"].encode()).hexdigest()[:12]
    image = (
        "local/bench-studio-task:"
        + hashlib.sha256(record["id"].encode()).hexdigest()[:16]
    )
    cached = VALIDATION / (record["id"] + ".json")
    if cached.exists():
        shutil.copytree(
            SOURCE / record["id"] / "tests", task / "tests", dirs_exist_ok=True
        )
    for script in task.rglob("*.sh"):
        script.chmod(0o755)
    if cached.exists():
        evidence = json.loads(cached.read_text())
        if evidence.get("passed"):
            selected.append(record | evidence)
            print("Cached oracle:", record["id"], flush=True)
            continue
    shutil.copytree(SOURCE / record["id"], task, dirs_exist_ok=True)
    for script in task.rglob("*.sh"):
        script.chmod(0o755)
    print("Preparing", record["language"], record["id"], flush=True)
    try:
        if shutil.disk_usage(DATA).free < 80 * 1024**3:
            raise RuntimeError("Less than 80 GiB free; refusing more task downloads")
        base = record.get("base_digest", record["base_image"])
        if run(["docker", "pull", base], log, timeout=900):
            raise RuntimeError("Base image pull failed")
        obj = json.loads(
            subprocess.check_output(["docker", "image", "inspect", base], text=True)
        )[0]
        pinned = obj["RepoDigests"][0]
        df = task / "environment" / "Dockerfile"
        text = df.read_text()
        text = text.replace("FROM " + record["base_image"], "FROM " + pinned)
        df.write_text(text)
        if run(["docker", "build", "-t", image, str(task / "environment")], log):
            raise RuntimeError("Task image build failed")
        image_id = subprocess.check_output(
            ["docker", "image", "inspect", image, "--format", "{{.Id}}"], text=True
        ).strip()
        out = VALIDATION / record["id"]
        out.mkdir(exist_ok=True)
        out.chmod(0o777)
        command = [
            "docker",
            "run",
            "--name",
            name,
            "--label",
            "io.bench-studio.setup=true",
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "SETUID",
            "--cap-add",
            "SETGID",
            "--security-opt",
            "no-new-privileges",
            "--cpus",
            "2",
            "--memory",
            "8g",
            "--pids-limit",
            "512",
            "--mount",
            f"type=bind,src={task}/tests,dst=/tests,readonly",
            "--mount",
            f"type=bind,src={task}/solution,dst=/solution,readonly",
            "--mount",
            f"type=bind,src={out},dst=/logs/verifier",
            image,
            "bash",
            "-lc",
            "bash /solution/solve.sh && bash /tests/test.sh",
        ]
        code = run(command, log, timeout=1800)
        reward = (
            (out / "reward.txt").read_text().strip()
            if (out / "reward.txt").exists()
            else ""
        )
        if code or reward != "1":
            raise RuntimeError(
                "Reference patch did not pass all required tests offline"
            )
        # Pin task runtime to the exact built image ID. No image build during benchmarks.
        cfg = task / "task.toml"
        cfg.write_text(
            cfg.read_text().replace(
                "[environment]", '[environment]\ndocker_image = "' + image_id + '"'
            )
        )
        evidence = {
            "passed": True,
            "image_id": image_id,
            "base_digest": pinned,
            "oracle_checked_at": datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat(),
        }
        cached.write_text(json.dumps(evidence, indent=2))
        selected.append(record | evidence)
        print("PASS", len(selected), record["id"], flush=True)
    except Exception as e:
        print("EXCLUDED", record["id"], str(e), flush=True)
        rejected.append(record | {"reason": str(e)})
        if published:
            raise RuntimeError(
                "Pinned task failed validation; refusing substitution"
            ) from e
    finally:
        subprocess.run(
            ["docker", "rm", "-f", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        audit = manifest | {"tasks": selected, "excluded": rejected}
        (DATA / "repository-manifest.json").write_text(json.dumps(audit, indent=2))
    if len(selected) >= 20:
        break
(DATA / "repository-manifest.json").write_text(
    json.dumps(
        (published or manifest) | {"tasks": selected, "excluded": rejected}, indent=2
    )
)
if len(selected) < 20:
    raise SystemExit(
        f"Only {len(selected)} oracle-verified tasks available; review exclusions before publishing profiles"
    )
print("All 20 repository tasks passed reference-solution validation.", flush=True)
