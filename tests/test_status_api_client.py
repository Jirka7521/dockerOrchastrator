"""StatusApiClient against a real local HTTP server that checks signatures."""

from __future__ import annotations

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import List

from orchestrator.status.request_signer import (
    NONCE_HEADER,
    RESPONSE_SIGNATURE_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    RequestSigner,
)
from orchestrator.status.status_api_client import StatusApiClient, StatusApiRejected, StatusApiUnavailable
from tests.status_support import KEY


class FakeApi(BaseHTTPRequestHandler):
    """Verifies every request the way the C# API does, and signs responses."""

    received: List[tuple] = []
    polls: List[str] = []
    sign_responses = True
    commands: List[dict] = []

    def log_message(self, format, *args):  # noqa: A002 - silence the test output
        pass

    def _verify(self, body: bytes) -> bool:
        canonical = RequestSigner.canonical_request(
            self.command, self.path, int(self.headers[TIMESTAMP_HEADER]), self.headers[NONCE_HEADER], body
        )
        return RequestSigner(KEY).signature(canonical) == self.headers[SIGNATURE_HEADER]

    def _answer(self, status: int, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if self.sign_responses:
            signature = RequestSigner(KEY).signature(
                RequestSigner.canonical_response(self.headers[NONCE_HEADER], status, payload)
            )
            self.send_header(RESPONSE_SIGNATURE_HEADER, signature)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if not self._verify(body):
            return self._answer(401, b"")
        FakeApi.received.append((self.command, self.path, json.loads(body)))
        if self.path.endswith("/result"):
            return self._answer(404 if "unknown" in self.path else 202, b"")
        if "reject" in self.path:
            return self._answer(400, b'{"title":"One or more validation errors occurred.","errors":{"Name":["bad"]}}')
        return self._answer(202, b'{"stored":1}')

    do_PUT = do_POST

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        if not self._verify(b""):
            return self._answer(401, b"")
        FakeApi.polls.append(self.path)
        return self._answer(200, json.dumps({"commands": FakeApi.commands}).encode())


class StatusApiClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeApi)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        FakeApi.received = []
        FakeApi.polls = []
        FakeApi.sign_responses = True
        FakeApi.commands = []
        self.client = StatusApiClient(self.url, RequestSigner(KEY), timeout_seconds=5)

    def test_reports_are_signed_and_accepted(self) -> None:
        self.client.send_report("PUT", "/containers", {"collectedAt": "2026-10-03T12:00:00Z", "containers": []})

        self.assertEqual([("PUT", "/api/agent/v1/containers", {"collectedAt": "2026-10-03T12:00:00Z", "containers": []})], FakeApi.received)

    def test_a_wrong_key_is_rejected_by_the_server(self) -> None:
        client = StatusApiClient(self.url, RequestSigner(bytes(32)), timeout_seconds=5)

        with self.assertRaises(StatusApiRejected) as caught:
            client.send_report("POST", "/host", {})
        self.assertEqual(401, caught.exception.status)

    def test_validation_problems_are_reported_readably(self) -> None:
        with self.assertRaises(StatusApiRejected) as caught:
            self.client.send_report("POST", "/reject", {})

        self.assertEqual(400, caught.exception.status)
        self.assertIn("Name: bad", caught.exception.detail)

    def test_signed_commands_are_returned(self) -> None:
        FakeApi.commands = [{"id": "a" * 32, "type": "logs", "container": "temp-fe", "tail": 10}]

        commands = self.client.poll_commands(0)

        self.assertEqual(FakeApi.commands, commands)

    def test_the_poll_says_what_this_host_runs_inside_the_signed_target(self) -> None:
        self.client.poll_commands(0, ("logs", "restart"))
        self.client.poll_commands(0, ())

        # The fake API checked both signatures over exactly these targets.
        self.assertEqual(
            ["/api/agent/v1/commands?wait=0&accept=logs,restart", "/api/agent/v1/commands?wait=0&accept=none"],
            FakeApi.polls,
        )

    def test_unsigned_commands_are_never_trusted(self) -> None:
        FakeApi.sign_responses = False
        FakeApi.commands = [{"id": "a" * 32, "type": "logs", "container": "temp-fe"}]

        with self.assertRaisesRegex(StatusApiRejected, "not correctly signed"):
            self.client.poll_commands(0)

    def test_results_for_abandoned_commands_are_not_an_error(self) -> None:
        self.assertTrue(self.client.send_command_result("b" * 32, {"ok": True, "lines": []}))
        self.assertFalse(self.client.send_command_result("unknown", {"ok": True, "lines": []}))

    def test_an_unreachable_api_raises_unavailable(self) -> None:
        client = StatusApiClient("http://127.0.0.1:9", RequestSigner(KEY), timeout_seconds=1)

        started = time.monotonic()
        with self.assertRaises(StatusApiUnavailable):
            client.send_report("POST", "/host", {})
        self.assertLess(time.monotonic() - started, 5)


if __name__ == "__main__":
    unittest.main()
