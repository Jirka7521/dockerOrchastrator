"""HMAC-SHA256 request signing, shared with the dashboard API."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Callable, Dict

TIMESTAMP_HEADER = "X-Agent-Timestamp"
NONCE_HEADER = "X-Agent-Nonce"
SIGNATURE_HEADER = "X-Agent-Signature"
RESPONSE_SIGNATURE_HEADER = "X-Api-Signature"

_PREFIX = "v1="


@dataclass(frozen=True)
class SignedHeaders:
    """The three signature headers plus the nonce, needed again to verify the response."""

    headers: Dict[str, str]
    nonce: str


class RequestSigner:
    """Signs requests and verifies responses -- see docs/AGENT_PROTOCOL.md.

    The canonical forms below are the contract with the API's C#
    implementation (``AgentSignature``). Both test suites assert the same
    fixed vectors, so a change here that is not mirrored there fails a test.
    """

    def __init__(
        self,
        key: bytes,
        clock: Callable[[], float] = time.time,
        nonce_factory: Callable[[], str] = lambda: secrets.token_hex(16),
    ) -> None:
        self._key = bytes(key)
        self._clock = clock
        self._nonce_factory = nonce_factory

    # ---------------------------------------------------------------- forms

    @staticmethod
    def canonical_request(method: str, target: str, timestamp: int, nonce: str, body: bytes) -> str:
        return "\n".join(
            (
                "v1",
                method.upper(),
                target,
                str(int(timestamp)),
                nonce,
                hashlib.sha256(body).hexdigest(),
            )
        )

    @staticmethod
    def canonical_response(nonce: str, status: int, body: bytes) -> str:
        return "\n".join(("v1-response", nonce, str(int(status)), hashlib.sha256(body).hexdigest()))

    # -------------------------------------------------------------- signing

    def signature(self, canonical: str) -> str:
        mac = hmac.new(self._key, canonical.encode("utf-8"), hashlib.sha256).digest()
        return _PREFIX + base64.b64encode(mac).decode("ascii")

    def sign(self, method: str, target: str, body: bytes) -> SignedHeaders:
        """Headers for one request. Every call uses a fresh nonce."""
        timestamp = int(self._clock())
        nonce = self._nonce_factory()
        canonical = self.canonical_request(method, target, timestamp, nonce, body)
        return SignedHeaders(
            headers={
                TIMESTAMP_HEADER: str(timestamp),
                NONCE_HEADER: nonce,
                SIGNATURE_HEADER: self.signature(canonical),
            },
            nonce=nonce,
        )

    def verify_response(self, nonce: str, status: int, body: bytes, presented: str | None) -> bool:
        """True when the API signed exactly this response to exactly this request.

        Constant-time comparison; a missing or malformed header is simply false.
        """
        if not presented or not presented.startswith(_PREFIX):
            return False
        expected = self.signature(self.canonical_response(nonce, status, body))
        return hmac.compare_digest(expected.encode("ascii"), presented.encode("ascii", "replace"))
