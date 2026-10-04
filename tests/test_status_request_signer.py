"""The signature scheme shared with the dashboard API."""

from __future__ import annotations

import unittest

from orchestrator.status.request_signer import (
    NONCE_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    RequestSigner,
)
from tests.status_support import (
    KEY,
    NONCE,
    REQUEST_BODY,
    REQUEST_SIGNATURE,
    RESPONSE_BODY,
    RESPONSE_SIGNATURE,
    TIMESTAMP,
)


class RequestSignerTests(unittest.TestCase):
    def test_canonical_request_has_the_documented_shape(self) -> None:
        canonical = RequestSigner.canonical_request(
            "put", "/api/agent/v1/containers", TIMESTAMP, NONCE, REQUEST_BODY
        )

        self.assertEqual(
            "v1\nPUT\n/api/agent/v1/containers\n1790000000\n00112233445566778899aabbccddeeff\n"
            "487ddea30211eb7dbdb01ab2937ac2642f3ef17649e2fe516f229c6bbda5b569",
            canonical,
        )

    def test_request_signature_matches_the_vector_shared_with_the_api(self) -> None:
        signer = RequestSigner(KEY, clock=lambda: TIMESTAMP, nonce_factory=lambda: NONCE)

        signed = signer.sign("PUT", "/api/agent/v1/containers", REQUEST_BODY)

        self.assertEqual(REQUEST_SIGNATURE, signed.headers[SIGNATURE_HEADER])
        self.assertEqual(str(TIMESTAMP), signed.headers[TIMESTAMP_HEADER])
        self.assertEqual(NONCE, signed.headers[NONCE_HEADER])
        self.assertEqual(NONCE, signed.nonce)

    def test_response_signature_matches_the_vector_shared_with_the_api(self) -> None:
        signer = RequestSigner(KEY)

        self.assertTrue(signer.verify_response(NONCE, 200, RESPONSE_BODY, RESPONSE_SIGNATURE))

    def test_a_response_for_another_request_or_with_another_body_is_rejected(self) -> None:
        signer = RequestSigner(KEY)

        self.assertFalse(signer.verify_response("ff" * 16, 200, RESPONSE_BODY, RESPONSE_SIGNATURE))
        self.assertFalse(signer.verify_response(NONCE, 200, b'{"commands":[{}]}', RESPONSE_SIGNATURE))
        self.assertFalse(signer.verify_response(NONCE, 204, RESPONSE_BODY, RESPONSE_SIGNATURE))
        self.assertFalse(signer.verify_response(NONCE, 200, RESPONSE_BODY, None))
        self.assertFalse(signer.verify_response(NONCE, 200, RESPONSE_BODY, "garbage"))

    def test_another_key_does_not_verify(self) -> None:
        self.assertFalse(RequestSigner(bytes(32)).verify_response(NONCE, 200, RESPONSE_BODY, RESPONSE_SIGNATURE))

    def test_every_request_gets_a_fresh_nonce(self) -> None:
        signer = RequestSigner(KEY)

        first = signer.sign("GET", "/x", b"")
        second = signer.sign("GET", "/x", b"")

        self.assertNotEqual(first.nonce, second.nonce)
        self.assertRegex(first.nonce, r"^[0-9a-f]{32}$")


if __name__ == "__main__":
    unittest.main()
