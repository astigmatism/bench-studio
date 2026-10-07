"""LLM Router subscription for the long-running runner (client contract §4).

One RouterWatch (the router's reference client) holds /v1/router/events for
the runner's lifetime and polls the capabilities document while disconnected.
Each new revision is written to the SQLite events table, so the UI's
/api/events stream shows it, and wakes the runner to re-check active and
queued runs at once. Drain windows are recorded for speed workers.
"""

import threading

from common import client_headers, note_router_state, now, router_url, schema_warnings
from . import config, db
from .router_watch import RouterWatch


class StudioRouterWatch(RouterWatch):
    """The reference client, plus whether the event stream is live."""

    streaming = False
    _following = False

    def _follow(self, stop=None):
        self._following = True
        try:
            return super()._follow(stop)
        finally:
            self._following = False
            self.streaming = False

    def _set(self, doc):
        if self._following:
            self.streaming = True
        super()._set(doc)


def summary(doc):
    """The parts of a capabilities document the UI and logs show."""
    router = doc.get("router") or {}
    configuration = doc.get("configuration") or {}
    return {
        "revision": doc.get("revision"),
        "configuration": configuration.get("id"),
        "exclusive": configuration.get("exclusive"),
        "accepting_requests": router.get("accepting_requests"),
        "drain_reason": router.get("drain_reason"),
        "maintenance": router.get("maintenance"),
        "models": [
            {
                "service": m.get("service"),
                "model": m.get("id"),
                "available": m.get("available"),
                "context_window": m.get("context_window"),
            }
            for m in doc.get("models") or []
        ],
        "offline_services": [
            {
                "service": (o.get("aliases") or [o.get("model")])[0],
                "model": o.get("model"),
                "reason": o.get("reason"),
            }
            for o in doc.get("offline_services") or []
        ],
        "warnings": [*schema_warnings(doc), *(doc.get("warnings") or [])],
    }


def describe(value):
    if value.get("accepting_requests") is False:
        state = "not accepting requests (" + (value.get("drain_reason") or "draining") + ")"
    else:
        state = "accepting requests"
    models = ", ".join(
        f"{m['service']}={m['model']}{'' if m['available'] else ' (unavailable)'}"
        for m in value["models"]
    )
    offline = ", ".join(f"{o['service']} offline ({o['reason']})" for o in value["offline_services"])
    return "; ".join(
        x
        for x in (
            f"configuration {value.get('configuration')}",
            state,
            models,
            offline,
            ("warnings: " + ", ".join(value["warnings"])) if value["warnings"] else "",
        )
        if x
    )


class Subscriber:
    def __init__(self, settings=None, on_revision=None):
        settings = settings or config.SETTINGS
        self.on_revision = on_revision
        self.stop_event = threading.Event()
        self.thread = None
        self.watch = StudioRouterWatch(
            router_url(settings), on_change=self.changed, headers=client_headers()
        )

    @property
    def streaming(self):
        return self.watch.streaming

    @property
    def doc(self):
        return self.watch.doc

    def start(self):
        self.thread = threading.Thread(target=self.run, name="router-watch", daemon=True)
        self.thread.start()
        return self

    def stop(self):
        self.stop_event.set()

    def run(self):
        # Read the document at startup; an unreachable router is tolerated.
        try:
            self.watch.fetch()
        except Exception as exc:
            print(now(), "LLM Router capabilities unavailable; starting degraded:", exc, flush=True)
        self.watch.run_forever(self.stop_event)

    def changed(self, doc):
        # Exceptions here would read as a dropped stream to RouterWatch.
        try:
            record(doc)
        except Exception as exc:
            print(now(), "Router change could not be recorded:", exc, flush=True)
        if self.on_revision:
            try:
                self.on_revision(doc)
            except Exception as exc:
                print(now(), "Router change handler failed:", exc, flush=True)


def record(doc):
    value = summary(doc)
    print(now(), "LLM Router revision", value["revision"], "-", describe(value), flush=True)
    with db.transaction() as c:
        db.event(c, None, {"type": "router_capabilities", **value})
        c.execute(
            "INSERT OR REPLACE INTO state VALUES(?,?)",
            ("router", db.pack({**value, "observed_at": now()})),
        )
    note_router_state(
        config.DATA,
        value["accepting_requests"] is not False,
        value.get("drain_reason") or ("maintenance" if value.get("maintenance") else None),
    )
    return value
