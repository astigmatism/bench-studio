"""Generation only. This worker never executes model-produced code."""

import concurrent.futures
import hashlib
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import atomic_json, read_json, now, resolve, snapshot, check_drift, wait_for_runtime

ROOT = Path("/app/datasets/cache")


def choose_tasks(spec):
    result = []
    langs = spec["languages"]
    count = {"quick": 4, "standard": 40}.get(spec["size"])
    each = count // len(langs) if count else None
    for language in langs:
        if language == "python":
            rows = [
                json.loads(line)
                for line in (ROOT / "HumanEvalPlus.jsonl").read_text().splitlines()
                if line
            ]
            tasks = [
                {
                    "id": r["task_id"],
                    "language": "python",
                    "prompt": r["prompt"],
                    "entry_point": r["entry_point"],
                }
                for r in rows
            ]
        else:
            excluded = spec.get("dataset_versions", {}).get("exclusions", {}).get("typescript", {})
            tasks = [
                {
                    "id": r["name"],
                    "language": "typescript",
                    "prompt": r["prompt"],
                    "entry_point": r.get("entry_point"),
                    "tests": r["tests"],
                }
                for r in read_json(ROOT / "typescript.json")
                if r["name"] not in excluded
            ]
        tasks.sort(key=lambda r: hashlib.sha256(("42:" + r["id"]).encode()).hexdigest())
        result.extend(tasks[:each] if each else tasks)
    return result


def generate(endpoint, canonical, task, params, progress):
    payload = {
        "model": canonical,
        "messages": [
            {
                "role": "system",
                "content": "Implement the requested function. Return a complete, self-contained source file in a single code block. Include required imports. Do not include tests or examples.",
            },
            {
                "role": "user",
                "content": f'Language: {task["language"]}\nComplete this specification:\n\n{task["prompt"]}',
            },
        ],
        "temperature": params["temperature"],
        "top_p": params["top_p"],
        "seed": params["seed"],
        "max_tokens": params["max_tokens"],
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if params["reasoning_effort"] != "default":
        payload["reasoning_effort"] = (
            "none"
            if params["reasoning_effort"] == "off"
            else params["reasoning_effort"]
        )
    if params.get("reasoning_budget_tokens") is not None:
        payload["reasoning_budget_tokens"] = params["reasoning_budget_tokens"]
    answer = []
    reasoning = []
    usage = None
    finish = None
    done = False
    start = time.monotonic()
    last = start
    with httpx.Client(timeout=httpx.Timeout(600, connect=15)) as client:
        with client.stream(
            "POST", endpoint + "/chat/completions", json=payload
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                value = line[5:].strip()
                if value == "[DONE]":
                    done = True
                    break
                chunk = json.loads(value)
                if chunk.get("error"):
                    raise RuntimeError(str(chunk["error"]))
                usage = chunk.get("usage") or usage
                for c in chunk.get("choices", []):
                    finish = c.get("finish_reason") or finish
                    delta = c.get("delta", {})
                    if delta.get("content"):
                        answer.append(delta["content"])
                    if delta.get("reasoning_content"):
                        reasoning.append(delta["reasoning_content"])
                if time.monotonic() - last > 5:
                    progress(
                        f'Generating {task["id"]} · {len("".join(answer))+len("".join(reasoning))} characters received'
                    )
                    last = time.monotonic()
    if (
        not done
        or finish not in ["stop", "length"]
        or not usage
        or any(
            not isinstance(usage.get(k), int) or usage[k] <= 0
            for k in ["prompt_tokens", "completion_tokens"]
        )
    ):
        raise RuntimeError("Incomplete stream or missing real token usage")
    return {
        "id": task["id"],
        "language": task["language"],
        "response": "".join(answer),
        "reasoning": "".join(reasoning),
        "usage": usage,
        "finish_reason": finish,
        "duration": time.monotonic() - start,
    }


def main(path):
    manifest = Path(path)
    m = read_json(path)
    base = manifest.parent
    mutex = threading.RLock()
    tasks = choose_tasks(m["profile_spec"])

    def save():
        with mutex:
            m["updated_at"] = now()
            atomic_json(manifest, m)

    def log(s):
        with mutex:
            print(s, flush=True)
            with (base / "run.log").open("a") as f:
                f.write(now() + " " + s + "\n")

    def target(t):
        out = base / t
        out.mkdir(exist_ok=True)
        atomic_json(out / "tasks.json", tasks)
        responses = []
        for i, task in enumerate(tasks):
            wait_for_runtime(m["settings"], t, m["resolved"][t])

            def progress(s):
                with mutex:
                    m["targets"][t] = {
                        "status": "running",
                        "phase": "generation",
                        "progress": {
                            "request": i + 1,
                            "total": len(tasks),
                            "category": task["language"],
                            "task": task["id"],
                        },
                    }
                    m["progress"] = f"{t}: {i+1}/{len(tasks)} · {s}"
                    save()

            progress("Starting request")
            log(f'[{t}] task {i+1}/{len(tasks)} {task["id"]}')
            r = generate(
                m["settings"]["endpoint"],
                m["resolved"][t]["canonical"],
                task,
                m["profile_spec"]["parameters"],
                progress,
            )
            responses.append(r)
            atomic_json(out / "responses.json", responses)
            log(
                f'[{t}] {task["id"]} generated: {r["usage"]["completion_tokens"]} tokens; {r["finish_reason"]}'
            )
        wait_for_runtime(m["settings"], t, m["resolved"][t])

    try:
        m["status"] = "running"
        save()
        if m["mode"] == "parallel":
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=len(m["requested_targets"])
            ) as pool:
                list(pool.map(target, m["requested_targets"]))
        else:
            for t in m["requested_targets"]:
                target(t)
        m.update(
            status="grading",
            progress="Generation complete; waiting for isolated verifiers",
        )
        save()
    except BaseException as e:
        m.update(status="failed", error=str(e))
        save()
        log("FAILED: " + str(e))
        raise


if __name__ == "__main__":
    main(sys.argv[1])
