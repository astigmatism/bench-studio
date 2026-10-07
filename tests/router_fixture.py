"""Fixture LLM Router and AI Runtime (client contract 1.1 document shapes).

Builders produce capabilities documents with `offline_services`,
`router.accepting_requests` and error bodies; FixtureRouter serves them over
HTTP, including /v1/router/events, and records every request's headers.
"""

import copy
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def metadata(canonical, *, context=163840, revision="weights1", quantization="Q8_0",
             aliases=(), nsfw=False, score=68.3, complete=True, available=True, **extra):
    return {
        "schema_version": 2,
        "alias": False,
        "aliases": list(aliases),
        "complete": complete,
        "health": {"available": available, "status": 200 if available else 503},
        "upstream_model": canonical,
        "context_window": context,
        "context_safety_reserve": 1024,
        "revision": revision,
        "quantization": quantization,
        "reasoning": {"default": "default", "efforts": {"default": "default", "off": "none", "low": "low"}},
        "nsfw": nsfw,
        "capability_score": {"value": score, "version": 1, "basis": "computed"},
        **extra,
    }


def model(canonical, service, *, available=True, context=163840, nsfw=False, score=68.3,
          aliases=None, **meta):
    if aliases is None:
        aliases = ["local-active", service] if service == "daytime" else [service]
    return {
        "id": canonical,
        "service": service,
        "display_name": canonical.title(),
        "aliases": aliases,
        "available": available,
        "slots": 1,
        "context_window": context,
        "input_modalities": ["text"],
        "capabilities": ["completion", "thinking"],
        "nsfw": nsfw,
        "capability_score": score,
        "metadata": metadata(canonical, context=context, aliases=aliases, nsfw=nsfw,
                             score=score, available=available, **meta),
    }


def offline(canonical, service="nighttime", reason="exclusive_configuration"):
    return {"model": canonical, "aliases": [service], "display_name": canonical.title(),
            "role": "everyday", "reason": reason}


def capabilities(models, *, offline_services=(), configuration="paired-test", exclusive=False,
                 accepting=True, drain_reason=None, maintenance=False, load=None):
    ids = {}
    for m in models:
        ids[m["id"]] = m["id"]
        for alias in m["aliases"]:
            ids[alias] = m["id"]
    doc = {
        "object": "router.capabilities",
        "schema_version": 1,
        "observed_at": "2026-10-07T05:11:23.261Z",
        "complete": True,
        "warnings": [],
        "router": {
            "name": "llm-router", "version": "0.1.0",
            "accepting_requests": accepting,
            "draining": not accepting and not maintenance,
            "drain_reason": drain_reason,
            "maintenance": maintenance,
        },
        "configuration": {"id": configuration, "exclusive": exclusive,
                          "runtime_revision": "runtime1", "published_at": "2026-10-07T05:11:23.146Z"},
        "default_model": models[0]["id"] if models else None,
        "models": models,
        "offline_services": list(offline_services),
        "ids": ids,
    }
    rehash(doc)
    doc["load"] = load if load is not None else {
        m["id"]: {"active": 0, "queued": 0, "free_slots": 1} for m in models
    }
    return doc


def rehash(doc):
    """`revision` is a content hash of everything except load."""
    content = {k: v for k, v in doc.items() if k not in ("revision", "load")}
    doc["revision"] = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()[:16]
    return doc


def service(canonical, container="container1", *, name="Daytime", image="image1", healthy=True,
            running=True, processing=False, started_at="2026-10-07T04:50:54Z", restart_count=0):
    return {"model": canonical, "name": name, "id": container, "image_id": image,
            "healthy": healthy, "running": running, "processing": processing,
            "started_at": started_at, "restart_count": restart_count, "differences": [],
            "gpu_names": ["GPU A"]}


def runtime(services, *, profile="paired-test", ready=True, draining=False):
    return {"ready": ready, "profile": profile, "deployed_revision": "runtime1",
            "maintenance": {"draining": draining, "active_requests": 0, "queued_requests": 0},
            "services": services, "csrf_token": "private-token"}


def snapshot(models, services, *, profile=None, **caps):
    configuration = caps.setdefault("configuration", "paired-test")
    return {"observed_at": "2026-10-07T06:00:00+00:00",
            "runtime": runtime(services, profile=profile or configuration),
            "capabilities": capabilities(models, **caps)}


