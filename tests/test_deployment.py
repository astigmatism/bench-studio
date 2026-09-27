"""Deployment invariants shared by the installer and Service Portal updater."""

import gzip
import hashlib
import importlib.util
import json
import runpy
import shutil
import sqlite3
import subprocess
import sys
import types
import urllib.request
from pathlib import Path

import pytest


def updater_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "update.py"
    spec = importlib.util.spec_from_file_location("deployment_updater", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def compose_config(root):
    project = str(root)
    data = str(root / "data")
    return {
        "services": {
            "reports": {
                "user": "1001:1002",
                "environment": {"DATA_ROOT": "/data"},
                "volumes": [{"type": "bind", "source": data, "target": "/data"}],
            },
            "runner": {
                "user": "1001:1002",
                "environment": {
                    "PROJECT_DIR": project,
                    "DATA_ROOT": data,
                    "HOST_UID": "1001",
                    "HOST_GID": "1002",
                },
                "volumes": [
                    {"type": "bind", "source": data, "target": data},
                    {
                        "type": "bind",
                        "source": "/var/run/docker.sock",
                        "target": "/var/run/docker.sock",
                    },
                ],
            },
        }
    }


@pytest.mark.parametrize("fault", ["relative_project", "other_source", "wrong_user"])
def test_updater_rejects_invalid_data_contract(tmp_path, monkeypatch, fault):
    mod = updater_module()
    mod.ROOT = tmp_path
    config = compose_config(tmp_path)
    if fault == "relative_project":
        config["services"]["runner"]["environment"]["PROJECT_DIR"] = "."
    elif fault == "other_source":
        config["services"]["reports"]["volumes"][0]["source"] = str(
            tmp_path / "other-data"
        )
    else:
        config["services"]["runner"]["user"] = "1000:1000"
    monkeypatch.setattr(
        mod,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 0, json.dumps(config), ""
        ),
    )
    with pytest.raises(RuntimeError):
        mod.validate_compose_config()


