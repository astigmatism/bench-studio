#!/usr/bin/env python3
"""Fetch fixed public datasets; verify recorded digests before conversion."""

import gzip, hashlib, json, urllib.request, io
from pathlib import Path
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "datasets" / "cache"
OUT.mkdir(parents=True, exist_ok=True)
SOURCES = {
    "humanevalplus": "https://github.com/evalplus/humanevalplus_release/releases/download/v0.1.10/HumanEvalPlus.jsonl.gz",
    "typescript": "https://huggingface.co/datasets/nuprl/MultiPL-E/resolve/28441b6024e71d4a1c1c0f6bf171c935cd5a43f2/humaneval-ts/test-00000-of-00001.parquet",
}
manifest_path = ROOT / "datasets" / "coding-manifest.json"
expected = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
manifest = {}
for key, url in SOURCES.items():
    raw = urllib.request.urlopen(url, timeout=60).read()
    sha = hashlib.sha256(raw).hexdigest()
    if expected and expected[key]["sha256"] != sha:
        raise RuntimeError("Dataset digest mismatch: " + key)
    if key == "humanevalplus":
        text = gzip.decompress(raw).decode()
        rows = [json.loads(s) for s in text.splitlines() if s]
        (OUT / "HumanEvalPlus.jsonl").write_text(text)
    else:
        rows = pq.read_table(io.BytesIO(raw)).to_pylist()
        (OUT / "typescript.json").write_text(json.dumps(rows))
    ids = [r.get("task_id") or r.get("name") for r in rows]
    manifest[key] = {"url": url, "sha256": sha, "count": len(rows), "task_ids": ids}
    print(key, len(rows), "tasks", len(raw), "download bytes", sha)
if not expected:
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
