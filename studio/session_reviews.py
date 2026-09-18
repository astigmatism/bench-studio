"""Transactional human decisions. Only the scheduler grants permission to resume."""

from common import now
from . import db


def reviews(rid):
    with db.connect() as c:
        return [
            db.unpack(r)
            for r in c.execute(
                "SELECT document FROM session_reviews WHERE run_id=? ORDER BY target,attempt_id,revision",
                (rid,),
            )
        ]


def create(rid, target, attempt_id, revision, kind, artifact):
    value = dict(
        run_id=rid,
        target=target,
        attempt_id=attempt_id,
        revision=revision,
        kind=kind,
        artifact=artifact,
        requested_at=now(),
        decision=None,
    )
    with db.transaction() as c:
        c.execute(
            "INSERT INTO session_reviews(run_id,target,attempt_id,revision,document) VALUES(?,?,?,?,?)",
            (rid, target, attempt_id, revision, db.pack(value)),
        )
        db.event(
            c, rid, {"type": "review", "attempt_id": attempt_id, "revision": revision}
        )
    return value


def decide(rid, target, attempt_id, revision, action, feedback, key):
    if action not in {"approve", "revise", "stop"}:
        raise ValueError("Unknown review action")
    if action == "revise" and not feedback.strip():
        raise ValueError("Revision requests need feedback")
    if action != "revise" and feedback.strip():
        raise ValueError("Feedback belongs to a revision request")
    with db.transaction() as c:
        old = c.execute(
            "SELECT document FROM session_reviews WHERE run_id=? AND decision_key=?",
            (rid, key),
        ).fetchone()
        if old:
            value = db.unpack(old)
            if (
                value["target"],
                value["attempt_id"],
                value["revision"],
                value["decision"],
                value.get("feedback", ""),
            ) != (target, attempt_id, revision, action, feedback.strip()):
                raise ValueError(
                    "Idempotency key already belongs to a different decision"
                )
            return value
        run = db.unpack(
            c.execute("SELECT document FROM runs WHERE id=?", (rid,)).fetchone()
        )
        if not run or run["status"] in db.TERMINAL or run.get("cancel_requested"):
            raise ValueError("This run no longer accepts reviews")
        value = db.unpack(
            c.execute(
                "SELECT document FROM session_reviews WHERE run_id=? AND target=? AND attempt_id=? AND revision=?",
                (rid, target, attempt_id, revision),
            ).fetchone()
        )
        if not value or value.get("decision"):
            raise ValueError("Review is stale or already decided")
        latest = c.execute(
            "SELECT MAX(revision) FROM session_reviews WHERE run_id=? AND target=? AND attempt_id=?",
            (rid, target, attempt_id),
        ).fetchone()[0]
        if latest != revision:
            raise ValueError("Review revision is stale")
        value.update(decision=action, feedback=feedback.strip(), decided_at=now())
        c.execute(
            "UPDATE session_reviews SET document=?,decision_key=? WHERE run_id=? AND target=? AND attempt_id=? AND revision=?",
            (db.pack(value), key, rid, target, attempt_id, revision),
        )
        if action == "stop":
            run["cancel_requested"] = True
            db.update_run(run, c=c)
        db.event(
            c,
            rid,
            {"type": "review_decision", "attempt_id": attempt_id, "revision": revision},
        )
    return value
