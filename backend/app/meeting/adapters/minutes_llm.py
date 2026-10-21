# ========= Copyright 2025-2026 @ Eigent.ai All Rights Reserved. =========
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ========= Copyright 2025-2026 @ Eigent.ai All Rights Reserved. =========

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Protocol

import httpx

from app.meeting.adapters.transcriber import ProviderNotConfigured

_ROOT = Path(__file__).resolve().parent.parent / "fixtures"
_MINUTES = _ROOT / "minutes.json"
_TODOS = _ROOT / "todos.json"

# Data-plane root from the Ark OpenAI-compatible docs. The chat method is
# POST {base}/chat/completions. Do not use the control-plane host.
VOLCANO_DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
VOLCANO_DEFAULT_MODEL = "doubao-pro-32k"
_DEFAULT_TIMEOUT = 120.0
_DECISION_MIN = 3
_DECISION_MAX = 5
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FAIL = "Volcano minutes request failed: "
_MISSING_KEY = (
    "Minutes LLM is not configured. Set MOCK_LLM=1 or "
    "VOLCANO_API_KEY (or ARK_API_KEY) for Volcano Ark."
)
_MINUTES_SYSTEM = (
    "You draft meeting minutes from a transcript. "
    "Reply with one JSON object and no other text. "
    "Write title, decisions, summary, and body in the transcript language. "
    "Required keys: title (non-empty string), decisions (array of 3 to 5 "
    "non-empty strings grounded in the transcript), summary (non-empty "
    "string), body (string). Do not invent decisions the transcript does "
    "not support."
)
_TODOS_SYSTEM = (
    "You extract action items from a meeting transcript and its minutes. "
    "Reply with one JSON object and no other text, shaped as "
    '{"todos": [ ... ]}. Each item has title (non-empty string), '
    "description (non-empty string), assignee (string or null; a name "
    "spoken in the transcript, never an invented id), due_date "
    "(YYYY-MM-DD or null), and source_quote (short transcript quote or "
    "null). Include every concrete action item. Use null when assignee "
    "or due date is unknown."
)


class MinutesLLM(Protocol):
    name: str

    def draft_minutes(self, transcript: str) -> dict[str, Any]: ...

    def extract_todos(
        self, transcript: str, minutes: dict[str, Any]
    ) -> list[dict[str, Any]]: ...


class MockMinutesLLM:
    """Default minutes model. Returns fixed minutes and todo fixtures."""

    name = "mock"

    def __init__(
        self,
        minutes_path: Path | None = None,
        todos_path: Path | None = None,
    ) -> None:
        self._minutes_path = minutes_path or _MINUTES
        self._todos_path = todos_path or _TODOS

    def draft_minutes(self, transcript: str) -> dict[str, Any]:
        del transcript
        return json.loads(self._minutes_path.read_text(encoding="utf-8"))

    def extract_todos(
        self, transcript: str, minutes: dict[str, Any]
    ) -> list[dict[str, Any]]:
        del transcript, minutes
        return json.loads(self._todos_path.read_text(encoding="utf-8"))


class UnconfiguredMinutesLLM:
    name = "unconfigured"

    def draft_minutes(self, transcript: str) -> dict[str, Any]:
        del transcript
        raise ProviderNotConfigured(_MISSING_KEY)

    def extract_todos(
        self, transcript: str, minutes: dict[str, Any]
    ) -> list[dict[str, Any]]:
        del transcript, minutes
        raise ProviderNotConfigured(_MISSING_KEY)


class VolcanoMinutesLLM:
    """Volcano Ark chat completions, OpenAI-compatible HTTP.

    The API key is supplied by the caller. This class does not read a
    fixture and does not log the key. Error text is redacted before it
    is raised so pipeline logs do not keep the key.
    """

    name = "volcano_ark"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = VOLCANO_DEFAULT_BASE_URL,
        model: str = VOLCANO_DEFAULT_MODEL,
        client: httpx.Client | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._api_key = api_key.strip()
        root = (base_url or "").strip().rstrip("/") or VOLCANO_DEFAULT_BASE_URL
        self._url = f"{root}/chat/completions"
        self._model = (model or "").strip() or VOLCANO_DEFAULT_MODEL
        self._client = client or httpx.Client(timeout=timeout)

    def draft_minutes(self, transcript: str) -> dict[str, Any]:
        payload = self._complete(_MINUTES_SYSTEM, _minutes_user(transcript))
        return _minutes_from_model(payload)

    def extract_todos(
        self, transcript: str, minutes: dict[str, Any]
    ) -> list[dict[str, Any]]:
        payload = self._complete(
            _TODOS_SYSTEM, _todos_user(transcript, minutes)
        )
        return _todos_from_model(payload)

    def _complete(self, system: str, user: str) -> Any:
        if not self._api_key:
            raise ProviderNotConfigured(_MISSING_KEY)
        try:
            response = self._client.post(
                self._url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0.2,
                    "response_format": {"type": "json_object"},
                },
            )
        except httpx.HTTPError as exc:
            detail = _redact(" ".join(str(exc).split()), self._api_key)
            raise RuntimeError(_FAIL + detail) from None
        if response.status_code >= 400:
            detail = _error_detail(response, self._api_key)
            message = _FAIL + f"HTTP {response.status_code}"
            if detail:
                message = f"{message}: {detail}"
            raise RuntimeError(message)
        return _load_model_json(_message_text(response))


