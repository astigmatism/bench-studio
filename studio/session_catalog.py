"""Versioned, local workloads. Preparation receipts are evidence, never task inputs."""

import hashlib
import json
import os
import contextlib
import fcntl
from pathlib import Path
from . import config

FAMILIES = {"session", "vision"}
TIERS = {"small": (1800, 100), "medium": (5400, 300), "large": (14400, 800)}
PROTOCOL_VERSION = 1
ROOT = config.ROOT / "datasets" / "sessions"


def vision_support(metadata):
    capabilities = metadata.get("capabilities")
    advertised = (
        "vision" in capabilities
        if isinstance(capabilities, list)
        else capabilities.get("vision")
        if isinstance(capabilities, dict)
        else None
    )
    if advertised is None or metadata.get("input_modalities") is None:
        return None
    return advertised is True and "image" in metadata["input_modalities"]


def digest_tree(root):
    rows = {}
    excluded = {"node_modules", ".git", "__pycache__", ".pytest_cache", "dist"}
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in excluded]
        if any((Path(base) / d).is_symlink() for d in dirs):
            raise ValueError("Fixture inputs cannot contain symlinks")
        for name in files:
            path = Path(base) / name
            if path.is_symlink():
                raise ValueError("Fixture inputs cannot contain symlinks")
            rows[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def catalog():
    return json.loads((ROOT / "manifest.json").read_text())


def tasks(profile):
    if "pinned_tasks" in profile:
        return profile["pinned_tasks"]
    suite = profile.get("suite", profile["id"])
    rows = catalog()["vision_checks" if suite == "vision-checks" else "tasks"]
    if suite != "vision-checks":
        rows = [r for r in rows if r["difficulty"] == profile["difficulty"]]
    selected = profile.get("task_selection", "all")
    return [r for r in rows if selected == "all" or r["id"] == selected]


def receipt():
    path = config.DATA / "session-preparation.json"
    return json.loads(path.read_text()) if path.exists() else {}


@contextlib.contextmanager
def receipt_lock():
    config.DATA.mkdir(parents=True, exist_ok=True)
    with (config.DATA / ".session-preparation.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def save_preparation(evidence):
    from common import atomic_json

    with receipt_lock():
        current = receipt()
        if all(
            current.get(k) == evidence.get(k)
            for k in ("source_hash", "image_id", "protocol_version")
        ):
            suites = {**current.get("suites", {}), **evidence["suites"]}
            for key, value in current.get("suites", {}).items():
                if value.get("qualified_run"):
                    suites[key]["qualified_run"] = value["qualified_run"]
            evidence["suites"] = suites
        atomic_json(config.DATA / "session-preparation.json", evidence)


def qualification_state(state):
    """Read live run state even while the setup coordinator yields to execution."""
    from . import db

    if not state.get("run_id"):
        return state
    run = db.get_run(state["run_id"])
    if not run:
        return dict(
            state,
            phase="missing_run",
            detail="Qualification run is unavailable; setup needs attention.",
        )
    phase = run["status"]
    detail = run.get("error") or run.get("progress")
    if phase == "completed":
        phase = "not_passed"
        detail = (
            "Smoke run finished without passing qualification. "
            "Open the run for test evidence, then use Run again to retry."
        )
    progress = run.get("session_progress") or {}
    return dict(
        state,
        phase=phase,
        detail=detail,
        turns=progress.get("turns"),
        started_at=run.get("started_at"),
        updated_at=run.get("updated_at"),
    )


def readiness(suite, *, evidence=None, setup=None):
    evidence = receipt() if evidence is None else evidence
    setup_path = config.DATA / "session-setup.json"
    if setup is None:
        setup = json.loads(setup_path.read_text()) if setup_path.exists() else {}
    prepared = (
        evidence.get("source_hash") == digest_tree(ROOT)
        and evidence.get("protocol_version") == PROTOCOL_VERSION
        and evidence.get("suites", {}).get(suite, {}).get("passed") is True
        and (
            setup.get("revision") != config.REVISION
            or not setup.get("expected_image_id")
            or setup["expected_image_id"] == evidence.get("image_id")
        )
    )
    ready = prepared and bool(
        evidence.get("suites", {}).get(suite, {}).get("qualified_run")
    )
    reason = "Select Start setup to validate this suite and run its qualification smoke test. No checks start automatically."
    if setup:
        if setup.get("phase") in {
            "preparing",
            "failed",
            "interrupted",
            "paused",
            "requested",
        }:
            reason = setup["detail"]
        elif prepared and not ready:
            state = qualification_state(setup.get("suites", {}).get(suite, {}))
            reason = (
                "Live qualification "
                + state.get("phase", "pending").replace("_", " ")
                + (" · run " + state["run_id"] if state.get("run_id") else "")
                + ". "
                + (
                    state.get("detail")
                    or "This suite becomes available after its smoke run passes."
                )
            )
    return {
        "ready": bool(ready),
        "prepared": bool(prepared),
        "reason": None if ready else reason,
    }


def attach(profile):
    from .profiles import fingerprint

    suite = profile["suite"]
    evidence = receipt()
    if not readiness(suite)["ready"] and not (
        profile.get("qualification") and readiness(suite)["prepared"]
    ):
        raise ValueError(readiness(suite)["reason"])
    selected = tasks(profile)
    profile.update(
        pinned_tasks=selected,
        task_ids=[r["id"] for r in selected],
        task_manifest_hash=fingerprint(
            {"tasks": selected, "source": evidence["source_hash"]}
        ),
        fixture_source_hash=evidence["source_hash"],
        session_image=evidence["image_id"],
        base_revisions=evidence["base_revisions"],
        execution_adapter_version=PROTOCOL_VERSION,
        acceptance_version=1,
        compaction_version=1,
        screenshot_renderer=catalog().get("screenshot_renderer")
        if suite == "vision-checks"
        else None,
    )
    return profile


def qualify_run(run):
    if run.get("status") != "completed" or not run["profile_spec"].get("qualification"):
        return
    from common import atomic_json, read_json

    for target in run["requested_targets"]:
        summary = read_json(config.DATA / "runs" / run["id"] / target / "result.json")
        if summary.get("partial") or not summary.get("passed"):
            return
    with receipt_lock():
        evidence = receipt()
        if evidence.get("source_hash") != run["profile_spec"].get(
            "fixture_source_hash"
        ) or evidence.get("image_id") != run["profile_spec"].get("session_image"):
            return
        evidence["suites"][run["profile_spec"]["suite"]]["qualified_run"] = run["id"]
        atomic_json(config.DATA / "session-preparation.json", evidence)


def builtin_profiles():
    common = {
        "version": 1,
        "builtin": True,
        "engine": "Harbor 0.23.0 / Studio session agent 1",
        "sizes": ["standard"],
        "parameters": {
            "temperature": 0,
            "top_p": 1,
            "seed": 42,
            "reasoning_effort": "default",
            "max_tokens": 8192,
            "reasoning_budget_tokens": None,
        },
    }
    rows = []
    for key, name, description in [
        (
            "coding-sessions",
            "Coding sessions",
            "Plan, approve, implement, and verify a real feature. Time and correctness together.",
        ),
        (
            "vision-checks",
            "Vision checks",
            "Twelve fixed screenshots: text, interface state, and layout defects.",
        ),
        (
            "visual-design",
            "Visual design",
            "Build a clickable prototype, inspect screenshots, and review the design.",
        ),
    ]:
        row = dict(
            common,
            id=key,
            suite=key,
            name=name,
            description=description,
            family="vision" if key == "vision-checks" else "session",
            parameters=dict(common["parameters"]),
            task_selection="all",
            repetitions=1,
            review_mode="unattended" if key == "vision-checks" else "interactive",
            requires_vision=key != "coding-sessions",
            preparation=readiness(key),
        )
        row["tasks"] = catalog()["vision_checks" if key == "vision-checks" else "tasks"]
        if key == "vision-checks":
            row["engine"] = "Studio vision checks 1 / streaming router"
        if key != "vision-checks":
            row.update(
                difficulty="small",
                difficulties=list(TIERS),
                tier_budgets={
                    k: {"task_timeout": v[0], "max_turns": v[1]}
                    for k, v in TIERS.items()
                },
            )
            row["parameters"].update(task_timeout=1800, max_turns=100)
        rows.append(row)
    return rows


def configure(
    profile, *, difficulty=None, task_selection=None, repetitions=None, review_mode=None
):
    if profile["family"] not in FAMILIES:
        if any(
            x is not None
            for x in (difficulty, task_selection, repetitions, review_mode)
        ):
            raise ValueError("Session options require a session or vision profile")
        return profile
    if profile["family"] == "session":
        difficulty = difficulty or profile.get("difficulty", "small")
        if difficulty not in TIERS:
            raise ValueError("Unknown task difficulty")
        if difficulty != profile.get("difficulty"):
            seconds, turns = TIERS[difficulty]
            profile["parameters"].update(task_timeout=seconds, max_turns=turns)
        profile["difficulty"] = difficulty
    elif difficulty is not None:
        raise ValueError("Fixed vision checks do not have difficulty tiers")
    profile["task_selection"] = (
        task_selection
        if task_selection is not None
        else profile.get("task_selection", "all")
    )
    profile["repetitions"] = (
        repetitions if repetitions is not None else profile.get("repetitions", 1)
    )
    profile["review_mode"] = review_mode or profile.get("review_mode", "interactive")
    if type(profile["repetitions"]) is not int or profile["repetitions"] not in (
        1,
        3,
        5,
    ):
        raise ValueError("Repetitions must be 1, 3, or 5")
    if profile["review_mode"] not in ("interactive", "unattended") or (
        profile["family"] == "vision" and profile["review_mode"] != "unattended"
    ):
        raise ValueError("Unsupported review mode")
    if not tasks(profile):
        raise ValueError("Task does not belong to the selected tier")
    return profile
