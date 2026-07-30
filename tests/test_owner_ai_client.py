import io
import json
import unittest
import urllib.error

from owner_ai_client import (
    OwnerAiAccessError,
    OwnerAiClientConfig,
    OwnerAiClientError,
    query_owner_ai,
)


class FakeResponse:
    def __init__(self, payload, *, status=200):
        self.status = status
        self.body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, limit):
        return self.body[:limit]


class OwnerAiClientTests(unittest.TestCase):
    def setUp(self):
        self.config = OwnerAiClientConfig(
            url="https://example.test/internal/owner-ai/v1/query",
            token="internal-" + ("x" * 32),
            timeout_seconds=5,
        )

    def test_query_sends_versioned_bounded_request(self):
        captured = {}

        def urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse(
                {
                    "version": "1",
                    "scope": "staff",
                    "answer": "Сегодня 12 продаж.",
                }
            )

        result = query_owner_ai(
            self.config,
            user_id=874403512,
            question="  Продажи   сегодня? ",
            conversation_id="telegram:-100123:874403512",
            maintenance_context={
                "points": [
                    {
                        "point_name": "Сити",
                        "service_dates": ["2026-07-26", "2026-07-24"],
                    }
                ]
            },
            urlopen=urlopen,
        )

        self.assertEqual(
            result,
            {"scope": "staff", "answer": "Сегодня 12 продаж."},
        )
        self.assertEqual(captured["timeout"], 5)
        request = captured["request"]
        self.assertEqual(request.full_url, self.config.url)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(
            request.get_header("Authorization"),
            f"Bearer {self.config.token}",
        )
        self.assertEqual(
            json.loads(request.data),
            {
                "version": "1",
                "user_id": 874403512,
                "question": "Продажи сегодня?",
                "conversation_id": "telegram:-100123:874403512",
                "maintenance_context": {
                    "points": [
                        {
                            "point_name": "Сити",
                            "service_dates": ["2026-07-26", "2026-07-24"],
                        }
                    ]
                },
            },
        )

    def test_invalid_conversation_id_is_rejected_locally(self):
        with self.assertRaises(OwnerAiClientError):
            query_owner_ai(
                self.config,
                user_id=123,
                question="Продажи сегодня",
                conversation_id="telegram:bad conversation",
            )

    def test_forbidden_user_has_distinct_safe_error(self):
        def urlopen(_request, timeout=None):
            raise urllib.error.HTTPError(
                self.config.url,
                403,
                "Forbidden",
                {},
                io.BytesIO(b"{}"),
            )

        with self.assertRaises(OwnerAiAccessError):
            query_owner_ai(
                self.config,
                user_id=123,
                question="Продажи сегодня",
                urlopen=urlopen,
            )

    def test_invalid_or_oversized_response_is_rejected(self):
        def wrong_version(_request, timeout=None):
            return FakeResponse(
                {
                    "version": "2",
                    "scope": "owner",
                    "answer": "Нет",
                }
            )

        with self.assertRaises(OwnerAiClientError):
            query_owner_ai(
                self.config,
                user_id=123,
                question="Продажи сегодня",
                urlopen=wrong_version,
            )

        small_config = OwnerAiClientConfig(
            url=self.config.url,
            token=self.config.token,
            max_response_bytes=10,
        )

        def oversized(_request, timeout=None):
            response = FakeResponse(
                {
                    "version": "1",
                    "scope": "owner",
                    "answer": "Очень длинный ответ",
                }
            )
            response.read = lambda limit: b"x" * limit
            return response

        with self.assertRaises(OwnerAiClientError):
            query_owner_ai(
                small_config,
                user_id=123,
                question="Продажи сегодня",
                urlopen=oversized,
            )

    def test_config_requires_https_and_separate_long_token(self):
        with self.assertRaises(ValueError):
            OwnerAiClientConfig(
                url="http://example.test/query",
                token=self.config.token,
            )
        with self.assertRaises(ValueError):
            OwnerAiClientConfig(
                url=self.config.url,
                token="short",
            )


if __name__ == "__main__":
    unittest.main()