def _minutes_user(transcript: str) -> str:
    return f"Transcript:\n{transcript}"


def _todos_user(transcript: str, minutes: dict[str, Any]) -> str:
    encoded = json.dumps(
        minutes, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return f"Transcript:\n{transcript}\n\nMinutes JSON:\n{encoded}"


def _message_text(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        raise RuntimeError(_FAIL + "response was not JSON") from None
    if not isinstance(payload, dict):
        raise RuntimeError(_FAIL + "response was not a JSON object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError(_FAIL + "response missing choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise RuntimeError(_FAIL + "response missing message content")
    message = first.get("message")
    if not isinstance(message, dict):
        raise RuntimeError(_FAIL + "response missing message content")
    return _content_text(message.get("content"))


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        text = "".join(parts).strip()
    else:
        text = ""
    if not text:
        raise RuntimeError(_FAIL + "model output was empty")
    return text


def _load_model_json(text: str) -> Any:
    try:
        return json.loads(_strip_fence(text))
    except json.JSONDecodeError:
        raise RuntimeError(_FAIL + "model output was not JSON") from None


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _minutes_from_model(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RuntimeError(_FAIL + "minutes JSON must be an object")
    raw = payload
    if "title" not in raw and isinstance(raw.get("minutes"), dict):
        raw = raw["minutes"]
    title = _required_str(raw.get("title"), "minutes title")
    summary = _required_str(raw.get("summary"), "minutes summary")
    body = raw.get("body", "")
    if body is None:
        body = ""
    if not isinstance(body, str):
        raise RuntimeError(_FAIL + "minutes body must be a string")
    decisions = raw.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        raise RuntimeError(_FAIL + "minutes decisions are missing")
    cleaned = [_required_str(item, "minutes decision") for item in decisions]
    if not _DECISION_MIN <= len(cleaned) <= _DECISION_MAX:
        raise RuntimeError(
            _FAIL + "minutes decisions must contain 3 to 5 items"
        )
    return {
        "title": title,
        "decisions": cleaned,
        "summary": summary,
        "body": body.strip(),
    }


def _todos_from_model(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        raw: Any = payload
    elif isinstance(payload, dict) and isinstance(payload.get("todos"), list):
        raw = payload["todos"]
    else:
        raise RuntimeError(_FAIL + "todos JSON must be a list")
    if not raw:
        raise RuntimeError(_FAIL + "todos JSON was empty")
    todos: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise RuntimeError(_FAIL + "todo must be an object")
        todos.append(
            {
                "title": _required_str(item.get("title"), "todo title"),
                "description": _required_str(
                    item.get("description"), "todo description"
                ),
                "assignee": _optional_str(item.get("assignee"), "assignee"),
                "due_date": _optional_date(item.get("due_date")),
                "source_quote": _optional_str(
                    item.get("source_quote"), "source_quote"
                ),
            }
        )
    return todos


def _required_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(_FAIL + f"{label} is missing")
    return value.strip()


def _optional_str(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeError(_FAIL + f"todo {label} must be a string or null")
    cleaned = value.strip()
    return cleaned or None


def _optional_date(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeError(_FAIL + "todo due_date must be YYYY-MM-DD or null")
    cleaned = value.strip()
    if not cleaned:
        return None
    if not _DATE_RE.fullmatch(cleaned):
        raise RuntimeError(_FAIL + "todo due_date must be YYYY-MM-DD or null")
    return cleaned


def _error_detail(response: httpx.Response, api_key: str) -> str:
    detail = ""
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            detail = error["message"]
        elif isinstance(payload.get("message"), str):
            detail = payload["message"]
    if not detail:
        detail = response.text or ""
    detail = " ".join(detail.split())
    return _redact(detail, api_key)[:300]


def _redact(text: str, api_key: str) -> str:
    if api_key and text:
        return text.replace(api_key, "[redacted]")
    return text