def paired(*, night="night-a", night_container="night1", night_revision="nweights1", **caps):
    """Daytime and Nighttime both running."""
    return snapshot(
        [model("model-a", "daytime"),
         model(night, "nighttime", context=98304, nsfw=True, score=64.9, revision=night_revision)],
        [service("model-a"), service(night, night_container, name="Nighttime", image="image2")],
        **caps,
    )


def solo(*, night="night-a", **caps):
    """A solo configuration: Daytime only, Nighttime deliberately stopped."""
    caps.setdefault("configuration", "solo-test")
    caps.setdefault("exclusive", True)
    return snapshot(
        [model("model-a", "daytime", context=131072)],
        [service("model-a")],
        offline_services=[offline(night)],
        **caps,
    )


def draining(snap, reason="configuration switch"):
    """The same deployment while the router drains for a switch."""
    snap = copy.deepcopy(snap)
    router = snap["capabilities"]["router"]
    router.update(accepting_requests=False, draining=True, drain_reason=reason)
    for m in snap["capabilities"]["models"]:
        m["available"] = False
    rehash(snap["capabilities"])
    return snap


def error_body(code, message=None):
    return {"error": {"code": code, "message": message or code}}


def stream_body(text="ready", finish="stop", usage=(10, 2)):
    packets = [
        {"choices": [{"delta": {"content": text}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": finish}],
         "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]}},
    ]
    return "".join("data: " + json.dumps(p) + "\n\n" for p in packets) + "data: [DONE]\n\n"


class FixtureRouter:
    """Serves the router API and AI Runtime status from one local port.

    `responses` is a list consumed by POST /v1/chat/completions: each item is
    (status, body) where body is a dict (JSON) or str (SSE text). When empty, a
    successful stream is returned. `requests` records (method, path, headers).
    """

    def __init__(self, snap):
        self.snap = copy.deepcopy(snap)
        self.responses = []
        self.requests = []
        self.bodies = []
        self.changed = threading.Condition()
        self.closed = False
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def record(self):
                fixture.requests.append((self.command, self.path, dict(self.headers)))

            def send_json(self, status, value, headers=()):
                data = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in headers:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.record()
                if self.path.startswith("/api/status"):
                    return self.send_json(200, fixture.snap["runtime"])
                if self.path.startswith("/v1/router/capabilities"):
                    doc = copy.deepcopy(fixture.snap["capabilities"])
                    if "include=load" not in self.path:
                        doc.pop("load", None)
                    etag = '"' + doc["revision"] + '"'
                    if self.headers.get("If-None-Match") == etag:
                        self.send_response(304)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    return self.send_json(200, doc, [("ETag", etag)])
                if self.path.startswith("/v1/router/events"):
                    return self.events()
                self.send_json(404, error_body("MODEL_NOT_FOUND"))

            def events(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                sent = None
                try:
                    self.wfile.write(b"retry: 3000\n\n")
                    while not fixture.closed:
                        doc = copy.deepcopy(fixture.snap["capabilities"])
                        doc.pop("load", None)
                        if doc["revision"] != sent:
                            sent = doc["revision"]
                            self.wfile.write(
                                f"event: capabilities\nid: {sent}\ndata: {json.dumps(doc)}\n\n".encode()
                            )
                        else:
                            self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        with fixture.changed:
                            fixture.changed.wait(0.2)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                self.close_connection = True

            def do_POST(self):
                self.record()
                fixture.bodies.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0))))
                status, body = fixture.responses.pop(0) if fixture.responses else (200, stream_body())
                if isinstance(body, dict):
                    return self.send_json(status, body)
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    @property
    def settings(self):
        return {"endpoint": self.base + "/v1", "runtime_url": self.base + "/api/status"}

    def publish(self, snap):
        """Replace the deployment and push it to event-stream subscribers."""
        self.snap = copy.deepcopy(snap)
        with self.changed:
            self.changed.notify_all()

    def headers(self, path_prefix, name="X-Client-Name"):
        return [h.get(name) for _, path, h in self.requests if path.startswith(path_prefix)]

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.closed = True
        with self.changed:
            self.changed.notify_all()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
