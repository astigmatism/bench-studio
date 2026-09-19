"""Bound model commands inside the container, including their child processes."""

# Sent as trusted Python source to the environment. Temporary files prevent a
# detached server from keeping Harbor's output pipes open after its shell exits.
COMMAND_RUNNER = r"""
import base64, json, os, signal, subprocess, sys, tempfile
command, seconds = base64.b64decode(sys.argv[1]).decode(), float(sys.argv[2])
with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
    process = subprocess.Popen(
        ['bash', '-lc', command], stdout=out, stderr=err, start_new_session=True
    )
    timed_out = False
    try:
        process.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        # Kill remaining descendants even if the shell already exited on TERM.
        import time
        time.sleep(0.2)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
    def tail(source, limit):
        source.seek(0, 2)
        source.seek(max(0, source.tell() - limit))
        return source.read().decode(errors='replace')
    result = dict(exit_code=124 if timed_out else process.returncode,
                  stdout=tail(out, 12000), stderr=tail(err, 8000))
    if timed_out:
        result.update(timed_out=True, detail=f'Command exceeded {seconds:g} seconds and was stopped. Continue with another command. Dependencies and Chromium are already installed; network access is disabled.')
    print(json.dumps(result))
"""
