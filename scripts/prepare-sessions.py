#!/usr/bin/env python3
"""Build offline fixtures, validate positive/negative controls, optionally queue runtime qualification."""

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import uuid
import zipfile
import contextlib
import signal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import now
from studio import config
from studio.session_catalog import (
    ROOT,
    catalog,
    digest_tree,
    receipt,
    PROTOCOL_VERSION,
    save_preparation,
)
from studio.session_environment import verify_candidate


def command(args, **kwargs):
    subprocess.run(args, check=True, **kwargs)


def ownership():
    owner = os.environ.get("STUDIO_PREPARATION_OWNER")
    return ["--label", "io.bench-studio.preparation=" + owner] if owner else []


def archive(project, out):
    out.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out / "source.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(project.rglob("*")):
            if any(
                x in {".git", "node_modules", "dist", "__pycache__", ".pytest_cache"}
                for x in path.parts
            ):
                continue
            if path.is_file() and path.suffix != ".sqlite3":
                z.write(path, str(path.relative_to(project)))
    return out / "source.zip"


def app_config(app):
    return (
        {
            "kind": "issues",
            "title": "Issue tracker",
            "description": "Plan, assign and track your team’s work.",
        }
        if app == "issue-tracker"
        else {
            "kind": "inventory",
            "title": "Inventory desk",
            "description": "A clear view of supplies and stock.",
        }
    )


