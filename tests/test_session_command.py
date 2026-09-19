import json
import base64
import shlex
import subprocess
import sys
import time

from studio.session_command import COMMAND_RUNNER


def execute(command, seconds=2):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            COMMAND_RUNNER,
            base64.b64encode(command.encode()).decode(),
            str(seconds),
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    return json.loads(result.stdout)


def test_command_exit_and_bounded_output():
    command = shlex.join(
        [
            sys.executable,
            "-c",
            "import sys; print('a'*20000); print('error',file=sys.stderr); sys.exit(3)",
        ]
    )
    result = execute(command)
    assert result["exit_code"] == 3
    assert len(result["stdout"]) == 12000 and result["stderr"] == "error\n"
    assert execute("true") == {"exit_code": 0, "stdout": "", "stderr": ""}


def test_timeout_kills_descendants_and_retains_partial_output(tmp_path):
    marker = tmp_path / "orphan"
    child = shlex.join(
        [
            sys.executable,
            "-c",
            f"import signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(1); pathlib.Path({str(marker)!r}).touch()",
        ]
    )
    result = execute(f"{child} & echo partial; wait", seconds=0.2)
    assert result["timed_out"] and result["exit_code"] == 124
    assert result["stdout"] == "partial\n"
    time.sleep(1)
    assert not marker.exists()
    assert execute("echo recovered")["stdout"] == "recovered\n"


def test_background_server_does_not_hold_output_pipe():
    # A short-lived stand-in for a server; no orphan remains after this test.
    start = time.monotonic()
    result = execute("sleep 1 & echo started")
    assert result["exit_code"] == 0 and result["stdout"] == "started\n"
    assert time.monotonic() - start < 1


def test_cleanup_command_does_not_match_supervisor_arguments():
    # The command text must not appear in supervisor/Harbor argv. A model's
    # process-name cleanup should only be able to terminate the child command.
    command = shlex.join(
        [
            sys.executable,
            "-c",
            "import os,subprocess; needle='unique-cleanup-token'; parent=subprocess.check_output(['ps','-o','command=','-p',str(os.getppid())],text=True); assert needle not in parent; print('supervisor-safe')",
        ]
    )
    result = execute(command)
    assert result["exit_code"] == 0 and result["stdout"] == "supervisor-safe\n"
