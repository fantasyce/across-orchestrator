from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import json
import unittest

from across_orchestrator.a2a_client import A2AAgentClient, A2AClientError


class _Agent(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self._send({"name": "Fixture Agent", "url": self.server.endpoint, "capabilities": {"streaming": True}})

    def do_POST(self):
        size = int(self.headers.get("content-length") or 0)
        payload = json.loads(self.rfile.read(size))
        method = payload["method"]
        task_id = payload["params"]["id"]
        if method == "tasks/sendSubscribe":
            rows = [
                {"jsonrpc": "2.0", "result": {"taskId": task_id, "state": "working"}},
                {"jsonrpc": "2.0", "result": {"taskId": task_id, "artifact": {"artifactId": "report", "parts": [{"kind": "text", "text": "ok"}]}}},
                {"jsonrpc": "2.0", "result": {"taskId": task_id, "state": "completed"}},
            ]
            raw = b"".join(f"data: {json.dumps(row)}\n\n".encode() for row in rows)
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if method == "tasks/cancel":
            self._send({"jsonrpc": "2.0", "id": payload["id"], "result": {"id": task_id, "state": "canceled"}})
            return
        self._send({"jsonrpc": "2.0", "id": payload["id"], "result": {"id": task_id, "state": "submitted"}})

    def _send(self, value):
        raw = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class A2AClientTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Agent)
        self.server.endpoint = f"http://127.0.0.1:{self.server.server_address[1]}/a2a"
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_agent_card_task_stream_artifact_and_cancel(self):
        client = A2AAgentClient(self.server.endpoint)
        self.assertEqual(client.agent_card()["name"], "Fixture Agent")
        self.assertEqual(client.send_task(task_id="task-1", text="review")["state"], "submitted")
        streamed = client.stream_task(task_id="task-1", text="review")
        self.assertEqual(streamed.state, "completed")
        self.assertEqual(streamed.artifacts[0]["artifactId"], "report")
        self.assertEqual(client.cancel_task("task-2")["state"], "canceled")

    def test_plain_http_remote_agent_is_rejected(self):
        with self.assertRaises(A2AClientError):
            A2AAgentClient("http://agent.example/a2a")


if __name__ == "__main__":
    unittest.main()