async def prepare(args):
    image = args.image
    if not args.no_build:
        command(
            [
                "docker",
                "build",
                "-f",
                str(ROOT / "Dockerfile"),
                "-t",
                image,
                str(config.ROOT),
            ]
        )
    image_id = subprocess.check_output(
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"], text=True
    ).strip()
    image_sources = json.loads(
        subprocess.check_output(
            [
                "docker",
                "run",
                "--rm",
                *ownership(),
                "--network",
                "none",
                image_id,
                "python",
                "-c",
                """
import hashlib,json,os
from pathlib import Path
result={}
for name in ('base','tools'):
 root=Path('/opt')/name; rows={}
 for base,dirs,files in os.walk(root):
  dirs[:]=[d for d in dirs if d not in {'node_modules','.git','__pycache__','.pytest_cache','dist'}]
  for file in files:
   path=Path(base)/file
   rows[str(path.relative_to(root))]=hashlib.sha256(path.read_bytes()).hexdigest()
 result[name]=hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest()
print(json.dumps(result))
""",
            ],
            text=True,
            timeout=90,
        )
    )
    if image_sources != {name: digest_tree(ROOT / name) for name in ("base", "tools")}:
        raise RuntimeError(
            "Prepared image sources are stale; rebuild without --no-build"
        )
    validation = (
        config.DATA / "session-validation" / ("prepare-" + uuid.uuid4().hex[:10])
    )
    validation.mkdir(parents=True)
    spec = importlib.util.spec_from_file_location(
        "reference", ROOT / "solutions/reference.py"
    )
    reference = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reference)
    receipts = {
        "source_hash": digest_tree(ROOT),
        "image_id": image_id,
        "base_revisions": {},
        "suites": {},
        "validated_at": now(),
        "evidence": str(validation),
        "protocol_version": PROTOCOL_VERSION,
        "image_sources": image_sources,
    }
    previous = receipt()
    if all(
        previous.get(key) == receipts[key]
        for key in ("source_hash", "image_id", "protocol_version")
    ):
        receipts["suites"] = previous.get("suites", {})
    for app in ("issue-tracker", "inventory"):
        revision = (
            subprocess.check_output(
                [
                    "docker",
                    "run",
                    "--rm",
                    *ownership(),
                    "--network",
                    "none",
                    image_id,
                    "python",
                    "/opt/tools/bootstrap.py",
                    "/workspace",
                    app,
                ],
                text=True,
            )
            .strip()
            .splitlines()[-1]
        )
        receipts["base_revisions"][app] = revision
    selected = (
        ("coding-sessions", "visual-design")
        if args.suite == "all"
        else (() if args.suite == "vision-checks" else (args.suite,))
    )
    for suite in selected:
        checks = []
        semaphore = asyncio.Semaphore(args.jobs)

        async def validate_task(task):
            async with semaphore:
                variants = ("base", "reference", "incomplete")
                if task["id"] == "issues-small":
                    variants += (
                        "reference-short-status-label",
                        "reference-status-radio",
                        "reference-status-button",
                        "reference-status-tab",
                        "reference-main-role",
                    )
                for variant in variants:
                    dest = validation / suite / task["id"] / variant
                    project = dest / "project"
                    shutil.copytree(ROOT / "base", project)
                    (project / "app.json").write_text(
                        json.dumps(app_config(task["app"]), sort_keys=True) + "\n"
                    )
                    if variant != "base":
                        reference.apply(project, task["id"])
                    if variant == "reference-short-status-label":
                        path = project / "frontend/src/App.tsx"
                        path.write_text(
                            path.read_text().replace("Status filter", "Status")
                        )
                    if variant == "reference-main-role":
                        path = project / "frontend/src/App.tsx"
                        source = path.read_text()
                        assert "<main>" in source
                        path.write_text(
                            source.replace("<main>", '<div role="main">').replace(
                                "</main>", "</div>"
                            )
                        )
                    if variant in {
                        "reference-status-radio",
                        "reference-status-button",
                        "reference-status-tab",
                    }:
                        path = project / "frontend/src/App.tsx"
                        source = path.read_text()
                        original = "<label>Status filter<select value={status} onChange={e=>setStatus(e.target.value)}>{['All','Open','Closed'].map(s=><option key={s}>{s}</option>)}</select></label>"
                        assert original in source
                        kind = variant.rsplit("-", 1)[1]
                        if kind == "radio":
                            replacement = "<fieldset><legend>Status</legend>{['All','Open','Closed'].map(s=><label key={s}><input type=\"radio\" name=\"status\" value={s} checked={status===s} onChange={()=>setStatus(s)}/>{s}</label>)}</fieldset>"
                        else:
                            role = "tablist" if kind == "tab" else "group"
                            attrs = (
                                'role="tab" aria-selected={status===s}'
                                if kind == "tab"
                                else "aria-pressed={status===s}"
                            )
                            replacement = f"<div role=\"{role}\" aria-label=\"Status filter\">{{['All','Open','Closed'].map(s=><button key={{s}} type=\"button\" {attrs} onClick={{()=>setStatus(s)}}>{{s}}</button>)}}</div>"
                        path.write_text(source.replace(original, replacement))
                    if suite == "visual-design":
                        if variant != "base":
                            reference.visual_screens(project, task["id"])
                            path = project / "frontend/src/App.tsx"
                            text = path.read_text()
                            start = text.index("export async function api")
                            end = text.index("export default function App")
                            path.write_text(
                                text[:start]
                                + (ROOT / "solutions/mock-api.ts").read_text()
                                + "\n"
                                + text[end:]
                            )
                            command(
                                [
                                    "docker",
                                    "run",
                                    "--rm",
                                    *ownership(),
                                    "--network",
                                    "none",
                                    "--user",
                                    str(os.getuid()) + ":" + str(os.getgid()),
                                    "--mount",
                                    f"type=bind,src={project.resolve()},dst=/workspace",
                                    image_id,
                                    "sh",
                                    "-c",
                                    "ln -s /opt/frontend/node_modules frontend/node_modules && npm --prefix frontend run build && node /opt/tools/inline.mjs frontend/dist prototype.html",
                                ]
                            )
                            (project / "frontend/node_modules").unlink()
                        else:
                            (project / "prototype.html").write_text(
                                "<!doctype html><h1>Empty prototype</h1>"
                            )
                    if variant == "incomplete":
                        if suite == "visual-design":
                            prototype = project / "prototype.html"
                            prototype.write_text(
                                prototype.read_text()
                                + "<script>for(const event of ['click','input','change'])document.addEventListener(event,e=>{e.preventDefault();e.stopImmediatePropagation()},true)</script>"
                            )
                        elif task["id"] == "issues-small":
                            source = project / "frontend/src/App.tsx"
                            source.write_text(
                                source.read_text().replace(
                                    "&&i.title.toLowerCase().includes(search.toLowerCase())",
                                    "",
                                )
                            )
                        elif task["id"] == "inventory-small":
                            source = project / "frontend/src/App.tsx"
                            source.write_text(
                                source.read_text().replace(
                                    "a.quantity-b.quantity:b.quantity-a.quantity",
                                    "a.quantity-b.quantity:a.quantity-b.quantity",
                                )
                            )
                        else:
                            # Keep the backend reference but remove the requested interface.
                            shutil.copyfile(
                                ROOT / "base/frontend/src/App.tsx",
                                project / "frontend/src/App.tsx",
                            )
                    source = archive(project, dest / "candidate")
                    print(suite, task["id"], variant, flush=True)
                    result = await verify_candidate(
                        image_id,
                        source,
                        dest / "result",
                        task,
                        suite,
                        "fixture-validation",
                    )
                    if bool(result["passed"]) != variant.startswith("reference"):
                        raise RuntimeError(
                            f"{suite}/{task['id']}/{variant}: unexpected verifier result; see {dest}/result"
                        )
                    if not variant.startswith("reference"):
                        failed = [
                            row for row in result.get("checks", []) if not row["passed"]
                        ]
                        if not failed or not any(
                            path in failed[-1]["command"]
                            for path in ("/verify/check.py", "/verify/browser.mjs")
                        ):
                            raise RuntimeError(
                                f"{task['id']}/{variant} failed before task acceptance; this is not a valid negative control: {dest}/result"
                            )
                    checks.append(
                        {"task": task["id"], "variant": variant, "passed": True}
                    )

        async with asyncio.TaskGroup() as group:
            for task in catalog()["tasks"]:
                group.create_task(validate_task(task))
        receipts["suites"][suite] = {
            **receipts["suites"].get(suite, {}),
            "passed": True,
            "controls": checks,
            "evidence": str(validation / suite),
        }
    images = []
    for task in catalog()["vision_checks"]:
        path = ROOT / task["image"]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != task["image_sha256"]:
            raise RuntimeError("Vision fixture hash mismatch: " + task["id"])
        if not task.get("answer") or path.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
            raise RuntimeError("Invalid vision fixture")
        images.append({"id": task["id"], "sha256": digest})
    if args.suite in ("all", "vision-checks"):
        receipts["suites"]["vision-checks"] = {
            **receipts["suites"].get("vision-checks", {}),
            "passed": True,
            "images": images,
        }
    if digest_tree(ROOT) != receipts["source_hash"]:
        raise RuntimeError(
            "Fixture sources changed during preparation; rerun validation"
        )
    save_preparation(receipts)
    print(
        "Offline reference validation complete. Runtime qualification is still required."
    )


