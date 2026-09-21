"""Readable eligibility failures from retained, local verifier evidence."""

import json
import re
from pathlib import Path
from . import config


def evidence_path(value):
    """Never serve paths outside validation evidence, including symlink escapes."""
    if not value:
        return None
    try:
        return _evidence_path(value)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _evidence_path(value):
    root = (config.DATA / "session-validation").resolve()
    path = Path(value)
    if path.is_absolute() and not path.is_relative_to(root):
        # The runner stores host paths; the web service mounts the same data
        # at /data. Map only the validation subtree, never arbitrary host files.
        marker = "/session-validation/"
        if marker not in str(path):
            return None
        path = root / str(path).split(marker, 1)[1]
    elif not path.is_absolute():
        path = root / path
    resolved = path.resolve()
    if (
        not resolved.is_relative_to(root)
        or not resolved.is_file()
        or any(p.is_symlink() for p in [path, *path.parents] if p.is_relative_to(root))
    ):
        return None
    return resolved


def log_tail(state):
    path = evidence_path(state.get("log"))
    if not path:
        return None
    try:
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 128000))
            return stream.read(128000).decode(errors="replace")
    except OSError:
        return None


def failure_details(state):
    if not state.get("last_error"):
        return None
    log = log_tail(state) or ""
    failure = {
        "kind": "environment",
        "title": "Eligibility checks could not finish",
        "summary": "The benchmark environment could not complete its offline checks. Your model was not used.",
        "next_step": "Open the failure details and eligibility log to identify the failed check before retrying.",
        "model_used": False,
        "details": log[-12000:] or state["last_error"],
    }
    # Also understands evidence written before structured browser diagnostics
    # existed, so an upgrade can explain the already-failed production checks.
    matches = re.findall(r"see (/[^\r\n]+/result)(?=\r?\n|$)", log)
    if not matches:
        return failure
    path = evidence_path(matches[-1] + "/verification.json")
    if not path:
        return failure
    try:
        if path.stat().st_size > 1000000:
            return failure
        result = json.loads(path.read_text())
        failed = [c for c in result.get("checks", []) if c.get("passed") is False]
        check = failed[-1] if failed else {}
        output = check.get("output", "")
        suite, task, variant = path.parts[-5:-2]
        failure.update(
            suite=suite,
            task=task,
            variant=variant,
            command=" ".join(check.get("command", [])),
            details=output[-16000:] or result.get("error") or log[-12000:],
        )
        if "browserType.launch:" in output or result.get("infrastructure_error"):
            failure.update(
                kind="browser_startup",
                title="Benchmark browser failed to start",
                summary="Chromium could not start the browser checks. This is a benchmark environment failure; your model was not used.",
                next_step="Retry eligibility. Browser startup crashes are retried automatically. If this persists, retain the log for diagnosing the server's browser runtime.",
            )
            if "SIGSEGV" in output or "Received signal 11" in output:
                failure["summary"] = (
                    "Chromium crashed during startup (SIGSEGV), before browser checks could run. Your model was not used."
                )
        else:
            failure.update(
                kind="fixture_validation",
                title="Benchmark reference validation failed",
                summary="A bundled reference or negative-control application did not produce the expected test result. Your model was not used.",
                next_step="Inspect the failed check below. The benchmark fixtures or verifier need attention before this suite can run.",
            )
    except (ValueError, TypeError, KeyError, OSError, AttributeError):
        pass
    return failure