def test_portal_update_keeps_database_baseline_and_artifact(tmp_path, monkeypatch):
    mod = updater_module()
    mod.ROOT = tmp_path
    data = tmp_path / "data"
    report = data / "runs" / "saved" / "report.json"
    report.parent.mkdir(parents=True)
    report.write_text('{"score": 42}')
    database = data / "studio.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE runs (id TEXT, status TEXT)")
        connection.execute("INSERT INTO runs VALUES ('saved', 'completed')")
        connection.execute("CREATE TABLE baselines (run_id TEXT)")
        connection.execute("INSERT INTO baselines VALUES ('saved')")

    config = compose_config(tmp_path)
    monkeypatch.setattr(mod, "preflight", lambda: None)
    calls = []
    revisions = iter(["old-revision", "new-revision"])

    def fake_run(*args, **kwargs):
        calls.append(args)
        if args[:4] == ("docker", "compose", "config", "--format"):
            output = json.dumps(config)
        elif args == ("git", "rev-parse", "HEAD"):
            output = next(revisions)
        elif args[:3] == ("docker", "image", "inspect"):
            output = "sha256:previous-image"
        else:
            output = ""
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(mod, "run", fake_run)
    mod.deploy()

    assert report.read_text() == '{"score": 42}'
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT id FROM runs").fetchall() == [("saved",)]
        assert connection.execute("SELECT run_id FROM baselines").fetchall() == [
            ("saved",)
        ]
    backups = list((data / "backups").glob("*/studio.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("SELECT id FROM runs").fetchall() == [("saved",)]
        assert connection.execute("SELECT run_id FROM baselines").fetchall() == [
            ("saved",)
        ]
    assert not (data / ".maintenance").exists()
    assert any(args[:3] == ("docker", "compose", "up") for args in calls)
    assert not any("down" in args or "prune" in args for args in calls)


def lifecycle_database(root, rows):
    data = root / "data"
    data.mkdir()
    database = data / "studio.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE runs(id TEXT PRIMARY KEY, created_at TEXT, status TEXT,
                document TEXT, idempotency_key TEXT UNIQUE);
            CREATE TABLE deleted_runs(id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE);
            CREATE TABLE baselines(slot TEXT PRIMARY KEY, run_id TEXT, target TEXT);
            CREATE TABLE session_reviews(run_id TEXT, target TEXT);
            CREATE TABLE events(id INTEGER PRIMARY KEY, run_id TEXT);
            CREATE TABLE state(key TEXT PRIMARY KEY, document TEXT);
        """)
        connection.execute(
            "INSERT INTO state VALUES(?,?)",
            (
                "session_setup_control",
                json.dumps({"active": True, "outcome": "requested"}),
            ),
        )
        for rid, status, finished_at in rows:
            connection.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?)",
                (
                    rid,
                    "2025-01-01T00:00:00+00:00",
                    status,
                    json.dumps(
                        {"id": rid, "status": status, "finished_at": finished_at}
                    ),
                    "request-" + rid,
                ),
            )
            connection.execute(
                "INSERT INTO baselines VALUES(?,?,?)", ("slot-" + rid, rid, "model")
            )
            connection.execute(
                "INSERT INTO session_reviews VALUES(?,?)", (rid, "model")
            )
            connection.execute("INSERT INTO events(run_id) VALUES(?)", (rid,))
            directory = data / "runs" / rid
            directory.mkdir(parents=True)
            (directory / "manifest.json").write_text("{}")
            export = data / "exports" / (rid + ".zip")
            export.parent.mkdir(exist_ok=True)
            export.write_bytes(b"zip")
    return database


def test_portal_stop_captures_request_time_and_start_retries_safely(
    tmp_path, monkeypatch
):
    mod = updater_module()
    mod.ROOT = tmp_path
    (tmp_path / "compose.yaml").write_text("name: betterbench\n")
    statuses = [
        "queued",
        "blocked",
        "starting",
        "running",
        "grading",
        "stopping",
        "awaiting_review",
        "resume_queued",
    ]
    rows = [(status, status, None) for status in statuses] + [
        ("just_finished", "completed", "2026-01-01T00:00:00.700000+00:00"),
        ("just_before", "completed", "2026-01-01T00:00:00.400000+00:00"),
        ("older_completed", "completed", "2025-01-01T00:00:00+00:00"),
        ("older_failed", "failed", "2025-01-01T00:00:00+00:00"),
        ("older_cancelled", "cancelled", "2025-01-01T00:00:00+00:00"),
    ]
    database = lifecycle_database(tmp_path, rows)
    (tmp_path / "data" / "session-setup.json").write_text(
        json.dumps(
            {
                "owner": "session-setup-local",
                "request_id": "setup-request",
                "phase": "preparing",
            }
        )
    )
    preparation = (
        tmp_path
        / "data"
        / "session-validation"
        / "session-setup-local"
        / "setup-request"
    )
    preparation.mkdir(parents=True)
    (preparation / "preparation.log").write_text("partial preparation")
    receipt = tmp_path / "data" / "session-preparation.json"
    receipt.write_text(
        json.dumps(
            {
                "evidence": str(
                    tmp_path / "data" / "session-validation" / "older-evidence"
                ),
                "suites": {
                    "coding-sessions": {
                        "passed": True,
                        "qualified_run": "just_finished",
                    },
                    "vision-checks": {
                        "passed": True,
                        "qualified_run": "older_completed",
                    },
                },
            }
        )
    )
    monkeypatch.setenv("SERVICE_PORTAL_ACTION_REQUESTED_AT", "2026-01-01T00:00:00.500Z")
    monkeypatch.setattr(mod, "runtime_preflight", lambda: None)
    calls = []
    containers = {"worker-1", "prep-1"}
    stops = 0

    def fake_run(*args, **kwargs):
        nonlocal stops
        calls.append(args)
        output = ""
        if args[:3] == ("docker", "compose", "stop"):
            guard = json.loads((tmp_path / "data" / ".maintenance").read_text())
            assert {row["id"] for row in guard["captured"]} == set(statuses) | {
                "just_finished"
            }
            # The controller may finish a captured job after Stop was requested.
            if stops == 0:
                with sqlite3.connect(database) as connection:
                    connection.execute(
                        "UPDATE runs SET status='completed' WHERE id='running'"
                    )
                    assert connection.execute(
                        "SELECT 1 FROM runs WHERE id='running'"
                    ).fetchone()
            stops += 1
        elif args[:3] == ("docker", "ps", "-aq"):
            label = args[-1]
            if label.startswith("label=io.bench-studio.project="):
                output = "worker-1\n" if "worker-1" in containers else ""
            elif label == "label=io.bench-studio.preparation=session-setup-local":
                output = "prep-1\n" if "prep-1" in containers else ""
        elif args[:3] == ("docker", "rm", "-f"):
            containers.difference_update(args[3:])
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(mod, "run", fake_run)
    mod.stop()
    assert (tmp_path / "data" / ".maintenance").exists()
    assert not containers
    assert not preparation.exists()
    assert receipt.exists()
    saved_receipt = json.loads(receipt.read_text())
    assert "qualified_run" not in saved_receipt["suites"]["coding-sessions"]
    assert (
        saved_receipt["suites"]["vision-checks"]["qualified_run"] == "older_completed"
    )
    assert (
        json.loads((tmp_path / "data" / "session-setup.json").read_text())["phase"]
        == "paused"
    )
    with sqlite3.connect(database) as connection:
        kept = {row[0] for row in connection.execute("SELECT id FROM runs")}
        assert kept == {
            "just_before",
            "older_completed",
            "older_failed",
            "older_cancelled",
        }
        assert {
            row[0] for row in connection.execute("SELECT id FROM deleted_runs")
        } == set(statuses) | {"just_finished"}
        assert not connection.execute(
            "SELECT 1 FROM events WHERE run_id='running'"
        ).fetchone()
        assert not connection.execute(
            "SELECT 1 FROM baselines WHERE run_id='running'"
        ).fetchone()
        assert not connection.execute(
            "SELECT 1 FROM session_reviews WHERE run_id='running'"
        ).fetchone()
        assert (
            json.loads(
                connection.execute(
                    "SELECT document FROM state WHERE key='session_setup_control'"
                ).fetchone()[0]
            )["active"]
            is False
        )
    for rid in set(statuses) | {"just_finished"}:
        assert not (tmp_path / "data" / "runs" / rid).exists()
        assert not (tmp_path / "data" / "exports" / (rid + ".zip")).exists()
    for rid in ("just_before", "older_completed", "older_failed", "older_cancelled"):
        assert (tmp_path / "data" / "runs" / rid / "manifest.json").exists()
    mod.start()
    assert not (tmp_path / "data" / ".maintenance").exists()
    assert (
        "docker",
        "compose",
        "up",
        "-d",
        "--no-build",
        "--wait",
        "--wait-timeout",
        "150",
        "reports",
        "runner",
    ) in calls
    assert not any(
        args[-1] in {"label=io.bench-studio.run", "label=io.bench-studio.preparation"}
        for args in calls
        if args[:3] == ("docker", "ps", "-aq")
    )
    assert not any("build" in args[:3] for args in calls)


def test_portal_stop_failure_keeps_guard_for_start_cleanup(tmp_path, monkeypatch):
    mod = updater_module()
    mod.ROOT = tmp_path
    database = lifecycle_database(tmp_path, [("unfinished", "queued", None)])
    monkeypatch.setattr(mod, "runtime_preflight", lambda: None)
    fail_health = False

    def fake_run(*args, **kwargs):
        if fail_health and args[:3] == ("docker", "compose", "up"):
            raise RuntimeError("reports health failed")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(mod, "run", fake_run)
    with monkeypatch.context() as patch:
        patch.setattr(
            mod.shutil,
            "rmtree",
            lambda path: (_ for _ in ()).throw(PermissionError("read-only artifacts")),
        )
        with pytest.raises(PermissionError, match="read-only artifacts"):
            mod.stop()
    assert (tmp_path / "data" / ".maintenance").exists()
    assert (tmp_path / "data" / "runs" / "unfinished").exists()
    with sqlite3.connect(database) as connection:
        assert not connection.execute(
            "SELECT 1 FROM runs WHERE id='unfinished'"
        ).fetchone()
        assert connection.execute(
            "SELECT 1 FROM deleted_runs WHERE id='unfinished'"
        ).fetchone()
    fail_health = True
    with pytest.raises(RuntimeError, match="reports health failed"):
        mod.start()
    assert not (tmp_path / "data" / "runs" / "unfinished").exists()
    assert (tmp_path / "data" / ".maintenance").exists()
    fail_health = False
    mod.start()
    assert not (tmp_path / "data" / "runs" / "unfinished").exists()
    assert not (tmp_path / "data" / ".maintenance").exists()


def test_stop_preserves_preparation_evidence_already_saved_in_receipt(
    tmp_path, monkeypatch
):
    mod = updater_module()
    mod.ROOT = tmp_path
    lifecycle_database(tmp_path, [])
    preparation = tmp_path / "data" / "session-validation" / "owner" / "request"
    evidence = preparation / "validation"
    evidence.mkdir(parents=True)
    (evidence / "passed.json").write_text('{"passed":true}')
    (tmp_path / "data" / "session-setup.json").write_text(
        json.dumps({"owner": "owner", "request_id": "request", "phase": "preparing"})
    )
    (tmp_path / "data" / "session-preparation.json").write_text(
        json.dumps(
            {"evidence": str(evidence), "suites": {"coding-sessions": {"passed": True}}}
        )
    )
    monkeypatch.setattr(mod, "runtime_preflight", lambda: None)
    monkeypatch.setattr(
        mod, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "", "")
    )
    mod.stop()
    assert (evidence / "passed.json").exists()


def test_stop_preserves_completed_eligibility_status(tmp_path, monkeypatch):
    mod = updater_module()
    mod.ROOT = tmp_path
    database = lifecycle_database(tmp_path, [])
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE state SET document=? WHERE key='session_setup_control'",
            (json.dumps({"active": False, "outcome": "ready"}),),
        )
    setup = {
        "owner": "owner",
        "request_id": "request",
        "phase": "ready",
        "detail": "Eligibility checks passed",
        "suites": {"coding-sessions": {"phase": "passed"}},
    }
    path = tmp_path / "data" / "session-setup.json"
    path.write_text(json.dumps(setup))
    monkeypatch.setattr(mod, "runtime_preflight", lambda: None)
    monkeypatch.setattr(
        mod, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "", "")
    )
    mod.stop()
    assert json.loads(path.read_text()) == setup


@pytest.mark.parametrize("failure", [None, "build", "health"])
def test_update_from_stopped_reconciles_cleanup_and_preserves_guard_on_failure(
    tmp_path, monkeypatch, failure
):
    mod = updater_module()
    mod.ROOT = tmp_path
    database = lifecycle_database(tmp_path, [("unfinished", "queued", None)])
    mod.atomic_marker(
        {
            "action": "stop",
            "started_at": "2026-01-01T00:00:00+00:00",
            "requested_at": "2026-01-01T00:00:00Z",
            "captured": [{"id": "unfinished", "idempotency_key": "request-unfinished"}],
        }
    )
    monkeypatch.setattr(mod, "preflight", lambda: None)
    config = compose_config(tmp_path)
    calls = []
    failing = failure
    revisions = iter(["old", "new"] * 2)

    def fake_run(*args, **kwargs):
        nonlocal failing
        calls.append(args)
        if failing == "build" and args[:5] == (
            "docker",
            "compose",
            "--profile",
            "images",
            "build",
        ):
            raise RuntimeError("build failed")
        if failing == "health" and args[:3] == ("docker", "compose", "up"):
            raise RuntimeError("health failed")
        if args[:4] == ("docker", "compose", "config", "--format"):
            output = json.dumps(config)
        elif args == ("git", "rev-parse", "HEAD"):
            output = next(revisions)
        elif args[:3] == ("docker", "image", "inspect"):
            output = "sha256:previous-image"
        else:
            output = ""
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(mod, "run", fake_run)
    if failure:
        with pytest.raises(RuntimeError, match="failed"):
            mod.deploy()
        assert (
            json.loads((tmp_path / "data" / ".maintenance").read_text())["action"]
            == "stop"
        )
        failing = None
    mod.deploy()
    assert not (tmp_path / "data" / ".maintenance").exists()
    assert not (tmp_path / "data" / "runs" / "unfinished").exists()
    with sqlite3.connect(database) as connection:
        assert not connection.execute(
            "SELECT 1 FROM runs WHERE id='unfinished'"
        ).fetchone()
        assert connection.execute(
            "SELECT 1 FROM deleted_runs WHERE id='unfinished'"
        ).fetchone()
    assert any(args[:3] == ("docker", "compose", "stop") for args in calls)
    assert any(
        args[:5] == ("docker", "compose", "--profile", "images", "build")
        for args in calls
    )
    assert any(args[:3] == ("docker", "compose", "up") for args in calls)


def test_runtime_state_is_ignored_by_git_and_docker():
    root = Path(__file__).resolve().parents[1]
    paths = [
        ".env",
        "data/studio.sqlite3",
        "data/runs/saved/report.json",
        "data/backups/20260101/studio.sqlite3",
        "datasets/cache/HumanEvalPlus.jsonl",
    ]
    for path in paths:
        result = subprocess.run(
            ["git", "check-ignore", "-q", "--no-index", path], cwd=root
        )
        assert result.returncode == 0, path
    docker_ignores = set((root / ".dockerignore").read_text().splitlines())
    assert {".env", "data", "datasets/cache"} <= docker_ignores
    assert (
        subprocess.run(
            [
                "git",
                "check-ignore",
                "-q",
                "--no-index",
                "datasets/coding-manifest.json",
            ],
            cwd=root,
        ).returncode
        == 1
    )


def test_coding_preparation_keeps_committed_manifest_clean(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "scripts").mkdir()
    (tmp_path / "datasets").mkdir()
    script = tmp_path / "scripts" / "prepare-coding-data.py"
    shutil.copyfile(root / "scripts" / script.name, script)
    urls = {
        key: record["url"]
        for key, record in json.loads(
            (root / "datasets" / "coding-manifest.json").read_text()
        ).items()
        if isinstance(record, dict) and "url" in record
    }
    rows = {
        "humanevalplus": [{"task_id": "HumanEval/0"}],
        "typescript": [{"name": "ts/0"}],
    }
    raw = {
        "humanevalplus": gzip.compress(b'{"task_id":"HumanEval/0"}\n'),
        "typescript": b"test-parquet-bytes",
    }
    manifest = {
        key: {
            "url": urls[key],
            "sha256": hashlib.sha256(raw[key]).hexdigest(),
            "count": 1,
            "task_ids": [row.get("task_id") or row.get("name") for row in rows[key]],
        }
        for key in urls
    }
    manifest_path = tmp_path / "datasets" / "coding-manifest.json"
    original = json.dumps(manifest, indent=2) + "\n"
    manifest_path.write_text(original)
    (tmp_path / ".gitignore").write_text("/datasets/cache/\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "baseline",
        ],
        cwd=tmp_path,
        check=True,
    )
    parquet = types.ModuleType("pyarrow.parquet")
    parquet.read_table = lambda stream: types.SimpleNamespace(
        to_pylist=lambda: rows["typescript"]
    )
    pyarrow = types.ModuleType("pyarrow")
    pyarrow.__path__ = []
    pyarrow.parquet = parquet
    monkeypatch.setitem(sys.modules, "pyarrow", pyarrow)
    monkeypatch.setitem(sys.modules, "pyarrow.parquet", parquet)
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda url, timeout=60: types.SimpleNamespace(
            read=lambda: raw[next(key for key, value in urls.items() if value == url)]
        ),
    )

    runpy.run_path(str(script))
    assert manifest_path.read_text() == original
    assert (tmp_path / "datasets" / "cache" / "HumanEvalPlus.jsonl").is_file()
    assert (tmp_path / "datasets" / "cache" / "typescript.json").is_file()
    assert not subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    manifest["humanevalplus"]["sha256"] = "invalid"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="Dataset digest mismatch"):
        runpy.run_path(str(script))
