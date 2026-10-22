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

import base64
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

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
# 豆包录音文件识别标准版. Submit, then poll query. Not flash auc_turbo.
# Submit: https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit
# Query:  https://openspeech.bytedance.com/api/v3/auc/bigmodel/query
# Model 2.0 resource volc.seedasr.auc. Model 1.0 is volc.bigasr.auc.
# Header X-Api-Status-Code 20000000 is success. Query 20000001 is
# processing and 20000002 is queued. Silence is 20000003.
# https://docs.volcengine.com/docs/6561/1354868
# https://docs.volcengine.com/docs/DoubaoVoice/task-submission-http-1
# https://docs.volcengine.com/docs/DoubaoVoice/result-query-http-1
# Inline base64 audio.data avoids a public URL. The standard field table
# names audio.url; url and data are the two sources on this schema
# (flash docs: 二选一, other request fields match the standard API).
VOLCANO_ASR_SUBMIT_URL = (
    "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
)
VOLCANO_ASR_QUERY_URL = (
    "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
)
VOLCANO_ASR_DEFAULT_RESOURCE_ID = "volc.seedasr.auc"
VOLCANO_ASR_DEFAULT_UID = "dept-meeting-agent"
VOLCANO_ASR_DEFAULT_HTTP_TIMEOUT = 60.0
VOLCANO_ASR_DEFAULT_POLL_TIMEOUT = 1800.0
VOLCANO_ASR_DEFAULT_POLL_INTERVAL = 2.0
VOLCANO_ASR_DEFAULT_POLL_MAX_INTERVAL = 15.0
VOLCANO_ASR_MAX_BYTES = 512 * 1024 * 1024
VOLCANO_ASR_SUCCESS_CODE = "20000000"
VOLCANO_ASR_PENDING_CODES = frozenset({"20000001", "20000002"})
_DOUBAO_FAIL = "Doubao ASR failed: "
_VOLCANO_AUDIO_FORMATS = {
    ".wav": ("wav", ""),
    ".mp3": ("mp3", ""),
    ".ogg": ("ogg", "opus"),
    ".opus": ("ogg", "opus"),
    ".spx": ("spx", ""),
    ".amr": ("amr", ""),
    ".aac": ("aac", ""),
    ".m4a": ("m4a", ""),
}
VOLCANO_ASR_MISSING_KEY = (
    "豆包语音 ASR 未配置 / Doubao speech ASR is not configured. "
    "Set MOCK_ASR=1, or set VOLCANO_ASR_API_KEY from the "
    "豆包语音控制台 (Volcano openspeech, "
    "https://openspeech.bytedance.com). "
    "VOLCANO_API_KEY is the Ark minutes key and is not an "
    "openspeech speech key. Older consoles may set "
    "VOLCANO_ASR_APP_KEY and VOLCANO_ASR_ACCESS_KEY together."
)
OPENAI_ASR_MISSING_KEY = (
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

    def __init__(self, message: str | None = None) -> None:
        self._message = message or VOLCANO_ASR_MISSING_KEY

    def transcribe(self, audio: bytes, *, filename: str) -> str:
        del audio, filename
        raise ProviderNotConfigured(self._message)


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
            raise ProviderNotConfigured(OPENAI_ASR_MISSING_KEY)
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


class VolcanoDoubaoTranscriber:
    """Volcano openspeech standard ASR (豆包录音文件识别标准版).

    Submits in-process audio as base64 ``audio.data``, then polls the
    query endpoint. The speech API Key is supplied by the caller and is
    not the Ark minutes key. This class does not read a fixture and does
    not log the key.
    """

    name = "volcano_doubao"

    def __init__(
        self,
        *,
        api_key: str = "",
        app_key: str = "",
        access_key: str = "",
        resource_id: str = "",
        uid: str = "",
        submit_url: str = "",
        query_url: str = "",
        client: httpx.Client | None = None,
        http_timeout: float = VOLCANO_ASR_DEFAULT_HTTP_TIMEOUT,
        poll_timeout: float = VOLCANO_ASR_DEFAULT_POLL_TIMEOUT,
        poll_interval: float = VOLCANO_ASR_DEFAULT_POLL_INTERVAL,
        poll_max_interval: float = VOLCANO_ASR_DEFAULT_POLL_MAX_INTERVAL,
        max_bytes: int = VOLCANO_ASR_MAX_BYTES,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self._api_key = api_key.strip()
        self._app_key = app_key.strip()
        self._access_key = access_key.strip()
        self._resource_id = (
            resource_id.strip() or VOLCANO_ASR_DEFAULT_RESOURCE_ID
        )
        self._uid = uid.strip() or VOLCANO_ASR_DEFAULT_UID
        self._submit_url = submit_url.strip() or VOLCANO_ASR_SUBMIT_URL
        self._query_url = query_url.strip() or VOLCANO_ASR_QUERY_URL
        self._http_timeout = _positive_number(
            http_timeout, VOLCANO_ASR_DEFAULT_HTTP_TIMEOUT
        )
        self._poll_timeout = _positive_number(
            poll_timeout, VOLCANO_ASR_DEFAULT_POLL_TIMEOUT
        )
        self._poll_interval = _positive_number(
            poll_interval, VOLCANO_ASR_DEFAULT_POLL_INTERVAL
        )
        self._poll_max_interval = _positive_number(
            poll_max_interval, VOLCANO_ASR_DEFAULT_POLL_MAX_INTERVAL
        )
        if self._poll_max_interval < self._poll_interval:
            self._poll_max_interval = self._poll_interval
        if max_bytes is None or max_bytes <= 0:
            max_bytes = VOLCANO_ASR_MAX_BYTES
        self._max_bytes = max_bytes
        self._clock = clock or time.monotonic
        self._sleeper = sleeper or time.sleep
        self._client = client or httpx.Client(timeout=self._http_timeout)

    def transcribe(self, audio: bytes, *, filename: str) -> str:
        self._require_credentials()
        if not audio:
            raise RuntimeError(_DOUBAO_FAIL + "audio is empty")
        if len(audio) > self._max_bytes:
            raise RuntimeError(
                _DOUBAO_FAIL
                + "audio exceeds the standard limit of "
                + _byte_limit(self._max_bytes)
            )
        request_id = str(uuid.uuid4())
        self._submit(request_id, audio, filename)
        return self._poll(request_id)

    def _submit(self, request_id: str, audio: bytes, filename: str) -> None:
        try:
            response = self._client.post(
                self._submit_url,
                headers=self._headers(request_id, sequence=True),
                json=self._payload(audio, filename),
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(self._transport_error(exc)) from None
        payload = _optional_json(response)
        status = _api_status(response, payload)
        logid = response.headers.get("X-Tt-Logid", "").strip()
        if response.status_code >= 400 or status != VOLCANO_ASR_SUCCESS_CODE:
            # Do not echo a nested result.text. A submit body is not a
            # transcript, and a missing status is not success.
            if not status and response.status_code < 400:
                detail = "submit missing X-Api-Status-Code"
            else:
                detail = _status_detail(response, payload)
            raise RuntimeError(
                _failure_message(
                    response.status_code,
                    status,
                    detail,
                    logid,
                    self._secrets(),
                )
            )

    def _poll(self, request_id: str) -> str:
        deadline = self._clock() + self._poll_timeout
        interval = self._poll_interval
        headers = self._headers(request_id, sequence=False)
        transport_error = ""
        while True:
            try:
                response = self._client.post(
                    self._query_url,
                    headers=headers,
                    json={},
                )
            except httpx.HTTPError as exc:
                transport_error = self._transport_error(exc).removeprefix(
                    _DOUBAO_FAIL
                )
                self._wait_or_timeout(deadline, interval, transport_error, "")
                interval = self._next_interval(interval)
                continue
            payload = _optional_json(response)
            status = _api_status(response, payload)
            logid = response.headers.get("X-Tt-Logid", "").strip()
            if (
                status in VOLCANO_ASR_PENDING_CODES
                and response.status_code < 400
            ):
                self._wait_or_timeout(deadline, interval, "", logid)
                interval = self._next_interval(interval)
                continue
            if response.status_code >= 400 or (
                status and status != VOLCANO_ASR_SUCCESS_CODE
            ):
                detail = _status_detail(response, payload)
                raise RuntimeError(
                    _failure_message(
                        response.status_code,
                        status,
                        detail,
                        logid,
                        self._secrets(),
                    )
                )
            if payload is None:
                raise RuntimeError(
                    _with_logid(_DOUBAO_FAIL + "response was not JSON", logid)
                )
            try:
                return _transcript_from_payload(payload)
            except RuntimeError as exc:
                raise RuntimeError(_with_logid(str(exc), logid)) from None

    def _wait_or_timeout(
        self,
        deadline: float,
        interval: float,
        transport_error: str,
        logid: str,
    ) -> None:
        now = self._clock()
        if now >= deadline:
            self._raise_timeout(transport_error, logid)
        delay = min(interval, deadline - now)
        if delay <= 0:
            self._raise_timeout(transport_error, logid)
        self._sleeper(delay)

    def _next_interval(self, interval: float) -> float:
        return min(interval * 2, self._poll_max_interval)

    def _raise_timeout(self, transport_error: str, logid: str) -> None:
        message = (
            _DOUBAO_FAIL
            + "query timed out after "
            + _format_seconds(self._poll_timeout)
            + "s"
        )
        if transport_error:
            message = f"{message}: {transport_error}"
        raise RuntimeError(
            _with_logid(message, _redact_secrets(logid, self._secrets()))
        )

    def _transport_error(self, exc: BaseException) -> str:
        detail = _redact_secrets(" ".join(str(exc).split()), self._secrets())
        return _DOUBAO_FAIL + detail

    def _require_credentials(self) -> None:
        if self._api_key:
            return
        if self._app_key and self._access_key:
            return
        raise ProviderNotConfigured(VOLCANO_ASR_MISSING_KEY)

    def _secrets(self) -> list[str]:
        return [self._api_key, self._app_key, self._access_key]

    def _headers(self, request_id: str, *, sequence: bool) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "X-Api-Resource-Id": self._resource_id,
            "X-Api-Request-Id": request_id,
        }
        if sequence:
            headers["X-Api-Sequence"] = "-1"
        if self._api_key:
            headers["X-Api-Key"] = self._api_key
        else:
            headers["X-Api-App-Key"] = self._app_key
            headers["X-Api-Access-Key"] = self._access_key
        return headers

    def _payload(self, audio: bytes, filename: str) -> dict[str, Any]:
        audio_body: dict[str, Any] = {
            "data": base64.b64encode(audio).decode("ascii"),
        }
        audio_format, codec = _volcano_format(filename)
        if audio_format:
            audio_body["format"] = audio_format
        if codec:
            audio_body["codec"] = codec
        return {
            "user": {"uid": self._uid},
            "audio": audio_body,
            "request": {"model_name": "bigmodel"},
        }


def _volcano_format(filename: str) -> tuple[str, str]:
    suffix = Path(_audio_filename(filename)).suffix.lower()
    return _VOLCANO_AUDIO_FORMATS.get(suffix, ("", ""))


def _positive_number(value: float | None, default: float) -> float:
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number <= 0:
        return default
    return number


def _format_seconds(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return str(value)


def _byte_limit(max_bytes: int) -> str:
    if max_bytes == VOLCANO_ASR_MAX_BYTES:
        return "512MB"
    return f"{max_bytes} bytes"


def _optional_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return None


def _api_status(response: httpx.Response, payload: Any) -> str:
    header = response.headers.get("X-Api-Status-Code", "").strip()
    if header:
        return header
    if not isinstance(payload, dict):
        return ""
    for key in ("code", "status_code"):
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return str(value)
        if isinstance(value, str) and value.strip().isdigit():
            return value.strip()
    return ""


def _status_detail(response: httpx.Response, payload: Any) -> str:
    message = response.headers.get("X-Api-Message", "").strip()
    if message and message.upper() != "OK":
        return message
    body_message = _body_message(payload)
    if body_message and body_message.upper() != "OK":
        return body_message
    return response.text or ""


def _body_message(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("message", "msg"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    error = payload.get("error")
    if isinstance(error, dict):
        nested = error.get("message")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    if isinstance(error, str) and error.strip():
        return error.strip()
    return ""


def _failure_message(
    http_status: int,
    status: str,
    detail: str,
    logid: str,
    secrets: list[str],
) -> str:
    bits: list[str] = []
    if http_status >= 400:
        bits.append(f"HTTP {http_status}")
    if status:
        bits.append(f"status {status}")
    head = ", ".join(bits) if bits else "request failed"
    message = _DOUBAO_FAIL + head
    cleaned = _redact_secrets(" ".join((detail or "").split()), secrets)
    if cleaned:
        message = f"{message}: {cleaned[:300]}"
    return _with_logid(message, _redact_secrets(logid, secrets))


def _with_logid(message: str, logid: str) -> str:
    if logid:
        return f"{message} [logid={logid}]"
    return message


def _transcript_from_payload(payload: Any) -> str:
    if isinstance(payload, dict):
        text = _text_from_node(_result_node(payload))
        if text:
            return text
        top = payload.get("text")
        if isinstance(top, str) and top.strip():
            return top
    elif isinstance(payload, list):
        text = _text_from_node(payload)
        if text:
            return text
    raise RuntimeError(_DOUBAO_FAIL + "response missing transcript text")


def _result_node(payload: dict[str, Any]) -> Any:
    if "result" in payload:
        return payload.get("result")
    body = payload.get("body")
    if isinstance(body, dict) and "result" in body:
        return body.get("result")
    return None


def _text_from_node(node: Any) -> str | None:
    if isinstance(node, str):
        stripped = node.strip()
        return node if stripped else None
    if isinstance(node, dict):
        text = node.get("text")
        if isinstance(text, str) and text.strip():
            return text
        joined = _join_utterances(node.get("utterances"))
        if joined:
            return joined
        return None
    if isinstance(node, list):
        parts: list[str] = []
        for item in node:
            piece = _text_from_node(item)
            if piece:
                parts.append(piece)
        if parts:
            return "".join(parts)
        return None
    return None


def _join_utterances(utterances: Any) -> str:
    if not isinstance(utterances, list):
        return ""
    parts: list[str] = []
    for item in utterances:
        if isinstance(item, str):
            piece = item.strip()
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            piece = item["text"].strip()
        else:
            piece = ""
        if piece:
            parts.append(piece)
    return "".join(parts)


def _redact_secrets(text: str, secrets: list[str]) -> str:
    if not text:
        return ""
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted


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
