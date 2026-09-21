import asyncio
import json
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from common import atomic_json
from studio import config, db
from studio.api import app
from studio.session_diagnostics import evidence_path, failure_details


@pytest.fixture
def failed_eligibility(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA", tmp_path)
    db.initialize()
    result = (
        tmp_path
        / "session-validation/prepare-latest/coding-sessions/issues-medium/reference/result"
    )
    output = "browserType.launch: Target page, context or browser has been closed\nReceived signal 11\nprocess did exit: signal=SIGSEGV"
    atomic_json(
        result / "verification.json",
        {
            "passed": False,
            "checks": [
                {
                    "command": ["node", "/verify/browser.mjs", "issues-medium"],
                    "passed": False,
                    "output": output,
                }
            ],
        },
    )
    log = tmp_path / "session-validation/controller/request/preparation.log"
    log.parent.mkdir(parents=True)
    host_result = "/host/project/data/session-validation/prepare-latest/coding-sessions/issues-medium/reference/result"
    log.write_text(
        "RuntimeError: unexpected verifier result; see " + host_result + "\n"
    )
    state = {
        "phase": "paused",
        "last_error": "offline fixture checks failed",
        "log": "/host/project/data/session-validation/controller/request/preparation.log",
        "suites": {},
    }
    atomic_json(tmp_path / "session-setup.json", state)
    return state


def test_legacy_browser_failure_is_explained_from_shared_host_evidence(
    failed_eligibility,
):
    failure = failure_details(failed_eligibility)
    assert failure["kind"] == "browser_startup" and failure["model_used"] is False
    assert failure["task"] == "issues-medium" and failure["variant"] == "reference"
    assert (
        "SIGSEGV" in failure["summary"]
        and "Your model was not used" in failure["summary"]
    )
    with TestClient(app) as client:
        state = client.get("/api/health").json()["session_setup"]
        assert state["phase"] == "failed" and state["can_start"]
        assert state["failure"] == failure
        profiles = client.get("/api/profiles").json()
        coding = next(p for p in profiles if p["id"] == "coding-sessions")
        assert coding["preparation"]["reason"] == failure["summary"]
        log = client.get("/api/session-setup/logs")
        assert log.status_code == 200 and "unexpected verifier result" in log.text
        assert log.headers["content-type"].startswith("text/plain")


def test_eligibility_log_rejects_traversal_and_symlinks(failed_eligibility, tmp_path):
    outside = tmp_path / "private.txt"
    outside.write_text("private")
    (tmp_path / "session-validation/link").symlink_to(outside)
    for value in [
        str(outside),
        "../private.txt",
        "link",
        "/host/data/session-validation/../private.txt",
    ]:
        assert evidence_path(value) is None
        atomic_json(
            tmp_path / "session-setup.json", {**failed_eligibility, "log": value}
        )
        with TestClient(app) as client:
            assert client.get("/api/session-setup/logs").status_code == 404


def test_assertion_failure_is_not_called_a_browser_crash(failed_eligibility, tmp_path):
    path = next((tmp_path / "session-validation").rglob("verification.json"))
    result = json.loads(path.read_text())
    result["checks"][0]["output"] = "AssertionError: expected settings to persist"
    atomic_json(path, result)
    assert failure_details(failed_eligibility)["kind"] == "fixture_validation"


def test_browser_infrastructure_failure_cannot_pass_negative_control(
    tmp_path, monkeypatch
):
    from studio.session_environment import verify_candidate

    async def communicate():
        return b"browser crashed", None

    async def spawn(*args, **kwargs):
        return SimpleNamespace(returncode=1, communicate=communicate)

    monkeypatch.setattr(
        "studio.session_environment.asyncio.create_subprocess_exec", spawn
    )
    out = tmp_path / "result"
    atomic_json(
        out / "verification.json",
        {"passed": False, "infrastructure_error": "Chromium failed to start"},
    )
    with pytest.raises(RuntimeError, match="Chromium failed to start"):
        asyncio.run(
            verify_candidate(
                "image",
                tmp_path / "source.zip",
                out,
                {"id": "issues-small", "app": "issue-tracker"},
                "coding-sessions",
                "fixture-validation",
            )
        )
