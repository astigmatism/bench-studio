import asyncio
import contextlib
import json
import shutil
import uuid
import zipfile
from datetime import datetime, timezone
from urllib.parse import urlparse
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict
from common import now
from . import config, db, discovery, profiles, results


@contextlib.asynccontextmanager
async def lifespan(app):
    db.initialize()
    results.import_legacy()
    yield


app = FastAPI(title="Bench Studio", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def same_origin(request: Request, call_next):
    if request.method not in ["GET", "HEAD", "OPTIONS"]:
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return JSONResponse(
                {"detail": "Cross-origin mutations are not allowed"}, status_code=403
            )
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            return JSONResponse(
                {"detail": "application/json required"}, status_code=415
            )
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


@app.exception_handler(ValueError)
async def value_error(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=400)


class Launch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    targets: list[str] = Field(min_length=1, max_length=8)
    profile: str
    size: str = "standard"
    overrides: dict = Field(default_factory=dict)
    mode: str = "sequential"
    note: str = Field(default="", max_length=1000)
    idempotency_key: str = Field(min_length=8, max_length=100)
    difficulty: str | None = None
    task_selection: str | None = None
    repetitions: int | None = Field(default=None, strict=True)
    review_mode: str | None = None
    qualification: bool = False
    setup_request_id: str | None = None


class SetupStart(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=100)
    run_smoke: bool = False


class Custom(BaseModel):
    profile: str
    size: str = "standard"
    overrides: dict = Field(default_factory=dict)
    name: str = Field(min_length=1, max_length=70)
    difficulty: str | None = None
    task_selection: str | None = None
    repetitions: int | None = Field(default=None, strict=True)
    review_mode: str | None = None


def session_options(body):
    return {
        k: getattr(body, k)
        for k in ("difficulty", "task_selection", "repetitions", "review_mode")
    }


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    feedback: str = Field(default="", max_length=8000)
    idempotency_key: str = Field(min_length=8, max_length=100)


class Baseline(BaseModel):
    target: str


class DeleteRuns(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[str] = Field(min_length=1)


def require_run(rid):
    m = db.get_run(rid)
    if not m:
        raise HTTPException(404, "Run not found")
    return m


@app.get("/api/health")
def health():
    from .session_setup import status as setup_status

    with db.connect() as c:
        c.execute("SELECT 1")
    return {
        "ok": True,
        "revision": config.REVISION,
        "runner": db.state("runner"),
        "session_setup": setup_status(),
    }


@app.get("/api/models")
def models():
    try:
        data = discovery.discover()
        data.pop("snapshot", None)
        return data
    except Exception as e:
        return JSONResponse(
            {"models": [], "error": str(e), "runtime": {"ready": False}},
            status_code=503,
        )


@app.post("/api/session-setup/start", status_code=202)
def start_session_setup(body: SetupStart):
    from . import session_control

    return session_control.start(body.idempotency_key, run_smoke=body.run_smoke)


@app.post("/api/session-setup/stop", status_code=202)
def stop_session_setup():
    from . import session_control

    return session_control.stop()


@app.get("/api/profiles")
def list_profiles():
    return profiles.all_profiles()


@app.post("/api/profiles", status_code=201)
def save_profile(body: Custom):
    p = profiles.configure(
        body.profile, body.size, body.overrides, **session_options(body)
    )
    p.update(
        id="custom-" + uuid.uuid4().hex[:12],
        name=body.name,
        builtin=False,
        parent=body.profile,
        sizes=[body.size],
    )
    p["fingerprint"] = profiles.fingerprint(p)
    with db.transaction() as c:
        c.execute("INSERT INTO profiles VALUES(?,?)", (p["id"], db.pack(p)))
    return p


@app.get("/api/runs")
def list_runs():
    return [results.enrich(m) for m in db.runs()]


@app.get("/api/runs/{rid}")
def get_run(rid: str):
    return results.enrich(require_run(rid))


@app.post("/api/runs/delete")
def delete_runs(body: DeleteRuns):
    ids = list(dict.fromkeys(body.ids))
    if any(not rid or rid in {".", ".."} or "/" in rid or "\\" in rid for rid in ids):
        raise ValueError("Invalid run ID")
    with db.transaction() as c:
        if (config.DATA / ".maintenance").exists():
            raise HTTPException(409, "Application update in progress")
        rows = []
        for rid in ids:
            row = c.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
            if row is None:
                if c.execute(
                    "SELECT 1 FROM deleted_runs WHERE id=?", (rid,)
                ).fetchone():
                    continue
                raise HTTPException(404, "Run not found; nothing was deleted")
            if row["status"] not in db.TERMINAL:
                raise HTTPException(
                    409,
                    "Stop unfinished runs before deleting them; nothing was deleted",
                )
            rows.append(row)
        for row in rows:
            rid = row["id"]
            c.execute(
                "INSERT INTO deleted_runs VALUES(?,?)", (rid, row["idempotency_key"])
            )
            for table in ("baselines", "session_reviews", "events"):
                c.execute(f"DELETE FROM {table} WHERE run_id=?", (rid,))
            c.execute("DELETE FROM runs WHERE id=?", (rid,))
        if rows:
            db.event(
                c, None, {"type": "runs_deleted", "ids": [row["id"] for row in rows]}
            )
    # Commit history removal first. Tombstones also make retries safe and prevent
    # leftover legacy manifests from being imported if disk cleanup fails.
    cleanup_failed = []
    for rid in ids:
        try:
            for path in (
                config.DATA / "runs" / rid,
                config.DATA / "exports" / (rid + ".zip"),
            ):
                if path.parent.is_symlink():
                    raise OSError("Artifact directory is a symlink")
                if path.is_symlink() or path.is_file():
                    path.unlink()
                elif path.exists():
                    shutil.rmtree(path)
        except OSError:
            cleanup_failed.append(rid)
    return {"deleted": ids, "cleanup_failed": cleanup_failed}


@app.post("/api/runs", status_code=202)
def launch(body: Launch):
    if body.setup_request_id and not body.qualification:
        raise ValueError("Setup ownership is only valid for qualification runs")
    if body.mode not in ["sequential", "parallel"]:
        raise ValueError("Invalid execution mode")
    if len(set(body.targets)) != len(body.targets):
        raise ValueError("Duplicate targets")
    with db.connect() as c:
        if c.execute(
            "SELECT 1 FROM deleted_runs WHERE idempotency_key=?",
            (body.idempotency_key,),
        ).fetchone():
            raise HTTPException(
                409,
                "This launch request belongs to a deleted run. Start a new benchmark.",
            )
        prior = c.execute(
            "SELECT document FROM runs WHERE idempotency_key=?", (body.idempotency_key,)
        ).fetchone()
        if prior:
            return db.unpack(prior)
    profile = profiles.configure(
        body.profile, body.size, body.overrides, **session_options(body)
    )
    if body.qualification:
        if (
            profile["family"] not in {"session", "vision"}
            or profile["repetitions"] != 1
            or profile["task_selection"] == "all"
            or profile["review_mode"] != "unattended"
            or len(body.targets) != 1
        ):
            raise ValueError(
                "Qualification requires one unattended task, one repetition and one model"
            )
        profile["qualification"] = True
    if profile["family"] in {"session", "vision"} and body.mode != "sequential":
        raise ValueError("Session and vision suites run models sequentially")
    profile = profiles.attach_manifest(profile)
    data = discovery.discover()
    available = {m["alias"]: m for m in data["models"] if m["available"]}
    if any(t not in available for t in body.targets):
        raise ValueError("Selected model is no longer available; refresh discovery")
    selected = {t: available[t]["resolved"] for t in body.targets}
    if len({r["service"]["container_name"] for r in selected.values()}) != len(
        selected
    ):
        raise ValueError("Targets must use distinct backends")
    for r in selected.values():
        from .session_catalog import vision_support

        if profile.get("requires_vision") and vision_support(r["metadata"]) is not True:
            raise ValueError(
                "Selected model does not advertise image input and vision support"
            )
        if profile["parameters"].get("reasoning_budget_tokens") is not None:
            levels = r["metadata"].get("reasoning", {}).get("per_effort", {})
            if not any(
                "reasoning_budget_tokens" in v
                for v in levels.values()
                if isinstance(v, dict)
            ):
                raise ValueError(
                    "Selected endpoint does not advertise reasoning-budget support"
                )
        limit = profile["parameters"].get("max_tokens")
        if limit and limit + r["reserve"] + 2048 >= r["context"]:
            raise ValueError("Output budget exceeds available context")
        effort = profile["parameters"]["reasoning_effort"]
        if effort != "default" and effort not in r["metadata"].get("reasoning", {}).get(
            "efforts", {}
        ):
            raise ValueError("Selected model does not advertise this reasoning setting")
    rid = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid.uuid4().hex[:8]
    )
    m = {
        "id": rid,
        "created_at": now(),
        "updated_at": now(),
        "status": "queued",
        "requested_targets": body.targets,
        "profile": profile["id"],
        "profile_spec": profile,
        "family": profile["family"],
        "mode": body.mode,
        "note": body.note,
        "resolved": selected,
        "settings": config.SETTINGS,
        "targets": {},
        "progress": "Waiting for an idle machine",
        "revision": config.REVISION,
    }
    if body.setup_request_id:
        m["setup_request_id"] = body.setup_request_id
    with db.transaction() as c:
        prior = c.execute(
            "SELECT document FROM runs WHERE idempotency_key=?", (body.idempotency_key,)
        ).fetchone()
        if prior:
            return db.unpack(prior)
        if (config.DATA / ".maintenance").exists():
            raise HTTPException(409, "Application update in progress")
        if body.setup_request_id:
            from .session_control import require_active

            require_active(c, body.setup_request_id)
        db.put_run(c, m, body.idempotency_key)
    return m


@app.post("/api/runs/{rid}/cancel")
def cancel(rid: str):
    with db.transaction() as c:
        m = db.unpack(
            c.execute("SELECT document FROM runs WHERE id=?", (rid,)).fetchone()
        )
        if not m:
            raise HTTPException(404, "Run not found")
        if m["status"] not in db.TERMINAL:
            if m["status"] in ["queued", "blocked"]:
                m.update(
                    status="cancelled",
                    finished_at=now(),
                    progress="Cancelled before execution",
                )
            else:
                m["cancel_requested"] = True
            db.update_run(m, c=c)
    return m


@app.post("/api/runs/{rid}/baseline")
def baseline(rid: str, body: Baseline):
    m = require_run(rid)
    if m["status"] != "completed":
        raise ValueError("Only completed runs can become baselines")
    result = results.summarize(m).get(body.target)
    if not result or result.get("score") is None:
        raise ValueError("No valid score for selected target")
    slot = results.baseline_slot(m, body.target)
    with db.transaction() as c:
        if not c.execute("SELECT 1 FROM runs WHERE id=?", (rid,)).fetchone():
            raise HTTPException(404, "Run not found")
        c.execute(
            "INSERT OR REPLACE INTO baselines VALUES(?,?,?)", (slot, rid, body.target)
        )
    return {"ok": True}


@app.get("/api/runs/{rid}/reviews")
def run_reviews(rid: str):
    from .session_reviews import reviews

    require_run(rid)
    return reviews(rid)


@app.post("/api/runs/{rid}/reviews/{target}/{attempt_id}/{revision}")
def review_decision(
    rid: str, target: str, attempt_id: str, revision: int, body: ReviewDecision
):
    from .session_reviews import decide

    require_run(rid)
    return decide(
        rid,
        target,
        attempt_id,
        revision,
        body.action,
        body.feedback,
        body.idempotency_key,
    )


@app.get("/api/compare")
def compare(a: str, b: str, ta: str, tb: str):
    return results.compare(require_run(a), require_run(b), ta, tb)


@app.get("/api/runs/{rid}/logs")
def logs(rid: str):
    require_run(rid)
    p = config.DATA / "runs" / rid / "run.log"
    if not p.exists():
        return {"text": "Waiting for execution.", "size": 0}
    with p.open("rb") as f:
        size = p.stat().st_size
        f.seek(max(0, size - 150000))
        data = f.read()
    return {"text": data.decode(errors="replace"), "size": size}


@app.get("/runs/{rid}/{name:path}")
@app.get("/api/runs/{rid}/artifacts/{name:path}")
def artifact(rid: str, name: str):
    require_run(rid)
    root = (config.DATA / "runs" / rid).resolve()
    p = (root / name).resolve()
    lexical = root / name
    if (
        not p.is_relative_to(root)
        or not p.is_file()
        or any(
            x.is_symlink()
            for x in [lexical, *lexical.parents]
            if x.is_relative_to(root)
        )
    ):
        raise HTTPException(404, "Artifact not found")
    return FileResponse(
        p,
        headers={
            "Content-Security-Policy": (
                "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'; form-action 'none'; frame-ancestors 'self'"
                if p.name == "prototype.html"
                else "sandbox allow-scripts; default-src 'self' 'unsafe-inline' data:; frame-ancestors 'self'"
            ),
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/runs/{rid}/export")
def export(rid: str):
    require_run(rid)
    root = config.DATA / "runs" / rid
    out = config.DATA / "exports"
    out.mkdir(exist_ok=True)
    dest = out / (rid + ".zip")
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("studio-run.json", json.dumps(require_run(rid), indent=2))
        if root.exists():
            for p in root.rglob("*"):
                if (
                    p.is_file()
                    and not p.is_symlink()
                    and p.stat().st_size < 100 * 1024 * 1024
                ):
                    z.write(p, p.relative_to(root))
    return FileResponse(dest, filename=dest.name)


@app.get("/api/events")
async def events(request: Request):
    try:
        cursor = int(request.headers.get("last-event-id", "0"))
    except ValueError:
        cursor = 0

    async def stream():
        nonlocal cursor
        while not await request.is_disconnected():
            with db.connect() as c:
                rows = c.execute(
                    "SELECT * FROM events WHERE id>? ORDER BY id LIMIT 100", (cursor,)
                ).fetchall()
            for row in rows:
                cursor = row["id"]
                yield f"id: {cursor}\ndata: {row['document']}\n\n"
            if not rows:
                yield ": heartbeat\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


static = config.ROOT / "frontend" / "dist"
if static.exists():
    app.mount("/", StaticFiles(directory=static, html=True), name="frontend")
