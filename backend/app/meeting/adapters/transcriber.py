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

from pathlib import Path
from typing import Protocol

import httpx

_FIXTURE = (
    Path(__file__).resolve().parent.parent / "fixtures" / "transcript.txt"
)
_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_MODEL = "whisper-1"
_DEFAULT_TIMEOUT = 120.0
_AUDIO_TYPES = {
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".mpeg": "audio/mpeg",
    ".mpga": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".webm": "audio/webm",
}
_MISSING_KEY = (
    "ASR provider is not configured. Set MOCK_ASR=1 or "
    "OPENAI_API_KEY for OpenAI Whisper."
)


class ProviderNotConfigured(RuntimeError):
    pass


class Transcriber(Protocol):
    name: str

    def transcribe(self, audio: bytes, *, filename: str) -> str: ...


class MockTranscriber:
    """Default ASR. Returns a fixed transcript fixture. Ignores audio bytes."""

    name = "mock"

    def __init__(self, fixture: Path | None = None) -> None:
        self._fixture = fixture or _FIXTURE

    def transcribe(self, audio: bytes, *, filename: str) -> str:
        del audio, filename
        return self._fixture.read_text(encoding="utf-8").strip()


class UnconfiguredTranscriber:
    name = "unconfigured"

    def transcribe(self, audio: bytes, *, filename: str) -> str:
        del audio, filename
        raise ProviderNotConfigured(_MISSING_KEY)


class OpenAIWhisperTranscriber:
    """OpenAI audio.transcriptions, including compatible HTTP endpoints.

    The API key is supplied by the caller. This class does not read a
    fixture and does not log the key.
    """

    name = "openai_whisper"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = _DEFAULT_BASE_URL,
        model: str = _DEFAULT_MODEL,
        client: httpx.Client | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._api_key = api_key.strip()
        root = (base_url or "").strip().rstrip("/") or _DEFAULT_BASE_URL
        self._url = f"{root}/audio/transcriptions"
        self._model = (model or "").strip() or _DEFAULT_MODEL
        self._client = client or httpx.Client(timeout=timeout)

    def transcribe(self, audio: bytes, *, filename: str) -> str:
        if not self._api_key:
            raise ProviderNotConfigured(_MISSING_KEY)
        safe_name = _audio_filename(filename)
        response = self._client.post(
            self._url,
            headers={"Authorization": f"Bearer {self._api_key}"},
            data={"model": self._model, "response_format": "json"},
            files={
                "file": (
                    safe_name,
                    audio,
                    _audio_content_type(safe_name),
                )
            },
        )
        if response.status_code >= 400:
            detail = _error_detail(response, self._api_key)
            message = (
                f"Whisper transcription failed: HTTP {response.status_code}"
            )
            if detail:
                message = f"{message}: {detail}"
            raise RuntimeError(message)
        return _transcript_from_response(response)


def _audio_filename(filename: str) -> str:
    name = Path(filename or "").name.strip()
    if not name or name in {".", ".."}:
        return "audio.bin"
    return name


def _audio_content_type(filename: str) -> str:
    return _AUDIO_TYPES.get(
        Path(filename).suffix.lower(), "application/octet-stream"
    )


def _transcript_from_response(response: httpx.Response) -> str:
    content_type = response.headers.get("content-type", "").lower()
    body = response.text or ""
    if "json" in content_type:
        return _transcript_from_json(response)
    if content_type.startswith("text/"):
        return body
    if body.lstrip().startswith("{"):
        return _transcript_from_json(response)
    return body


def _transcript_from_json(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception as exc:
        raise RuntimeError(
            "Whisper transcription failed: response was not JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(
            "Whisper transcription failed: response missing transcript text"
        )
    text = payload.get("text")
    if not isinstance(text, str):
        raise RuntimeError(
            "Whisper transcription failed: response missing transcript text"
        )
    return text


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
    if api_key:
        detail = detail.replace(api_key, "[redacted]")
    return detail[:300]
