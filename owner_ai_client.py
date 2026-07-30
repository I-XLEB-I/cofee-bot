"""Bounded client for the private read-only owner/staff AI API."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit


class OwnerAiClientError(RuntimeError):
    """The internal AI service could not return a safe answer."""


class OwnerAiAccessError(OwnerAiClientError):
    """The current employee is not allowlisted by the AI service."""


@dataclass(frozen=True, slots=True)
class OwnerAiClientConfig:
    url: str
    token: str
    timeout_seconds: float = 25.0
    max_question_chars: int = 1_200
    max_response_bytes: int = 65_536
    max_answer_chars: int = 4_000

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url.strip())
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Owner AI URL must be an absolute HTTPS URL.")
        if len(self.token.strip()) < 32:
            raise ValueError("Owner AI token must contain at least 32 characters.")
        if not 1 <= self.timeout_seconds <= 120:
            raise ValueError("Owner AI timeout must be between 1 and 120 seconds.")


UrlOpen = Callable[..., Any]


def query_owner_ai(
    config: OwnerAiClientConfig,
    *,
    user_id: int,
    question: str,
    conversation_id: str | None = None,
    urlopen: UrlOpen = urllib.request.urlopen,
) -> dict[str, str]:
    """Call the versioned internal API and return its validated response."""
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise OwnerAiClientError("Invalid Telegram user ID.")
    normalized = " ".join(str(question or "").split())
    if not normalized:
        raise OwnerAiClientError("Question must not be empty.")
    if len(normalized) > config.max_question_chars:
        raise OwnerAiClientError("Question is too long.")
    normalized_conversation_id = None
    if conversation_id is not None:
        normalized_conversation_id = str(conversation_id).strip()
        if (
            not normalized_conversation_id
            or len(normalized_conversation_id) > 160
            or not all(
                character.isascii()
                and (
                    character.isalnum()
                    or character in {":", "_", "-"}
                )
                for character in normalized_conversation_id
            )
        ):
            raise OwnerAiClientError("Invalid conversation ID.")

    payload = {
        "version": "1",
        "user_id": user_id,
        "question": normalized,
    }
    if normalized_conversation_id is not None:
        payload["conversation_id"] = normalized_conversation_id
    request_body = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        config.url,
        data=request_body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {config.token}",
            "Content-Type": "application/json",
            "User-Agent": "coffee-service-bot/1.0",
        },
    )
    try:
        with urlopen(request, timeout=config.timeout_seconds) as response:
            if response.status != 200:
                raise OwnerAiClientError(
                    f"Owner AI returned HTTP {response.status}."
                )
            raw = response.read(config.max_response_bytes + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise OwnerAiAccessError(
                "ИИ ещё не разрешён для этого сотрудника."
            ) from exc
        if exc.code == 401:
            raise OwnerAiClientError(
                "Сервис ИИ отклонил внутреннюю авторизацию."
            ) from exc
        raise OwnerAiClientError(
            "Сервис ИИ временно недоступен."
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OwnerAiClientError(
            "Сервис ИИ временно недоступен."
        ) from exc

    if len(raw) > config.max_response_bytes:
        raise OwnerAiClientError("Owner AI response is too large.")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OwnerAiClientError(
            "Owner AI returned invalid JSON."
        ) from exc
    if not isinstance(payload, dict) or payload.get("version") != "1":
        raise OwnerAiClientError("Owner AI returned an unsupported response.")
    scope = payload.get("scope")
    answer = payload.get("answer")
    if scope not in {"owner", "staff"}:
        raise OwnerAiClientError("Owner AI returned an invalid access scope.")
    if not isinstance(answer, str) or not answer.strip():
        raise OwnerAiClientError("Owner AI returned an empty answer.")
    answer = answer.strip()
    if len(answer) > config.max_answer_chars:
        raise OwnerAiClientError("Owner AI answer is too long.")
    return {"scope": scope, "answer": answer}
