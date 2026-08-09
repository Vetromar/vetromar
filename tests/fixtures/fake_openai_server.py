"""A scripted OpenAI-compatible HTTP server for wire-level provider tests.

The real `openai` SDK points at this server, so the provider's request
translation (tools, response_format negotiation, auth headers) is asserted at
the wire — not against monkeypatched SDK objects (fakes that aren't shaped
like the real thing hide bugs).

Import from tests via a sys.path insert of this directory (the tests package
has no __init__ chain): see test_providers.py.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def completion(content: str | None = None, tool_calls=None, finish_reason: str | None = None):
    """A chat-completions response payload. `tool_calls` is a list of
    (id, name, arguments_json_str) triples."""
    message: dict = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = [
            {"id": id_, "type": "function", "function": {"name": name, "arguments": args}}
            for id_, name, args in tool_calls
        ]
    if finish_reason is None:
        finish_reason = "tool_calls" if tool_calls else "stop"
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "created": 0,
        "model": "fake-model",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class FakeOpenAIServer:
    """Context manager serving /v1/chat/completions from a script (payload
    dicts or callables of the request body) and /v1/models. Behavior flags
    model the server variance the provider must negotiate around."""

    def __init__(
        self,
        script=None,
        *,
        reject_json_schema: bool = False,
        reject_json_object: bool = False,
        reject_max_tokens: bool = False,
        require_key: str | None = None,
        serve_models: bool = True,
    ):
        self.script = list(script or [])
        self.requests: list[dict] = []  # parsed /chat/completions bodies, in order
        self.reject_json_schema = reject_json_schema
        self.reject_json_object = reject_json_object
        self.reject_max_tokens = reject_max_tokens
        self.require_key = require_key
        self.serve_models = serve_models
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status: int, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _auth_ok(self) -> bool:
                if server.require_key is None:
                    return True
                return self.headers.get("Authorization") == f"Bearer {server.require_key}"

            def do_GET(self):
                if self.path.endswith("/models"):
                    if not self._auth_ok():
                        self._send(401, _err("Incorrect API key provided"))
                        return
                    if not server.serve_models:
                        self._send(404, _err("Not found"))
                        return
                    self._send(200, {"object": "list", "data": [{"id": "fake-model", "object": "model"}]})
                    return
                self._send(404, _err("Not found"))

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                if not self.path.endswith("/chat/completions"):
                    self._send(404, _err("Not found"))
                    return
                if not self._auth_ok():
                    self._send(401, _err("Incorrect API key provided"))
                    return
                server.requests.append(body)
                fmt = (body.get("response_format") or {}).get("type")
                if fmt == "json_schema" and server.reject_json_schema:
                    self._send(400, _err("response_format 'json_schema' is not supported"))
                    return
                if fmt == "json_object" and server.reject_json_object:
                    self._send(400, _err("response_format 'json_object' is not supported"))
                    return
                if server.reject_max_tokens and "max_tokens" in body:
                    self._send(400, _err(
                        "Unsupported parameter: 'max_tokens' is not supported with "
                        "this model. Use 'max_completion_tokens' instead."
                    ))
                    return
                if not server.script:
                    # 400: the SDK retries 5xx, which would hide a scripting bug.
                    self._send(400, _err("fake server script exhausted"))
                    return
                step = server.script.pop(0)
                if callable(step):
                    step = step(body)
                self._send(200, step)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address
        return f"http://{host}:{port}/v1"

    def __enter__(self) -> "FakeOpenAIServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def _err(message: str) -> dict:
    return {"error": {"message": message, "type": "invalid_request_error"}}