def qualify(args):
    import urllib.request

    if args.suite == "all":
        raise ValueError("Select a specific --suite for its runtime smoke run")

    body = {
        "profile": args.suite,
        "targets": [args.smoke_target],
        "task_selection": "text-1" if args.suite == "vision-checks" else "issues-small",
        "repetitions": 1,
        "review_mode": "unattended",
        "qualification": True,
        "idempotency_key": str(uuid.uuid4()),
    }
    if args.suite != "vision-checks":
        body["difficulty"] = "small"
    req = urllib.request.Request(
        args.url.rstrip("/") + "/api/runs",
        json.dumps(body).encode(),
        {"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as res:
        print("Qualification queued:", json.load(res)["id"])


async def supervised_prepare(args):
    from studio.session_job import watch_controller

    task = asyncio.create_task(prepare(args))
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    watcher = asyncio.create_task(
        watch_controller(
            task, int(os.environ.get("STUDIO_CONTROLLER_PID", os.getppid()))
        )
    )
    try:
        await task
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="local/bench-studio-session:current")
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument(
        "--jobs",
        type=int,
        choices=[1, 2],
        default=2,
        help="Concurrent offline fixture validations; model runs remain sequential",
    )
    parser.add_argument("--smoke-target")
    parser.add_argument(
        "--suite",
        choices=["all", "coding-sessions", "vision-checks", "visual-design"],
        default="all",
    )
    parser.add_argument(
        "--url", default=os.environ.get("BENCH_STUDIO_URL", "http://127.0.0.1:9001")
    )
    args = parser.parse_args()
    if args.smoke_target:
        qualify(args)
    else:
        asyncio.run(supervised_prepare(args))
