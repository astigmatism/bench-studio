#!/usr/bin/env python3
"""Fetch fixed public datasets; verify the committed manifest before caching."""

import gzip, hashlib, json, urllib.request, io
from pathlib import Path
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "datasets" / "cache"
SOURCES = {
    "humanevalplus": "https://github.com/evalplus/humanevalplus_release/releases/download/v0.1.10/HumanEvalPlus.jsonl.gz",
    "typescript": "https://huggingface.co/datasets/nuprl/MultiPL-E/resolve/28441b6024e71d4a1c1c0f6bf171c935cd5a43f2/humaneval-ts/test-00000-of-00001.parquet",
}
manifest_path = ROOT / "datasets" / "coding-manifest.json"
if not manifest_path.is_file():
    raise RuntimeError("Missing committed coding dataset manifest")
expected = json.loads(manifest_path.read_text())
recorded_sources = {
    key for key, record in expected.items() if isinstance(record, dict) and "url" in record
}
if recorded_sources != set(SOURCES):
    raise RuntimeError("Coding dataset manifest sources changed")
OUT.mkdir(parents=True, exist_ok=True)
for key, url in SOURCES.items():
    raw = urllib.request.urlopen(url, timeout=60).read()
    sha = hashlib.sha256(raw).hexdigest()
    if expected[key]["sha256"] != sha:
        raise RuntimeError("Dataset digest mismatch: " + key)
    if key == "humanevalplus":
        text = gzip.decompress(raw).decode()
        rows = [json.loads(s) for s in text.splitlines() if s]
    else:
        rows = pq.read_table(io.BytesIO(raw)).to_pylist()
    ids = [r.get("task_id") or r.get("name") for r in rows]
    actual = {"url": url, "sha256": sha, "count": len(rows), "task_ids": ids}
    if actual != expected[key]:
        raise RuntimeError("Dataset manifest mismatch: " + key)
    if key == "humanevalplus":
        (OUT / "HumanEvalPlus.jsonl").write_text(text)
    else:
        (OUT / "typescript.json").write_text(json.dumps(rows))
    print(key, len(rows), "tasks", len(raw), "download bytes", sha)
