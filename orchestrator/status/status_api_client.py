"""Signed HTTP requests to the dashboard API."""

from __future__ import annotations

import json
import logging
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, List, Mapping
from urllib.parse import urlsplit

from orchestrator import __version__
from orchestrator.errors import OrchestratorError
from orchestrator.status.request_signer import RESPONSE_SIGNATURE_HEADER, RequestSigner

#: Largest response body read from the API. Its answers are tiny; anything
#: bigger is not an answer worth trusting.
MAX_RESPONSE_BYTES = 1024 * 1024


class StatusApiUnavailable(OrchestratorError):
    """The API could not be reached at all (refused, timed out, DNS...)."""


class StatusApiRejected(OrchestratorError):
    """The API answered, but not with success."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class ApiResponse:
    status: int
    body: bytes
    signature: str | None
    nonce: str


class StatusApiClient:
    """Posts reports and polls for commands, every request signed.

    Proxy environment variables are deliberately ignored: the API sits on the
    LAN, and a stray ``http_proxy`` in a systemd environment would otherwise
    send signed requests -- and container logs -- through a third party.
    """

    def __init__(
        self,
        api_url: str,
        signer: RequestSigner,
        timeout_seconds: float = 10.0,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        parts = urlsplit(api_url)
        self._origin = f"{parts.scheme}://{parts.netloc}"
        self._base_path = parts.path.rstrip("/") + "/api/agent/v1"
        self._signer = signer
        self._timeout = timeout_seconds
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._log = logging.getLogger(self.__class__.__name__)

    # ---------------------------------------------------------------- reports

    def send_report(self, method: str, endpoint: str, payload: Mapping[str, Any]) -> None:
        """POST/PUT one report; raises unless the API accepted it."""
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        response = self._send(method, endpoint, body, self._timeout)
        if response.status not in (200, 201, 202, 204):
            raise StatusApiRejected(response.status, self._problem(response.body))

    # --------------------------------------------------------------- commands

    def poll_commands(self, wait_seconds: int) -> List[dict]:
        """Long-poll for commands. Verifies the API's signature before trusting any.

        A response with a missing or wrong signature is treated as an attack
        or a misconfiguration -- never as instructions.
        """
        response = self._send(
            "GET", f"/commands?wait={int(wait_seconds)}", None, wait_seconds + self._timeout
        )
        if response.status != 200:
            raise StatusApiRejected(response.status, self._problem(response.body))

        if not self._signer.verify_response(response.nonce, response.status, response.body, response.signature):
            raise StatusApiRejected(response.status, "the command response is not correctly signed")

        try:
            parsed = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise StatusApiRejected(response.status, f"the command response is not JSON: {exc}") from exc

        commands = parsed.get("commands") if isinstance(parsed, dict) else None
        if not isinstance(commands, list):
            raise StatusApiRejected(response.status, "the command response has no command list")
        return [command for command in commands if isinstance(command, dict)]

    def send_command_result(self, command_id: str, result: Mapping[str, Any]) -> bool:
        """Post a command's result. False when nobody is waiting for it any more."""
        body = json.dumps(result, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        response = self._send("POST", f"/commands/{command_id}/result", body, self._timeout)
        if response.status == 404:
            return False
        if response.status not in (200, 202, 204):
            raise StatusApiRejected(response.status, self._problem(response.body))
        return True

    # -------------------------------------------------------------- transport

    def _send(self, method: str, endpoint: str, body: bytes | None, timeout: float) -> ApiResponse:
        request = urllib.request.Request(
            self._origin + self._base_path + endpoint,
            data=body,
            method=method,
            headers={
                "Accept": "application/json",
                "User-Agent": f"docker-orchestrator/{__version__}",
            },
        )
        if body is not None:
            request.add_header("Content-Type", "application/json; charset=utf-8")

        # The selector is the target exactly as urllib will put it on the wire,
        # which is what the API reconstructs to check the signature.
        signed = self._signer.sign(method, request.selector, body or b"")
        for name, value in signed.headers.items():
            request.add_header(name, value)

        try:
            with self._opener.open(request, timeout=timeout) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
                return ApiResponse(
                    response.status, payload[:MAX_RESPONSE_BYTES],
                    response.headers.get(RESPONSE_SIGNATURE_HEADER), signed.nonce,
                )
        except urllib.error.HTTPError as exc:
            payload = exc.read(MAX_RESPONSE_BYTES) if exc.fp is not None else b""
            return ApiResponse(exc.code, payload, exc.headers.get(RESPONSE_SIGNATURE_HEADER) if exc.headers else None, signed.nonce)
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise StatusApiUnavailable(f"{self._origin} is unreachable: {reason}") from exc

    @staticmethod
    def _problem(body: bytes) -> str:
        """The API's ProblemDetails title/detail, or the start of the body."""
        try:
            parsed = json.loads(body.decode("utf-8"))
            if isinstance(parsed, dict):
                parts = [str(parsed[k]) for k in ("title", "detail") if parsed.get(k)]
                errors = parsed.get("errors")
                if isinstance(errors, dict):
                    parts.extend(f"{field}: {'; '.join(map(str, msgs))}" for field, msgs in list(errors.items())[:5])
                if parts:
                    return " | ".join(parts)
        except (UnicodeDecodeError, ValueError):
            pass
        return body[:300].decode("utf-8", "replace") or "(empty response)"
