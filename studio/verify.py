"""Offline verifier entrypoint. Container has only one target directory mounted."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import read_json, atomic_json
from studio.diagnostics import output_diagnostics
from studio.typescript import evaluate as evaluate_typescript


def extract(response):
    blocks = re.findall(
        r"```(?:python|typescript|ts|py|javascript|js)?\s*\n(.*?)```", response, re.S
    )
    return max(blocks, key=len).strip() if blocks else response.strip()


def main(root):
    root = Path(root)
    tasks = read_json(root / "tasks.json")
    responses = {r["id"]: r for r in read_json(root / "responses.json")}
    rows = []
    pys = [t for t in tasks if t["language"] == "python"]
    pygrades = {}
    if pys:
        from evalplus.sanitize import sanitize

        samples = []
        for task in pys:
            response = responses[task["id"]]
            source = extract(response["response"])
            if not re.search(
                r"\bdef\s+" + re.escape(task["entry_point"]) + r"\s*\(", source
            ):
                source = task["prompt"] + source
            source = sanitize(source, entrypoint=task["entry_point"])
            samples.append({"task_id": task["id"], "solution": source})
        samplepath = root / "samples.jsonl"
        samplepath.write_text("".join(json.dumps(r) + "\n" for r in samples))
        # EvalPlus requires one sample for every problem in its input dataset.
        # Supply the exact pinned subset, preserving all original tests.
        ids = {t["id"] for t in pys}
        selected = [
            line
            for line in Path("/app/datasets/cache/HumanEvalPlus.jsonl")
            .read_text()
            .splitlines()
            if json.loads(line)["task_id"] in ids
        ]
        subset = Path("/tmp/HumanEvalPlus-subset.jsonl")
        subset.write_text("\n".join(selected) + "\n")
        env = dict(
            os.environ,
            HUMANEVAL_OVERRIDE_PATH=str(subset),
            EVALPLUS_CACHE_DIR="/tmp/evalplus",
        )
        p = subprocess.run(
            [
                sys.executable,
                "-m",
                "evalplus.evaluate",
                "--dataset",
                "humaneval",
                "--samples",
                str(samplepath),
                "--parallel",
                "1",
                "--min-time-limit",
                "1",
                "--gt-time-limit-factor",
                "2",
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=1800,
        )
        (root / "python-verifier.log").write_text(p.stdout)
        if p.returncode:
            raise RuntimeError(
                "EvalPlus verifier infrastructure failed; see python-verifier.log"
            )
        result = read_json(root / "samples_eval_results.json")
        for tid, values in result["eval"].items():
            value = values[0]
            pygrades[tid] = (
                value.get("base_status") == "pass"
                and value.get("plus_status") == "pass"
            )
    for task in tasks:
        r = responses[task["id"]]
        diagnostics = output_diagnostics(r)
        detail = ""
        failure_kind = None
        if diagnostics["output_exhausted"] or not diagnostics["answer_chars"]:
            passed = False
            failure_kind = diagnostics["failure_kind"]
            detail = ("Output budget exhausted" if diagnostics["output_exhausted"] else "No final answer")
            if not diagnostics["answer_chars"]:
                detail += "; no final code returned"
            if diagnostics["repetition_detected"]:
                detail += "; repetitive reasoning detected"
        elif task["language"] == "python":
            passed = pygrades.get(task["id"], False)
            failure_kind = None if passed else "test_failure"
        else:
            path = Path("/tmp") / (re.sub("[^a-zA-Z0-9_]", "_", task["id"]) + ".ts")
            source = extract(r["response"])
            if not re.search(r"\bfunction\b|=>", source):
                source = task["prompt"] + source
            v = evaluate_typescript(source, task["tests"])
            passed = v["passed"]
            detail = v["detail"]
            failure_kind = v["failure_kind"]
            (root / (path.stem + ".test.log")).write_text(
                v["log"][-30000:]
            )
        rows.append(
            {
                "id": task["id"],
                "language": task["language"],
                "status": "passed" if passed else "failed",
                "duration": r["duration"],
                "detail": detail
                or ("All tests passed" if passed else "Executable tests failed"),
                "usage": r["usage"],
                "finish_reason": r["finish_reason"],
                "diagnostics": diagnostics,
                "failure_kind": failure_kind,
            }
        )
    passed = sum(r["status"] == "passed" for r in rows)
    languages = sorted({r["language"] for r in rows})
    metrics = []
    for language in languages:
        sub = [r for r in rows if r["language"] == language]
        n = sum(r["status"] == "passed" for r in sub)
        metrics.append(
            {
                "language": language,
                "passed": n,
                "count": len(sub),
                "rate": 100 * n / len(sub),
            }
        )
    atomic_json(
        root / "result.json",
        {
            "score": 100 * passed / len(rows),
            "unit": "%",
            "metric": "pass_at_1",
            "passed": passed,
            "count": len(rows),
            "tasks": rows,
            "metrics": metrics,
            "failure_counts": {kind: sum(r.get("failure_kind") == kind for r in rows)
                               for kind in sorted({r["failure_kind"] for r in rows if r.get("failure_kind")})},
            "repetition_count": sum(r["diagnostics"]["repetition_detected"] for r in rows),
        },
    )


if __name__ == "__main__":
    try:
        main(sys.argv[1])
    except BaseException as e:
        atomic_json(Path(sys.argv[1]) / "result.json", {"infrastructure_error": str(e)})
        raise
