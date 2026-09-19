"""Harbor execution plus fresh, offline verification of bounded candidate sources."""

import asyncio
import base64
import json
import shlex
import shutil
import tempfile
import struct
import contextlib
import uuid
import os
from pathlib import Path
from . import config
from common import read_json, atomic_json
from .session_command import COMMAND_RUNNER


def image_dimensions(path):
    with Path(path).open("rb") as source:
        header = source.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError("Renderer produced an invalid PNG")
    width, height = struct.unpack(">II", header[16:24])
    return {"width": width, "height": height}


async def verify_candidate(
    image, candidate, output, task, suite, run_id, *, checks_source=None
):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    output.chmod(0o777)
    # DATA has the same absolute path on the controller and Docker host. The
    # application's /app source path does not exist on that host. Snapshot the
    # trusted checks beside the artifacts, outside all candidate/result mounts.
    checks = Path(tempfile.mkdtemp(prefix="trusted-checks-", dir=output.parent))
    shutil.copytree(
        checks_source or config.ROOT / "datasets/sessions/acceptance",
        checks,
        dirs_exist_ok=True,
    )
    name = "bs-verify-" + uuid.uuid4().hex[:16]
    args = [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "--label",
        "io.bench-studio.run=" + run_id,
        *(
            [
                "--label",
                "io.bench-studio.preparation=" + os.environ["STUDIO_PREPARATION_OWNER"],
            ]
            if os.environ.get("STUDIO_PREPARATION_OWNER")
            else []
        ),
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--cpus",
        "2",
        "--memory",
        "4g",
        "--pids-limit",
        "512",
        "--shm-size",
        "256m",
        "--tmpfs",
        "/workspace:rw,uid=1000,gid=1000,size=1g",
        "--tmpfs",
        "/tmp:rw,size=1g",
        "-e",
        "HOME=/tmp",
        "--mount",
        f"type=bind,src={Path(candidate).resolve().parent},dst=/candidate,readonly",
        "--mount",
        f"type=bind,src={checks.resolve()},dst=/verify,readonly",
        "--mount",
        f"type=bind,src={output.resolve()},dst=/result",
        image,
        "python",
        "/verify/verify.py",
        task["id"],
        task["app"],
        suite,
    ]
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        logs, _ = await asyncio.wait_for(process.communicate(), timeout=900)
        (output / "verifier.log").write_bytes(logs)
    except TimeoutError as exc:
        (output / "verifier.log").write_text(
            "Verifier exceeded its 900-second infrastructure limit.\n"
        )
        raise RuntimeError("Verifier exceeded its infrastructure time limit") from exc
    except asyncio.CancelledError:
        (output / "verifier.log").write_text(
            "Verification interrupted by session cancellation or active-time limit.\n"
        )
        raise
    finally:
        if process.returncode is None:
            cleanup = await asyncio.create_subprocess_exec(
                "docker",
                "rm",
                "-f",
                name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await cleanup.wait()
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
    path = output / "verification.json"
    if not path.exists():
        raise RuntimeError(
            "Verifier infrastructure did not produce an outcome; see verifier.log"
        )
    result = read_json(path)
    browser = output / "browser.json"
    if browser.exists():
        metadata = read_json(browser)
        metadata["screenshot_dimensions"] = {
            name: image_dimensions(output / (name + ".png"))
            for name in ("desktop", "mobile")
            if (output / (name + ".png")).exists()
        }
        atomic_json(browser, metadata)
    if (process.returncode == 0) != (result.get("passed") is True):
        raise RuntimeError("Inconsistent verifier outcome")
    return result


class SessionEnvironment:
    def __init__(self, manifest, task, root):
        self.manifest = manifest
        self.task = task
        self.root = Path(root)
        self.env = None

    async def start(self):
        from harbor.models.task.config import (
            EnvironmentConfig,
            NetworkMode,
            NetworkPolicy,
        )
        from harbor.models.trial.paths import TrialPaths
        from .harbor_environment import IsolatedDocker

        trial = self.root / "harbor"
        trial.mkdir(parents=True, exist_ok=True)
        self.env = IsolatedDocker(
            environment_dir=config.ROOT / "datasets/sessions",
            environment_name="studio-session",
            session_id="bs-" + uuid.uuid4().hex[:16],
            trial_paths=TrialPaths(trial),
            task_env_config=EnvironmentConfig(
                docker_image=self.manifest["profile_spec"]["session_image"],
                network_mode=NetworkMode.NO_NETWORK,
                cpus=2,
                memory_mb=4096,
                gpus=0,
                workdir="/workspace",
            ),
            network_policy=NetworkPolicy(network_mode=NetworkMode.NO_NETWORK),
            run_id=self.manifest["id"],
        )
        await self.env.start(force_build=False)
        result = await self.env.exec(
            "python /opt/tools/bootstrap.py /workspace "
            + shlex.quote(self.task["app"]),
            timeout_sec=60,
        )
        if result.return_code:
            raise RuntimeError(
                "Could not initialize pinned fixture: " + (result.stderr or "")
            )
        revision = result.stdout.strip().splitlines()[-1]
        expected = self.manifest["profile_spec"]["base_revisions"][self.task["app"]]
        if revision != expected:
            raise RuntimeError(
                "Fixture base revision does not match preparation receipt"
            )
        return revision

    async def stop(self):
        if self.env:
            await self.env.stop(delete=True)

    async def action(self, action, *, read_only):
        name = action.get("action")
        if not isinstance(name, str) or name not in {
            "list",
            "read",
            "search",
            "write",
            "exec",
        }:
            return "Unknown action."
        if read_only and name not in {"list", "read", "search"}:
            return "Planning is read-only. Submit a plan for approval before changing files or running commands."
        if name == "exec":
            command = action.get("command")
            if not isinstance(command, str) or len(command) > 16000:
                return "Invalid command."
            # Keep command text out of supervisor/Harbor argv so a model's
            # process-name cleanup cannot accidentally match its supervisors.
            result = await self.env.exec(
                "python -c "
                + shlex.quote(COMMAND_RUNNER)
                + " "
                + shlex.quote(base64.b64encode(command.encode()).decode())
                + " 120",
                cwd="/workspace",
                timeout_sec=130,
            )
            if result.return_code:
                if result.return_code < 0 or result.return_code in {129, 130, 137, 143}:
                    return json.dumps(
                        {
                            "exit_code": result.return_code,
                            "stdout": (result.stdout or "")[-12000:],
                            "stderr": (result.stderr or "")[-8000:],
                            "detail": "Command supervisor was terminated by a signal. Stop background servers by their saved PID; avoid killing unrelated processes. Continue with another command.",
                        }
                    )
                raise RuntimeError("Command runner failed: " + (result.stderr or ""))
            # A command deadline is a recoverable tool result. The outer Harbor
            # deadline remains an infrastructure failure, not a model timeout.
            value = json.loads(result.stdout or "")
            if not isinstance(value, dict) or "exit_code" not in value:
                raise RuntimeError("Command runner returned an invalid outcome")
            return json.dumps(value)
        # This code runs with fixed operations and JSON data, never interpolated source.
        script = """import json,sys,os
from pathlib import Path
r=Path('/workspace');a=json.loads(sys.argv[1]);p=(r/a.get('path','.')).resolve()
if not p.is_relative_to(r) or any(x in {'.git','node_modules','dist','__pycache__'} for x in p.relative_to(r).parts): raise ValueError('Path outside source tree')
n=a['action']
if n=='read':
 if p.stat().st_size>1000000: raise ValueError('File too large')
 print(p.read_text()[:24000])
elif n=='write':
 if not isinstance(a.get('content'),str) or len(a['content'])>1000000: raise ValueError('Invalid file content')
 p.parent.mkdir(parents=True,exist_ok=True);p.write_text(a['content']);print('Written')
else:
 rows=[]
 for base,dirs,files in os.walk(p if n=='list' else r,followlinks=False):
  dirs[:]=[d for d in dirs if d not in {'.git','node_modules','dist','__pycache__','.pytest_cache'} and not (Path(base)/d).is_symlink()]
  for f in sorted(files):
   q=Path(base)/f
   if q.is_symlink() or not q.is_file(): continue
   if n=='list': rows.append(str(q.relative_to(r)))
   elif q.stat().st_size<1000000:
    for number,line in enumerate(q.read_text(errors='replace').splitlines(),1):
     if a.get('query','') in line: rows.append(f'{q.relative_to(r)}:{number}: {line[:300]}')
   if len(rows)>=500: break
  if len(rows)>=500: break
 print('\\n'.join(rows)[:24000])
"""
        result = await self.env.exec(
            "python -c " + shlex.quote(script) + " " + shlex.quote(json.dumps(action)),
            timeout_sec=20,
        )
        return (
            (result.stdout or "")
            if not result.return_code
            else "Tool error: " + (result.stderr or "")[-2000:]
        )

    async def export(self, out):
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        result = await self.env.exec(
            "python /opt/tools/export.py /tmp/source.zip", timeout_sec=30
        )
        if result.return_code:
            raise RuntimeError("Source export failed: " + (result.stderr or ""))
        await self.env.download_file("/tmp/source.zip", out / "source.zip")
        return out / "source.zip"

    async def render(self, out):
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        result = await self.env.exec(
            "node /opt/tools/render.mjs /workspace/prototype.html /tmp/render",
            timeout_sec=60,
        )
        if result.return_code:
            raise ValueError(
                "Prototype rendering failed: " + (result.stderr or "")[-3000:]
            )
        for name in ("desktop.png", "mobile.png", "render.json"):
            await self.env.download_file("/tmp/render/" + name, out / name)
        metadata = read_json(out / "render.json")
        metadata.update(network="disabled", headless=True)
        for layout in metadata["layouts"]:
            layout["image_dimensions"] = image_dimensions(
                out / (layout["name"] + ".png")
            )
        atomic_json(out / "render.json", metadata)
        return [out / "desktop.png", out / "mobile.png"]
