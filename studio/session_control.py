"""Durable, explicit permission to prepare fixtures and run qualification."""

import uuid
from common import now
from . import config, db

KEY = "session_setup_control"


def owned(row):
    run = db.unpack(row)
    return bool(
        run.get("setup_request_id")
        or (row["idempotency_key"] or "").startswith("automatic-qualification-")
    )


def stopping():
    with db.connect() as c:
        return any(
            owned(row) and db.unpack(row).get("cancel_requested")
            for row in c.execute(
                "SELECT document,idempotency_key FROM runs WHERE status IN ('starting','running','grading','stopping','awaiting_review','resume_queued')"
            )
        )


def read(c):
    return (
        db.unpack(
            c.execute("SELECT document FROM state WHERE key=?", (KEY,)).fetchone()
        )
        or {}
    )


def save(c, value):
    c.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (KEY, db.pack(value)))


def cancel_runs(c):
    """Cancel setup-owned jobs, including those queued by the old automatic setup."""
    for row in c.execute("SELECT document,idempotency_key FROM runs").fetchall():
        run = db.unpack(row)
        if (
            not owned(row)
            or run["status"] in db.TERMINAL
            or run.get("cancel_requested")
        ):
            continue
        if run["status"] in {"queued", "blocked"}:
            run.update(
                status="cancelled",
                finished_at=now(),
                progress="Suite setup stopped before execution",
            )
        else:
            run["cancel_requested"] = True
        db.update_run(run, c=c)


def start(key):
    with db.transaction() as c:
        if (config.DATA / ".maintenance").exists():
            raise ValueError("Application update in progress")
        current = read(c)
        if current.get("idempotency_key") == key or (
            current.get("active") and current.get("revision") == config.REVISION
        ):
            return current
        cancel_runs(c)
        current = dict(
            id=uuid.uuid4().hex,
            idempotency_key=key,
            revision=config.REVISION,
            active=True,
            requested_at=now(),
            outcome="requested",
        )
        save(c, current)
        return current


def stop():
    with db.transaction() as c:
        current = read(c)
        current.update(active=False, outcome="stopped", stopped_at=now())
        save(c, current)
        cancel_runs(c)
        return current


def claim(owner):
    with db.transaction() as c:
        current = read(c)
        if current.get("active"):
            if (
                current.get("revision") != config.REVISION
                or current.get("owner", owner) != owner
            ):
                current.update(active=False, outcome="interrupted", stopped_at=now())
                save(c, current)
            elif not current.get("owner"):
                current["owner"] = owner
                save(c, current)
        if not current.get("active"):
            cancel_runs(c)
        return current


def finish(request_id, outcome):
    with db.transaction() as c:
        current = read(c)
        if current.get("id") == request_id:
            current.update(active=False, outcome=outcome, finished_at=now())
            save(c, current)


def require_active(c, request_id):
    current = read(c)
    if (
        not current.get("active")
        or current.get("id") != request_id
        or current.get("revision") != config.REVISION
    ):
        raise ValueError(
            "Suite setup was stopped or interrupted; select Start setup to run it again"
        )
