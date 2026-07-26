from __future__ import annotations

import json
import unittest
import urllib.request
from typing import cast

from rpi_streamer.inference import (
    MAPPING_SCHEMA_VERSION,
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
        "episodes": [{"filename": filename, "hint": hint, "confidence": 0.95}],
    }


def mapping_result(
    *,
    filename: str = "Show_Bonus.mp4",
    mal_id: str | None = "2",
    confidence: float = 0.95,
) -> dict[str, object]:
    return {
        "schema_version": MAPPING_SCHEMA_VERSION,
        "mappings": [
            {
                "filename": filename,
                "mal_id": mal_id,
                "kind": "special",
                "episode_start": None,
                "episode_end": None,
                "confidence": confidence,
                "reason": "bonus tie-in",
            }
        ],
    }


def mapping_files() -> list[dict[str, object]]:
    return [
        {
            "filename": "Show_Bonus.mp4",
            "season": None,
            "episode_start": None,
            "episode_end": None,
            "special_kind": None,
            "explicit_ordinal": None,
            "markers": ["show", "bonus"],
        }
    ]


def mapping_candidates() -> list[dict[str, object]]:
    return [
        {
            "mal_id": "2",
            "title": "Show OVA",
            "media_type": "OVA",
            "episode_count": 1,
            "relation_type": "side story",
            "relation_distance": 1,
            "order": 1,
        }
    ]


class OpenAIInferenceTests(unittest.TestCase):
    def test_uses_strict_schema_minimal_input_and_never_sends_key_in_body(
        self,
    ) -> None:
        captured: list[urllib.request.Request] = []

        def transport(request: urllib.request.Request, _timeout: float) -> bytes:
            captured.append(request)
            return response_payload(result())

        client = OpenAIInferenceClient("secret-key", max_calls=1, transport=transport)
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

    def test_multi_work_request_is_strict_minimal_and_candidate_bounded(self) -> None:
        captured: list[urllib.request.Request] = []

        def transport(request: urllib.request.Request, _timeout: float) -> bytes:
            captured.append(request)
            return response_payload(mapping_result())

        client = OpenAIInferenceClient("secret-key", transport=transport)
        inferred = client.infer_multi_work(
            "Show", "Show", mapping_files(), mapping_candidates()
        )

        self.assertEqual(inferred.mappings[0].mal_id, "2")
        body = json.loads(cast(bytes, captured[0].data).decode())
        schema = body["text"]["format"]["schema"]
        mal_schema = schema["properties"]["mappings"]["items"]["properties"]["mal_id"]
        self.assertEqual(mal_schema["anyOf"][0]["enum"], ["2"])
        self.assertTrue(body["text"]["format"]["strict"])
        serialized = json.dumps(body)
        self.assertNotIn("secret-key", serialized)
        self.assertNotIn("synopsis", serialized.casefold())
        self.assertNotIn("/mnt/", serialized)
        self.assertNotIn("api_key", serialized)

    def test_multi_work_rejects_incomplete_invented_and_duplicate_output(self) -> None:
        duplicate_entry = {
            "filename": "Show_Bonus.mp4",
            "mal_id": "2",
            "kind": "special",
            "episode_start": None,
            "episode_end": None,
            "confidence": 0.95,
            "reason": "duplicate",
        }
        invalid = [
            {"schema_version": MAPPING_SCHEMA_VERSION, "mappings": []},
            mapping_result(mal_id="999"),
            {
                "schema_version": MAPPING_SCHEMA_VERSION,
                "mappings": [duplicate_entry, duplicate_entry],
            },
            mapping_result(filename="unknown.mp4"),
        ]
        for payload in invalid:
            with self.subTest(payload=payload):

                def transport(
                    _request: urllib.request.Request,
                    _timeout: float,
                    value: object = payload,
                ) -> bytes:
                    return response_payload(value)

                client = OpenAIInferenceClient(
                    "key",
                    transport=transport,
                )
                with self.assertRaises(InferenceError):
                    client.infer_multi_work(
                        "Show", "Show", mapping_files(), mapping_candidates()
                    )

    def test_multi_work_shares_the_existing_per_scan_call_budget(self) -> None:
        client = OpenAIInferenceClient(
            "key",
            max_calls=1,
            transport=lambda _request, _timeout: response_payload(
                result(filename="Show_01.mp4")
            ),
        )
        client.infer("Show", ["Show_01.mp4"])

        with self.assertRaisesRegex(InferenceError, "call limit"):
            client.infer_multi_work(
                "Show", "Show", mapping_files(), mapping_candidates()
            )
