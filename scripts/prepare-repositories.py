#!/usr/bin/env python3
"""Build a deterministic candidate set from pinned SWE-bench Pro records and scripts."""

import hashlib, json, sys, re, urllib.request, tarfile, io
from pathlib import Path
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "datasets" / "cache" / "repository-candidates"
OUT.mkdir(parents=True, exist_ok=True)
HF = "7ab5114912baf22bb098818e604c02fe7ad2c11f"
SCRIPTS = "ca10a60a5fcae51e6948ffe1485d4153d421e6c5"
HARBOR = "b07f3bfb2c5730c50119c6c84e0e4d8572d9a7f2"
raw = urllib.request.urlopen(
    f"https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro/resolve/{HF}/data/test-00000-of-00001.parquet"
).read()
rows = pq.read_table(io.BytesIO(raw)).to_pylist()
archive = tarfile.open(
    fileobj=io.BytesIO(
        urllib.request.urlopen(
            f"https://codeload.github.com/scaleapi/SWE-bench_Pro-os/tar.gz/{SCRIPTS}"
        ).read()
    )
)
files = {
    m.name.split("/", 1)[1]: archive.extractfile(m).read()
    for m in archive.getmembers()
    if m.isfile() and "/run_scripts/" in m.name
}
readme = (
    urllib.request.urlopen(
        f"https://raw.githubusercontent.com/harbor-framework/harbor/{HARBOR}/adapters/swebenchpro/README.md"
    )
    .read()
    .decode()
)
known_bad = set(
    re.findall(
        r"instance_[a-zA-Z0-9_-]+", readme.split("Known Issues and Constraints")[-1]
    )
)
langs = {
    l: sorted(
        [
            r
            for r in rows
            if r["repo_language"] == l and r["instance_id"] not in known_bad
        ],
        key=lambda r: hashlib.sha256(("42:" + r["instance_id"]).encode()).hexdigest(),
    )
    for l in ["python", "ts"]
}
manifest = {
    "dataset_revision": HF,
    "scripts_revision": SCRIPTS,
    "adapter_revision": HARBOR,
    "dataset_sha256": hashlib.sha256(raw).hexdigest(),
    "selection": "SHA256(42:instance_id), interleaved Python/TypeScript, excluding upstream known broken tasks; oracle validation required",
    "candidates": [],
}
# Extra Python candidates provide replacements if a task cannot execute offline.
for i in range(20):
    for lang in ["python", "ts"]:
        if i >= len(langs[lang]):
            continue
        r = langs[lang][i]
        iid = r["instance_id"]
        directory = OUT / iid.lower()
        for d in ["environment", "tests", "solution"]:
            (directory / d).mkdir(parents=True, exist_ok=True)
        tag = r["dockerhub_tag"]
        image = "jefzda/sweap-images:" + tag
        (directory / "record.json").write_text(json.dumps(r, indent=2))
        (directory / "instruction.md").write_text(
            r["problem_statement"]
            + "\n\nRequirements:\n"
            + (r.get("requirements") or "")
            + "\n\nInterface:\n"
            + (r.get("interface") or "")
        )
        (directory / "tests" / "config.json").write_text(json.dumps(r))
        for f in ["run_script.sh", "parser.py"]:
            text = (
                files["run_scripts/" + iid + "/" + f]
                .decode()
                .replace(
                    "npx jest --verbose --silent",
                    "npx jest --verbose --silent --maxWorkers=1 --forceExit",
                )
            )
            (directory / "tests" / f).write_text(text)
        (directory / "tests" / "upstream-test.sh").write_text(
            (ROOT / "third_party/harbor-swebenchpro/test.sh").read_text()
        )
        (directory / "tests" / "test.sh").write_text(
            '#!/bin/bash\nbash /tests/upstream-test.sh\nresult=$?\nchmod -R a+rX /logs/verifier\nexit "$result"\n'
        )
        (directory / "solution" / "solve.sh").write_text(
            (ROOT / "third_party/harbor-swebenchpro/solve.sh")
            .read_text()
            .replace("{patch}", (r["patch"] or "").rstrip() + "\n")
        )
        # The pristine repository excludes gold test changes. Gold tests are installed by the verifier only.
        dockerfile = f'FROM {image}\nENTRYPOINT []\nWORKDIR /app\nRUN git reset --hard {r["base_commit"]} && git clean -fd && git checkout {r["base_commit"]}\nRUN apt-get update && apt-get install -y --no-install-recommends tmux && rm -rf /var/lib/apt/lists/*\nRUN mkdir -p /logs\n'
        if lang == "ts":
            wrapper = directory / "tests" / "test.sh"
            wrapper.write_text(
                wrapper.read_text().replace(
                    "bash /tests/upstream-test.sh",
                    "env TZ=UTC LD_PRELOAD=/usr/lib/x86_64-linux-gnu/faketime/libfaketime.so.1 FAKETIME='@2025-01-15 12:00:00' FAKETIME_DONT_FAKE_MONOTONIC=1 bash /tests/upstream-test.sh",
                )
            )
            dockerfile = dockerfile.replace("tmux &&", "tmux libfaketime &&")
            dockerfile += "RUN cd /app && ./node_modules/.bin/node-gyp install --ensure\nRUN cd /app/test && node test > /tmp/dependency-preparation.log 2>&1 || true\n"
        (directory / "environment" / "Dockerfile").write_text(dockerfile)
        task = f"""schema_version = "1.0"
[task]
name = "bench-studio/{iid.lower()}"
authors = [{{ name = "ScaleAI" }}]
[metadata]
category = "debugging"
language = "{lang}"
[environment]
cpus = 2
memory_mb = 8192
storage_mb = 16384
gpus = 0
network_mode = "no-network"
[agent]
timeout_sec = 1800
network_mode = "no-network"
[verifier]
timeout_sec = 1800
network_mode = "no-network"
"""
        (directory / "task.toml").write_text(task)
        for script in directory.rglob("*.sh"):
            script.chmod(0o755)
        manifest["candidates"].append(
            {
                "id": iid.lower(),
                "original_id": iid,
                "language": "typescript" if lang == "ts" else "python",
                "repo": r["repo"],
                "base_commit": r["base_commit"],
                "base_image": image,
            }
        )
(ROOT / "datasets" / "repository-candidates.json").write_text(
    json.dumps(manifest, indent=2) + "\n"
)
print("Prepared", len(manifest["candidates"]), "candidate task definitions")
