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
    assert subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", "datasets/coding-manifest.json"],
        cwd=root,
    ).returncode == 1


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
