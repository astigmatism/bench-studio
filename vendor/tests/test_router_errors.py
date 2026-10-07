"""Bench Studio additions: LLM Router error codes and client identification.

The router reports failures as `{"error": {"code": ...}}` (or an incomplete
stream frame); BetterBench keeps the code on the result so Bench Studio can
classify it. An error frame inside a stream never yields a completed sample.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from betterbench.client import _error_code, stream_chat_sync


def _router(status, body, seen):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            seen.append(dict(self.headers))
            self.rfile.read(int(self.headers["Content-Length"]))
            data = (json.dumps(body) if isinstance(body, dict) else body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json" if isinstance(body, dict)
                             else "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def router():
    servers = []

    def start(status, body, seen):
        srv = _router(status, body, seen)
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_port}/v1"

    yield start
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def _chat(url):
    return stream_chat_sync(url, "pinned-model", [{"role": "user", "content": "hi"}],
                            max_tokens=4, temperature=0.0)


@pytest.mark.parametrize("code", ["SERVICE_OFFLINE", "BACKEND_DRAINING", "MODEL_NOT_FOUND"])
def test_rejection_keeps_the_router_error_code(router, code):
    r = _chat(router(503, {"error": {"code": code, "message": "no"}}, []))
    assert not r.ok and r.error_code == code
    assert r.error.startswith("HTTP 503")


def test_error_frame_inside_a_stream_is_never_a_completed_sample(router):
    frame = {"error": {"code": "BACKEND_UNAVAILABLE", "message": "stopped"},
             "x_router": {"status": "incomplete", "stop_reason": "BACKEND_UNAVAILABLE"}}
    body = ('data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}\n\n'
            "data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n")
    r = _chat(router(200, body, []))
    assert not r.ok
    assert r.error_code == "BACKEND_UNAVAILABLE" and r.error.startswith("stream error")


def test_error_code_tolerates_string_errors_and_reads_stop_reason():
    assert _error_code(b'{"error": "legacy"}') is None
    assert _error_code("not json") is None
    assert _error_code({"error": "x", "x_router": {"stop_reason": "MAINTENANCE_MODE"}}) == "MAINTENANCE_MODE"


def test_client_name_header_is_sent_when_configured(router, monkeypatch):
    seen = []
    url = router(503, {"error": {"code": "SERVICE_OFFLINE"}}, seen)
    _chat(url)
    monkeypatch.setenv("BETTERBENCH_CLIENT_NAME", "bench-studio/run-1")
    _chat(url)
    assert "X-Client-Name" not in seen[0]
    assert seen[1]["X-Client-Name"] == "bench-studio/run-1"
