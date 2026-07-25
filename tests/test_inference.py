from __future__ import annotations

import json
import unittest
import urllib.request
from typing import cast

from rpi_streamer.inference import (
    SCHEMA_VERSION,
    InferenceError,
    OpenAIInferenceClient,
)


def response_payload(structured: object, *, status: str = "completed") -> bytes:
    return json.dumps(
        {
            "status": status,
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(structured),
                        }
                    ],
                }
            ],
        }
    ).encode()


def result(
    *,
    title: str | None = "Okinawa de Suki ni Natta Ko ga Hougen Sugite Tsurasugiru",
    filename: str = "release_show_01.mp4",
    hint: str | None = "E1",
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "title_hint": title,
        "confidence": 0.97,
        "reason": "Normalized a truncated romanized title.",
        "episodes": [
            {"filename": filename, "hint": hint, "confidence": 0.95}
        ],
    }


class OpenAIInferenceTests(unittest.TestCase):
    def test_uses_strict_schema_minimal_input_and_never_sends_key_in_body(
        self,
    ) -> None:
        captured: list[urllib.request.Request] = []

        def transport(request: urllib.request.Request, _timeout: float) -> bytes:
            captured.append(request)
            return response_payload(result())

        client = OpenAIInferenceClient(
            "secret-key", max_calls=1, transport=transport
        )
        inferred = client.infer("Okinawa title", ["release_show_01.mp4"])

        self.assertEqual(inferred.episodes[0].hint, "E1")
        request = captured[0]
        body = json.loads(cast(bytes, request.data).decode())
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertFalse(body["store"])
        self.assertNotIn("secret-key", json.dumps(body))
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-key")
        with self.assertRaisesRegex(InferenceError, "call limit"):
            client.infer("again", [])

    def test_rejects_bad_filename_hint_and_incomplete_response(self) -> None:
        payloads = [
            result(filename="not-submitted.mp4"),
            result(hint="1080P"),
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                def transport(
                    _request: urllib.request.Request,
                    _timeout: float,
                    value: object = payload,
                ) -> bytes:
                    return response_payload(value)

                client = OpenAIInferenceClient("key", transport=transport)
                with self.assertRaises(InferenceError):
                    client.infer("title", ["release_show_01.mp4"])

        client = OpenAIInferenceClient(
            "key",
            transport=lambda _request, _timeout: response_payload(
                result(), status="incomplete"
            ),
        )
        with self.assertRaisesRegex(InferenceError, "not completed"):
            client.infer("title", ["release_show_01.mp4"])

    def test_bounds_private_local_input(self) -> None:
        client = OpenAIInferenceClient(
            "key", transport=lambda _request, _timeout: response_payload(result())
        )
        with self.assertRaisesRegex(InferenceError, "title"):
            client.infer("x" * 301, [])
        with self.assertRaisesRegex(InferenceError, "too many"):
            client.infer("title", [f"{number}.mp4" for number in range(51)])
