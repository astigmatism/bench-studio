"""Trusted checks in a fresh container, never in the agent's mutable environment."""

import json
import hashlib
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, "/opt/tools")
from bootstrap import bootstrap

root = Path("/workspace")
out = Path("/result")
out.mkdir(exist_ok=True)
task, app, suite = sys.argv[1:4]
result = {"passed": False, "checks": []}
server = None


def protected(path):
    return (
        str(path)
        in {
            "app.json",
            ".gitignore",
            "frontend/package.json",
            "frontend/package-lock.json",
            "frontend/tsconfig.json",
            "frontend/index.html",
            "frontend/src/main.tsx",
        }
        or path.parts[0] == "tests"
    )


def command(args, **kw):
    proc = subprocess.run(
        args, cwd=root, text=True, capture_output=True, timeout=180, **kw
    )
    result["checks"].append(
        {
            "command": args,
            "passed": proc.returncode == 0,
            "output": (proc.stdout + proc.stderr)[-20000:],
        }
    )
    if proc.returncode:
        startup = out / "browser-startup.json"
        if args[:2] == ["node", "/verify/browser.mjs"] and startup.exists():
            evidence = json.loads(startup.read_text())
            if evidence.get("status") == "failed":
                result["infrastructure_error"] = (
                    "Chromium failed to start after "
                    f"{evidence['attempts']} launch attempts; no browser checks ran"
                )
        raise RuntimeError("Check failed: " + " ".join(args))
    return proc.stdout


def wait_ready():
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise RuntimeError(
                "Application exited during startup: "
                + Path("/tmp/server.log").read_text()[-4000:]
            )
        try:
            urllib.request.urlopen(
                "http://127.0.0.1:8111/api/issues", timeout=1
            ).close()
            return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("Application did not become ready within 90 seconds")


try:
    revision = bootstrap(root, app)
    # Seed a pre-feature database to exercise migrations.
    env = dict(os.environ, APP_DATABASE="/tmp/acceptance.sqlite3", PYTHONPATH=str(root))
    command(["python", "-c", "import backend.app"], env=env)
    with zipfile.ZipFile("/candidate/source.zip") as z:
        original_sources = {}
        total = 0
        names = set()
        for row in z.infolist():
            path = Path(row.filename)
            total += row.file_size
            if (
                path.is_absolute()
                or not path.parts
                or ".." in path.parts
                or str(path) in names
                or len(names) >= 500
                or row.file_size > 8 * 1024 * 1024
                or total > 32 * 1024 * 1024
                or ((row.external_attr >> 16) & 0o170000) == 0o120000
            ):
                raise RuntimeError("Unsafe candidate archive")
            names.add(str(path))
            original_sources[str(path)] = hashlib.sha256(z.read(row)).hexdigest()
            target = root / path
            if target.exists() and target.is_file() and protected(path):
                if z.read(row) != target.read_bytes():
                    raise RuntimeError(
                        "Protected build or regression input changed: " + str(path)
                    )
            if path.parts[0] not in {"backend", "frontend", "tests", "docs"} and str(
                path
            ) not in {"README.md", "prototype.html", "app.json", ".gitignore"}:
                raise RuntimeError("Unexpected candidate path")
            if "node_modules" in path.parts or ".git" in path.parts:
                raise RuntimeError("Unexpected dependency or git path")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(row))
        # The archive is a full source snapshot. Preserve deletions as well as
        # additions, while requiring all trusted build and regression inputs.
        tracked = command(["git", "ls-files", "-z"]).split("\0")
        for name in filter(None, tracked):
            if name not in names:
                if protected(Path(name)):
                    raise RuntimeError("Protected input removed: " + name)
                (root / name).unlink(missing_ok=True)
    if suite == "visual-design":
        if not (root / "prototype.html").is_file():
            raise RuntimeError("Missing prototype.html")
        command(
            [
                "node",
                "/verify/browser.mjs",
                task,
                str(root / "prototype.html"),
                str(out),
            ],
            env=env,
        )
        shutil.copyfile(root / "prototype.html", out / "prototype.html")
    else:
        command(
            [
                "python",
                "-m",
                "ruff",
                "check",
                "--isolated",
                "--select",
                "E9,F63,F7,F82",
                "backend",
            ],
            env=env,
        )
        command(["npm", "--prefix", "frontend", "run", "build"], env=env)
        command(
            [
                "python",
                "-m",
                "pytest",
                "--confcutdir=/opt/base/tests",
                "-c",
                "/dev/null",
                "/opt/base/tests",
                "-q",
            ],
            env=dict(env, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1"),
        )
        server = subprocess.Popen(
            [
                "python",
                "-m",
                "uvicorn",
                "backend.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8111",
            ],
            cwd=root,
            env=env,
            stdout=open("/tmp/server.log", "w"),
            stderr=subprocess.STDOUT,
        )
        wait_ready()
        command(["python", "/verify/check.py", task], env=env)
        # Reboot against the same database before UI checks to detect in-memory persistence.
        server.terminate()
        server.wait(timeout=10)
        server = subprocess.Popen(
            [
                "python",
                "-m",
                "uvicorn",
                "backend.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8111",
            ],
            cwd=root,
            env=env,
            stdout=open("/tmp/server.log", "a"),
            stderr=subprocess.STDOUT,
        )
        wait_ready()
        command(["python", "/verify/persistence.py", task], env=env)
        command(["node", "/verify/browser.mjs", task, "", str(out)], env=env)
    if server and server.poll() is None:
        server.terminate()
        server.wait(timeout=10)
    command(["python", "/opt/tools/export.py", "/tmp/verified-source.zip"])
    with zipfile.ZipFile("/tmp/verified-source.zip") as z:
        verified_sources = {
            row.filename: hashlib.sha256(z.read(row)).hexdigest()
            for row in z.infolist()
        }
    if verified_sources != original_sources:
        raise RuntimeError(
            "Checks changed candidate source files. Regenerate those files before submitting for verification."
        )
    # Generate the patch from our clean base and verified files, including additions.
    command(["git", "add", "-N", "."])
    patch = command(["git", "--no-pager", "diff", "--no-ext-diff", "--binary", "HEAD"])
    (out / "change.patch").write_text(patch)
    (out / "changed-files.txt").write_text(
        command(["git", "diff", "--name-only", "HEAD"])
    )
    result.update(passed=True, base_revision=revision)
except Exception as exc:
    result["error"] = str(exc)
finally:
    if Path("/tmp/server.log").exists():
        shutil.copyfile("/tmp/server.log", out / "server.log")
    if server and server.poll() is None:
        server.terminate()
        server.wait(timeout=10)
    (out / "verification.json").write_text(json.dumps(result, indent=2))
print(json.dumps(result))
sys.exit(0 if result["passed"] else 1)
